frappe.pages["order-workbench"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("订单工作台"),
		single_column: true,
	});

	page.main.html(`
		<div class="process-simplification-page order-workbench">
			<div class="row form-section">
				<div class="col-md-4" data-field="sales_order"></div>
				<div class="col-md-8 text-right workbench-actions"></div>
			</div>
			<div class="workbench-summary text-muted"></div>
			<div class="table-responsive">
				<table class="table table-bordered table-hover workbench-table">
					<thead>
						<tr>
							<th><input type="checkbox" class="select-all"></th>
							<th>${__("产品")}</th>
							<th class="text-right">${__("订单")}</th>
							<th class="text-right">${__("已发")}</th>
							<th class="text-right">${__("待交")}</th>
							<th class="text-right">${__("已预留")}</th>
							<th class="text-right">${__("生产中")}</th>
							<th class="text-right">${__("已完工")}</th>
							<th class="text-right">${__("未覆盖")}</th>
							<th>${__("原料")}</th>
							<th>${__("状态")}</th>
							<th>${__("下一步")}</th>
						</tr>
					</thead>
					<tbody></tbody>
				</table>
			</div>
		</div>
	`);

	const $root = page.main.find(".order-workbench");
	const sales_order_field = frappe.ui.form.make_control({
		parent: $root.find('[data-field="sales_order"]'),
		df: {
			fieldname: "sales_order",
			fieldtype: "Link",
			options: "Sales Order",
			label: __("销售订单"),
			change: () => load_workbench(),
		},
		render_input: true,
	});
	page.fields_dict = page.fields_dict || {};
	page.fields_dict.sales_order = sales_order_field;

	function route_sales_order() {
		return frappe.get_route()[1] || (frappe.route_options && frappe.route_options.sales_order);
	}

	function fmt(value) {
		return format_number(flt(value), null, 2);
	}

	function load_workbench() {
		const sales_order = sales_order_field.get_value();
		if (!sales_order) return;
		frappe.call({
			method: "process_simplification.api.workbench.get_order_workbench",
			args: { sales_order },
			freeze: true,
			freeze_message: __("正在读取订单状态..."),
		}).then((r) => render(r.message));
	}

	function render(data) {
		const rows = data.rows || [];
		$root.find(".workbench-summary").html(
			`${__("客户")}: ${frappe.utils.escape_html(data.customer_name || data.customer || "")}`
		);
		$root.find("tbody").html(
			rows.map((row) => {
				const actions = (row.next_actions || [])
					.map((item) => `<button class="btn btn-xs btn-default row-action" data-action="${item.action}" data-row="${row.sales_order_item}" ${item.enabled ? "" : "disabled"}>${__(item.label)}</button>`)
					.join(" ");
				return `
					<tr class="${row.unsupported ? "text-muted" : ""}">
						<td><input type="checkbox" class="row-select" data-row="${row.sales_order_item}" ${row.unsupported ? "disabled" : "checked"}></td>
						<td><a href="/app/item/${encodeURIComponent(row.item_code)}">${frappe.utils.escape_html(row.item_code)}</a><br><small>${frappe.utils.escape_html(row.item_name || "")}</small></td>
						<td class="text-right">${fmt(row.order_qty)}</td>
						<td class="text-right">${fmt(row.delivered_qty)}</td>
						<td class="text-right">${fmt(row.pending_qty)}</td>
						<td class="text-right">${fmt(row.reserved_qty)}</td>
						<td class="text-right">${fmt(row.active_work_order_qty)}</td>
						<td class="text-right">${fmt(row.completed_qty)}</td>
						<td class="text-right">${fmt(row.uncovered_qty)}</td>
						<td>${frappe.utils.escape_html(row.material_status || "")}</td>
						<td><span class="indicator-pill gray">${frappe.utils.escape_html(row.status || "")}</span>${row.unsupported_reason ? `<br><small>${frappe.utils.escape_html(row.unsupported_reason)}</small>` : ""}</td>
						<td>${actions}</td>
					</tr>
				`;
			}).join("")
		);
	}

	function selected_rows() {
		const sales_order = sales_order_field.get_value();
		return $root.find(".row-select:checked").toArray().map((input) => ({
			sales_order,
			sales_order_item: $(input).data("row"),
		}));
	}

	function run_row_action(action, sales_order_item) {
		const sales_order = sales_order_field.get_value();
		const method_map = {
			reserve_stock: "process_simplification.api.actions.reserve_stock",
			create_work_order: "process_simplification.api.actions.create_work_order",
			reserve_completed_stock: "process_simplification.api.actions.reserve_completed_stock",
			create_delivery_note: "process_simplification.api.actions.create_delivery_note",
		};

		if (action === "view_sales_order") {
			frappe.set_route("Form", "Sales Order", sales_order);
			return;
		}
		if (action === "view_work_orders") {
			frappe.call({
				method: "process_simplification.api.workbench.get_work_order_details",
				args: { sales_order, sales_order_item },
			}).then((r) => show_work_orders(r.message.work_orders || []));
			return;
		}

		const method = method_map[action];
		if (!method) return;
		frappe.confirm(__("确认执行该操作？"), () => {
			frappe.call({
				method,
				args: { sales_order, sales_order_item },
				freeze: true,
			}).then(() => {
				frappe.show_alert({ message: __("操作完成"), indicator: "green" });
				load_workbench();
			});
		});
	}

	function show_work_orders(work_orders) {
		const html = work_orders.length
			? work_orders.map((wo) => `
				<div class="mb-3">
					<div><b><a href="/app/work-order/${encodeURIComponent(wo.name)}">${wo.name}</a></b> ${frappe.utils.escape_html(wo.status || "")}</div>
					<div class="text-muted">${__("数量")}: ${fmt(wo.qty)} / ${__("已生产")}: ${fmt(wo.produced_qty)}</div>
					<div>${__("原料")}: ${(wo.required_items || []).map((item) => `${frappe.utils.escape_html(item.item_code)} ${fmt(item.required_qty)}`).join(", ") || __("无")}</div>
				</div>
			`).join("")
			: __("暂无生产任务");
		frappe.msgprint({ title: __("生产任务"), message: html, wide: true });
	}

	$root.on("click", ".row-action", (event) => {
		const $button = $(event.currentTarget);
		run_row_action($button.data("action"), $button.data("row"));
	});
	$root.on("change", ".select-all", (event) => {
		$root.find(".row-select:not(:disabled)").prop("checked", $(event.currentTarget).prop("checked"));
	});

	page.add_inner_button(__("检查缺料"), () => {
		frappe.route_options = { selected_rows: selected_rows() };
		frappe.set_route("shortage-purchase-planning");
	});
	page.add_inner_button(__("刷新"), load_workbench);

	const initial = route_sales_order();
	if (initial) {
		sales_order_field.set_value(initial);
	}
};

frappe.pages["order-workbench"].refresh = function (wrapper) {
	const route_so = frappe.get_route()[1];
	if (route_so && wrapper.page && wrapper.page.fields_dict && wrapper.page.fields_dict.sales_order) {
		wrapper.page.fields_dict.sales_order.set_value(route_so);
	}
};
