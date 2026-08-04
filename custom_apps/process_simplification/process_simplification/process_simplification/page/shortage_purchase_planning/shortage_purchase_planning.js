frappe.pages["shortage-purchase-planning"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("缺料采购"),
		single_column: true,
	});

	page.main.html(`
		<div class="process-simplification-page shortage-purchase-planning">
			<div class="row form-section">
				<div class="col-md-4" data-field="sales_order"></div>
				<div class="col-md-4" data-field="schedule_date"></div>
				<div class="col-md-4 text-right shortage-actions"></div>
			</div>
			<div class="form-section selected-wrapper">
				<h5>${__("待检查订单明细")}</h5>
				<div class="selected-rows text-muted">${__("从订单履约总览选择订单行，或输入销售订单后读取。")}</div>
			</div>
			<div class="form-section">
				<h5>${__("缺料结果")}</h5>
				<div class="table-responsive">
					<table class="table table-bordered shortage-table">
						<thead>
							<tr>
								<th><input type="checkbox" class="select-all" checked></th>
								<th>${__("原料")}</th>
								<th class="text-right">${__("总需求")}</th>
								<th class="text-right">${__("当前库存")}</th>
								<th class="text-right">${__("采购申请")}</th>
								<th class="text-right">${__("采购订单")}</th>
								<th class="text-right">${__("采购缺口")}</th>
								<th class="text-right">${__("本次采购")}</th>
								<th>${__("来源")}</th>
							</tr>
						</thead>
						<tbody></tbody>
					</table>
				</div>
			</div>
		</div>
	`);

	const $root = page.main.find(".shortage-purchase-planning");
	let selected_rows = (frappe.route_options && frappe.route_options.selected_rows) || [];
	let shortage_rows = [];

	const sales_order_field = frappe.ui.form.make_control({
		parent: $root.find('[data-field="sales_order"]'),
		df: { fieldtype: "Link", options: "Sales Order", label: __("销售订单") },
		render_input: true,
	});
	const schedule_date_field = frappe.ui.form.make_control({
		parent: $root.find('[data-field="schedule_date"]'),
		df: { fieldtype: "Date", label: __("需要日期"), default: frappe.datetime.add_days(frappe.datetime.nowdate(), 1) },
		render_input: true,
	});

	function fmt(value) {
		return format_number(flt(value), null, 2);
	}

	function render_selected() {
		if (!selected_rows.length) {
			$root.find(".selected-rows").html(__("暂无已选择订单明细。"));
			return;
		}
		$root.find(".selected-rows").html(
			selected_rows.map((row) => `<span class="badge badge-default mr-2">${frappe.utils.escape_html(row.sales_order)} / ${frappe.utils.escape_html(row.sales_order_item)}</span>`).join(" ")
		);
	}

	function load_from_sales_order() {
		const sales_order = sales_order_field.get_value();
		if (!sales_order) return;
		frappe.call({
			method: "process_simplification.api.workbench.get_order_workbench",
			args: { sales_order },
			freeze: true,
		}).then((r) => {
			selected_rows = (r.message.rows || [])
				.filter((row) => !row.unsupported && (flt(row.uncovered_qty) > 0 || flt(row.active_work_order_qty) > 0))
				.map((row) => ({ sales_order, sales_order_item: row.sales_order_item }));
			render_selected();
		});
	}

	function check_shortage() {
		frappe.call({
			method: "process_simplification.api.shortage.check_shortage",
			args: { selected_rows },
			freeze: true,
			freeze_message: __("正在检查缺料..."),
		}).then((r) => {
			shortage_rows = (r.message && r.message.shortages) || [];
			render_shortages();
			if (!shortage_rows.length) {
				frappe.msgprint((r.message && r.message.message) || __("没有需要采购的缺料。"));
			}
		});
	}

	function render_shortages() {
		$root.find("tbody").html(
			shortage_rows.map((row, index) => {
				const sources = (row.sources || []).map((source) => `${source.sales_order}/${source.finished_item}: ${fmt(source.qty)}`).join("<br>");
				return `
					<tr data-index="${index}">
						<td><input type="checkbox" class="shortage-select" checked></td>
						<td>${frappe.utils.escape_html(row.item_code)}<br><small>${frappe.utils.escape_html(row.item_name || "")}</small></td>
						<td class="text-right">${fmt(row.required_qty)}</td>
						<td class="text-right">${fmt(row.available_qty)}</td>
						<td class="text-right">${fmt(row.open_material_request_qty)}</td>
						<td class="text-right">${fmt(row.open_purchase_order_qty)}</td>
						<td class="text-right">${fmt(row.shortage_qty)}</td>
						<td><input class="form-control input-sm text-right purchase-qty" type="number" min="0" step="any" value="${row.shortage_qty}"></td>
						<td><small>${sources}</small></td>
					</tr>
				`;
			}).join("")
		);
	}

	function selected_shortages() {
		return $root.find("tbody tr").toArray().filter((tr) => $(tr).find(".shortage-select").prop("checked")).map((tr) => {
			const index = cint($(tr).data("index"));
			return Object.assign({}, shortage_rows[index], {
				purchase_qty: flt($(tr).find(".purchase-qty").val()),
				schedule_date: schedule_date_field.get_value(),
			});
		});
	}

	function create_material_request() {
		const rows = selected_shortages();
		if (!rows.length) {
			frappe.msgprint(__("请至少选择一条缺料记录。"));
			return;
		}
		frappe.confirm(__("确认生成并提交采购申请？"), () => {
			frappe.call({
				method: "process_simplification.api.shortage.create_material_request",
				args: { shortage_rows: rows, schedule_date: schedule_date_field.get_value() },
				freeze: true,
				freeze_message: __("正在生成采购申请..."),
			}).then((r) => {
				if (r.message && r.message.material_request) {
					frappe.set_route("Form", "Material Request", r.message.material_request);
				}
			});
		});
	}

	$root.on("change", ".select-all", (event) => {
		$root.find(".shortage-select").prop("checked", $(event.currentTarget).prop("checked"));
	});
	page.add_inner_button(__("读取订单"), load_from_sales_order);
	page.add_inner_button(__("检查缺料"), check_shortage);
	page.set_primary_action(__("生成采购申请"), create_material_request);
	render_selected();
};

frappe.pages["shortage-purchase-planning"].refresh = function () {
	frappe.app.sidebar.set_workspace_sidebar();
};
