frappe.pages["shortage-purchase-planning"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("缺料采购"),
		single_column: true,
	});

	page.main.html(`
		<div class="process-simplification-page shortage-purchase-planning">
			<div class="shortage-scope-bar">
				<span class="shortage-scope-label"></span>
				<div class="shortage-by-order" data-field="sales_order"></div>
			</div>
			<div class="form-section">
				<div class="table-responsive">
					<table class="table shortage-table">
						<thead>
							<tr>
								<th class="col-check"><input type="checkbox" class="select-all" checked></th>
								<th>${__("原料")}</th>
								<th class="text-right">${__("采购缺口")}</th>
								<th class="text-right">${__("本次采购")}</th>
								<th>${__("来源订单")}</th>
							</tr>
						</thead>
						<tbody></tbody>
					</table>
					<div class="shortage-empty text-muted"></div>
				</div>
			</div>
		</div>
	`);

	const $root = page.main.find(".shortage-purchase-planning");
	let selected_rows = (frappe.route_options && frappe.route_options.selected_rows) || [];
	let shortage_rows = [];
	let scope = selected_rows.length ? "selected" : "all";

	const sales_order_field = frappe.ui.form.make_control({
		parent: $root.find('[data-field="sales_order"]'),
		df: {
			fieldtype: "Link",
			fieldname: "sales_order",
			options: "Sales Order",
			label: __("按订单查"),
			change() {
				const so = sales_order_field.get_value();
				if (so) load_from_sales_order(so);
			},
		},
		render_input: true,
	});

	function fmt(value) {
		return format_number(flt(value), null, 2);
	}

	function set_scope_label() {
		const label = scope === "all" ? __("已汇总全部订单缺料") : __("按所选订单缺料");
		const count = shortage_rows.length;
		$root.find(".shortage-scope-label").text(count ? `${label} · ${count} ${__("项")}` : label);
	}

	function humanize_sources(sources) {
		const list = sources || [];
		if (!list.length) return "";
		const first = list[0];
		const name = frappe.utils.escape_html(first.customer_name || first.sales_order || "");
		const item = frappe.utils.escape_html(first.finished_item_name || first.finished_item || "");
		const head = `${name}${item ? ` · ${item}` : ""}`;
		if (list.length === 1) return head;
		return `${head} <span class="text-muted">${__("等 {0} 个订单", [list.length])}</span>`;
	}

	function source_detail(sources) {
		return (sources || [])
			.map((s) => {
				const name = frappe.utils.escape_html(s.customer_name || s.sales_order || "");
				const item = frappe.utils.escape_html(s.finished_item_name || s.finished_item || "");
				const date = s.delivery_date ? frappe.datetime.str_to_user(s.delivery_date) : "";
				return `<div class="shortage-source-line">${name} · ${item} · ${__("需")} ${fmt(s.required_qty)}${date ? ` · ${__("交期")} ${date}` : ""}</div>`;
			})
			.join("");
	}

	function render_shortages() {
		set_scope_label();
		const $body = $root.find("tbody");
		if (!shortage_rows.length) {
			$body.empty();
			$root.find(".shortage-empty").text(
				scope === "all" ? __("当前所有订单没有需要采购的缺料。") : __("所选订单没有需要采购的缺料。")
			);
			return;
		}
		$root.find(".shortage-empty").empty();
		$body.html(
			shortage_rows
				.map((row, index) => {
					const covered = flt(row.available_qty) + flt(row.open_material_request_qty) + flt(row.open_purchase_order_qty);
					return `
						<tr class="shortage-row" data-index="${index}">
							<td class="col-check"><input type="checkbox" class="shortage-select" checked></td>
							<td>
								<button class="btn btn-xs btn-default shortage-expand" data-index="${index}">+</button>
								<strong>${frappe.utils.escape_html(row.item_code)}</strong>
								<div class="text-muted small">${frappe.utils.escape_html(row.item_name || "")} · ${frappe.utils.escape_html(row.warehouse || "")}</div>
							</td>
							<td class="text-right shortage-gap">${fmt(row.shortage_qty)}</td>
							<td class="text-right"><input class="form-control input-sm text-right purchase-qty" type="number" min="0" step="any" value="${row.shortage_qty}"></td>
							<td class="shortage-source">${humanize_sources(row.sources)}</td>
						</tr>
						<tr class="shortage-detail-row" data-index="${index}" style="display:none">
							<td></td>
							<td colspan="4">
								<div class="shortage-detail">
									<span>${__("总需求")} <strong>${fmt(row.required_qty)}</strong></span>
									<span>${__("当前可用库存")} <strong>${fmt(row.available_qty)}</strong></span>
									<span>${__("已申请")} <strong>${fmt(row.open_material_request_qty)}</strong></span>
									<span>${__("已下单在途")} <strong>${fmt(row.open_purchase_order_qty)}</strong></span>
								</div>
								<div class="shortage-detail-sources">${source_detail(row.sources)}</div>
							</td>
						</tr>
					`;
				})
				.join("")
		);
	}

	function load_all() {
		scope = "all";
		frappe.call({
			method: "process_simplification.api.shortage.check_all_shortages",
			freeze: true,
			freeze_message: __("正在汇总全部订单缺料..."),
		}).then((r) => {
			shortage_rows = (r.message && r.message.shortages) || [];
			render_shortages();
		});
	}

	function load_from_sales_order(sales_order) {
		frappe.call({
			method: "process_simplification.api.workbench.get_order_workbench",
			args: { sales_order },
			freeze: true,
		}).then((r) => {
			const rows = (r.message.rows || [])
				.filter((row) => !row.unsupported && (flt(row.uncovered_qty) > 0 || flt(row.active_work_order_qty) > 0))
				.map((row) => ({ sales_order, sales_order_item: row.sales_order_item }));
			if (!rows.length) {
				frappe.msgprint(__("该订单没有需要生产的明细。"));
				return;
			}
			scope = "selected";
			frappe.call({
				method: "process_simplification.api.shortage.check_shortage",
				args: { selected_rows: rows },
				freeze: true,
				freeze_message: __("正在检查缺料..."),
			}).then((res) => {
				shortage_rows = (res.message && res.message.shortages) || [];
				render_shortages();
			});
		});
	}

	function selected_shortages() {
		return $root
			.find("tbody tr.shortage-row")
			.toArray()
			.filter((tr) => $(tr).find(".shortage-select").prop("checked"))
			.map((tr) => {
				const index = cint($(tr).data("index"));
				return Object.assign({}, shortage_rows[index], {
					purchase_qty: flt($(tr).find(".purchase-qty").val()),
				});
			});
	}

	function create_material_request() {
		const rows = selected_shortages();
		if (!rows.length) {
			frappe.msgprint(__("请至少选择一条缺料记录。"));
			return;
		}
		const dialog = new frappe.ui.Dialog({
			title: __("生成采购申请"),
			fields: [
				{
					fieldtype: "Date",
					fieldname: "schedule_date",
					label: __("需要日期"),
					default: frappe.datetime.add_days(frappe.datetime.nowdate(), 1),
					reqd: 1,
				},
				{
					fieldtype: "HTML",
					options: `<p class="text-muted">${__("将为所选 {0} 项缺料生成一张采购申请。", [rows.length])}</p>`,
				},
			],
			primary_action_label: __("确认生成"),
			primary_action(values) {
				dialog.hide();
				frappe.call({
					method: "process_simplification.api.shortage.create_material_request",
					args: { shortage_rows: rows, schedule_date: values.schedule_date },
					freeze: true,
					freeze_message: __("正在生成采购申请..."),
				}).then((r) => {
					if (r.message && r.message.material_request) {
						frappe.set_route("Form", "Material Request", r.message.material_request);
					}
				});
			},
		});
		dialog.show();
	}

	$root.on("change", ".select-all", (event) => {
		$root.find(".shortage-select").prop("checked", $(event.currentTarget).prop("checked"));
	});
	$root.on("click", ".shortage-expand", (event) => {
		const index = $(event.currentTarget).data("index");
		const $detail = $root.find(`.shortage-detail-row[data-index="${index}"]`);
		const open = $detail.is(":visible");
		$detail.toggle(!open);
		$(event.currentTarget).text(open ? "+" : "−");
	});

	page.add_inner_button(__("汇总全部缺料"), load_all);
	page.set_primary_action(__("生成采购申请"), create_material_request);

	// Result-first: show all shortage on entry unless routed in with specific rows.
	if (selected_rows.length) {
		scope = "selected";
		frappe.call({
			method: "process_simplification.api.shortage.check_shortage",
			args: { selected_rows },
			freeze: true,
			freeze_message: __("正在检查缺料..."),
		}).then((r) => {
			shortage_rows = (r.message && r.message.shortages) || [];
			render_shortages();
		});
	} else {
		load_all();
	}
};

frappe.pages["shortage-purchase-planning"].refresh = function () {
	frappe.app.sidebar.set_workspace_sidebar();
};
