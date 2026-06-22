frappe.pages["quick-sales-order"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("快速开单"),
		single_column: true,
	});

	page.main.html(`
		<div class="process-simplification-page quick-sales-order">
			<div class="row form-section">
				<div class="col-md-4" data-field="customer"></div>
				<div class="col-md-3" data-field="delivery_date"></div>
				<div class="col-md-3" data-field="allow_partial_delivery"></div>
			</div>
			<div class="form-section">
				<div data-field="remarks"></div>
			</div>
			<div class="form-section">
				<div class="clearfix">
					<h5 class="pull-left">${__("产品明细")}</h5>
					<button class="btn btn-xs btn-default pull-right add-row">${__("添加产品")}</button>
				</div>
				<div class="table-responsive">
					<table class="table table-bordered quick-items">
						<thead>
							<tr>
								<th style="width: 28%">${__("产品")}</th>
								<th style="width: 10%">${__("数量")}</th>
								<th style="width: 10%">${__("单价")}</th>
								<th style="width: 14%">${__("交付日期")}</th>
								<th style="width: 18%">${__("仓库")}</th>
								<th>${__("备注")}</th>
								<th style="width: 44px"></th>
							</tr>
						</thead>
						<tbody></tbody>
					</table>
				</div>
			</div>
		</div>
	`);

	const state = {
		row_index: 0,
		rows: new Map(),
		defaults: {},
	};
	const $root = page.main.find(".quick-sales-order");
	const fields = {
		customer: frappe.ui.form.make_control({
			parent: $root.find('[data-field="customer"]'),
			df: { fieldname: "customer", fieldtype: "Link", options: "Customer", label: __("客户"), reqd: 1 },
			render_input: true,
		}),
		delivery_date: frappe.ui.form.make_control({
			parent: $root.find('[data-field="delivery_date"]'),
			df: { fieldname: "delivery_date", fieldtype: "Date", label: __("默认交付日期"), reqd: 1 },
			render_input: true,
		}),
		allow_partial_delivery: frappe.ui.form.make_control({
			parent: $root.find('[data-field="allow_partial_delivery"]'),
			df: { fieldname: "allow_partial_delivery", fieldtype: "Check", label: __("允许分批发货"), default: 1 },
			render_input: true,
		}),
		remarks: frappe.ui.form.make_control({
			parent: $root.find('[data-field="remarks"]'),
			df: { fieldname: "remarks", fieldtype: "Small Text", label: __("备注") },
			render_input: true,
		}),
	};
	Object.values(fields).forEach((control) => {
		control.$wrapper.find(".tooltip-content").remove();
	});

	function product_query() {
		return {
			query: "process_simplification.api.quick_order.search_quick_order_products",
		};
	}

	function warehouse_query() {
		return {
			filters: {
				company: state.defaults.company,
				is_group: 0,
			},
		};
	}

	function make_cell_control($tr, fieldname, df) {
		const control = frappe.ui.form.make_control({
			parent: $tr.find(`[data-field="${fieldname}"]`),
			df: Object.assign({ fieldname, label: "" }, df),
			render_input: true,
		});
		control.toggle_label(false);
		return control;
	}

	function row_template(row_id) {
		return $(`
			<tr data-row-id="${row_id}">
				<td>
					<div data-field="item_code"></div>
					<div class="quick-row-help text-muted"></div>
				</td>
				<td data-field="qty"></td>
				<td data-field="rate"></td>
				<td data-field="delivery_date"></td>
				<td data-field="warehouse"></td>
				<td data-field="remarks"></td>
				<td><button class="btn btn-xs btn-default remove-row" title="${__("删除")}">×</button></td>
			</tr>
		`);
	}

	function get_row($tr) {
		return state.rows.get(cint($tr.data("row-id")));
	}

	function set_row_help($tr, detail) {
		const parts = [];
		if (detail.item_name && detail.item_name !== detail.item_code) {
			parts.push(frappe.utils.escape_html(detail.item_name));
		}
		if (detail.stock_uom) {
			parts.push(`${__("单位")}: ${frappe.utils.escape_html(detail.stock_uom)}`);
		}
		if (detail.available_qty !== undefined && detail.warehouse) {
			parts.push(`${__("库存")}: ${format_number(flt(detail.available_qty), null, 2)}`);
		}
		if (detail.has_bom === false) {
			parts.push(__("无默认BOM"));
		}
		$tr.find(".quick-row-help").html(parts.join(" · "));
	}

	function apply_item_defaults($tr, item_code) {
		if (!item_code) return;
		frappe.call({
			method: "process_simplification.api.quick_order.get_quick_order_item_defaults",
			args: { item_code },
		}).then((r) => {
			const detail = r.message || {};
			const row = get_row($tr);
			if (!row) return;
			if (!flt(row.rate.get_value()) && detail.rate) {
				row.rate.set_value(detail.rate);
			}
			if (!row.warehouse.get_value() && detail.warehouse) {
				row.warehouse.set_value(detail.warehouse);
			}
			if (!row.delivery_date.get_value()) {
				row.delivery_date.set_value(fields.delivery_date.get_value());
			}
			set_row_help($tr, detail);
		});
	}

	function add_row(row = {}) {
		const row_id = ++state.row_index;
		const $tr = row_template(row_id);
		$root.find("tbody").append($tr);
		const controls = {
			item_code: make_cell_control($tr, "item_code", {
				fieldtype: "Link",
				options: "Item",
				placeholder: __("选择产品"),
				reqd: 1,
				only_select: true,
				get_query: product_query,
				change: () => apply_item_defaults($tr, controls.item_code.get_value()),
			}),
			qty: make_cell_control($tr, "qty", {
				fieldtype: "Float",
				placeholder: __("数量"),
				reqd: 1,
			}),
			rate: make_cell_control($tr, "rate", {
				fieldtype: "Currency",
				placeholder: __("单价"),
			}),
			delivery_date: make_cell_control($tr, "delivery_date", {
				fieldtype: "Date",
				reqd: 1,
			}),
			warehouse: make_cell_control($tr, "warehouse", {
				fieldtype: "Link",
				options: "Warehouse",
				placeholder: __("默认成品仓"),
				only_select: true,
				get_query: warehouse_query,
			}),
			remarks: make_cell_control($tr, "remarks", {
				fieldtype: "Data",
				placeholder: __("可选"),
			}),
		};
		state.rows.set(row_id, controls);
		Object.values(controls).forEach((control) => {
			control.$wrapper.find(".tooltip-content").remove();
		});
		controls.delivery_date.set_value(row.delivery_date || fields.delivery_date.get_value());
		controls.qty.set_value(row.qty || "");
		controls.rate.set_value(row.rate || "");
		controls.item_code.set_value(row.item_code || "");
		controls.warehouse.set_value(row.warehouse || state.defaults.fg_warehouse || "");
		controls.remarks.set_value(row.remarks || "");
	}

	function collect_items() {
		return $root.find("tbody tr").toArray().map((tr) => {
			const row = get_row($(tr));
			return {
				item_code: row.item_code.get_value(),
				qty: flt(row.qty.get_value()),
				rate: flt(row.rate.get_value()),
				delivery_date: row.delivery_date.get_value() || fields.delivery_date.get_value(),
				warehouse: row.warehouse.get_value(),
				remarks: row.remarks.get_value(),
			};
		});
	}

	function show_validation(message) {
		frappe.msgprint({ title: __("快速开单"), message, indicator: "red" });
	}

	function validate_before_submit(payload) {
		if (!payload.customer) {
			show_validation(__("请选择客户。"));
			return false;
		}
		if (!payload.delivery_date) {
			show_validation(__("请选择默认交付日期。"));
			return false;
		}
		const rows = payload.items.filter((row) => row.item_code || row.qty || row.rate || row.warehouse);
		if (!rows.length) {
			show_validation(__("请至少添加一个产品。"));
			return false;
		}
		for (let i = 0; i < rows.length; i++) {
			const row = rows[i];
			if (!row.item_code) {
				show_validation(__("第 {0} 行请选择产品。", [i + 1]));
				return false;
			}
			if (flt(row.qty) <= 0) {
				show_validation(__("第 {0} 行数量必须大于 0。", [i + 1]));
				return false;
			}
			if (!row.delivery_date) {
				show_validation(__("第 {0} 行请选择交付日期。", [i + 1]));
				return false;
			}
		}
		payload.items = rows;
		return true;
	}

	function submit_order() {
		const payload = {
			confirmed: true,
			customer: fields.customer.get_value(),
			delivery_date: fields.delivery_date.get_value(),
			allow_partial_delivery: cint(fields.allow_partial_delivery.get_value()),
			remarks: fields.remarks.get_value(),
			items: collect_items(),
		};
		if (!validate_before_submit(payload)) return;

		frappe.confirm(__("确认创建并提交销售订单？"), () => {
			frappe.call({
				method: "process_simplification.api.quick_order.create_quick_sales_order",
				args: { payload },
				freeze: true,
				freeze_message: __("正在创建销售订单..."),
			}).then((r) => {
				if (r.message && r.message.route) {
					frappe.show_alert({ message: __("销售订单已创建"), indicator: "green" });
					frappe.set_route(r.message.route);
				}
			});
		});
	}

	function load_context() {
		frappe.call({
			method: "process_simplification.api.quick_order.get_quick_order_context",
		}).then((r) => {
			state.defaults = r.message || {};
			fields.delivery_date.set_value(state.defaults.default_delivery_date || frappe.datetime.add_days(frappe.datetime.nowdate(), 7));
			add_row({});
		});
	}

	$root.on("click", ".add-row", () => add_row({}));
	$root.on("click", ".remove-row", (event) => {
		const $tr = $(event.currentTarget).closest("tr");
		state.rows.delete(cint($tr.data("row-id")));
		$tr.remove();
		if (!$root.find("tbody tr").length) {
			add_row({});
		}
	});

	page.set_primary_action(__("创建并提交"), submit_order);
	load_context();
};
