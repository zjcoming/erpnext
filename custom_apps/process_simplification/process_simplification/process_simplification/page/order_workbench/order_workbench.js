function isDueWithin7Days(order) {
	return ["today", "within_7_days"].includes(order.delivery_timing);
}

function filterFulfillmentOrders(orders, filters = {}) {
	const search = String(filters.search || "").trim().toLowerCase();
	return (orders || []).filter((order) => {
		const searchable = [
			order.name,
			order.customer,
			order.customer_name,
			...(order.rows || []).flatMap((row) => [row.item_code, row.item_name]),
		]
			.join(" ")
			.toLowerCase();
		return (
			(!search || searchable.includes(search)) &&
			(!filters.customer || order.customer === filters.customer) &&
			(!filters.deliveryWindow ||
				(filters.deliveryWindow === "within_7_days"
					? isDueWithin7Days(order)
					: order.delivery_timing === filters.deliveryWindow)) &&
			(!filters.status || order.status_code === filters.status) &&
			(!filters.riskOnly || ["red", "orange"].includes(order.risk_level))
		);
	});
}

function overviewSummary(orders) {
	return (orders || []).reduce(
		(summary, order) => {
			summary.total_orders += 1;
			summary.overdue_orders += Number(order.delivery_timing === "overdue");
			summary.due_within_7_days += Number(isDueWithin7Days(order));
			summary.needs_production_orders += Number(Boolean(order.needs_production));
			summary.direct_ship_orders += Number(Boolean(order.direct_ship));
			return summary;
		},
		{
			total_orders: 0,
			overdue_orders: 0,
			due_within_7_days: 0,
			needs_production_orders: 0,
			direct_ship_orders: 0,
		}
	);
}

function productionWorkbenchRoute(_salesOrder, salesOrderItem) {
	return ["production-workbench", salesOrderItem];
}

function deliveryNoteRouteFromResponse(response) {
	const deliveryNote = response?.message?.delivery_note;
	return deliveryNote ? ["Form", "Delivery Note", deliveryNote] : null;
}

function fulfillmentStatusColor(statusCode) {
	return {
		ready_to_ship: "green",
		needs_production: "blue",
		awaiting_stock: "orange",
		awaiting_fulfillment: "gray",
	}[statusCode] || "gray";
}

function fulfillmentCsv(orders) {
	const quote = (value) => {
		const text = String(value ?? "");
		const safeText = /^[=+\-@\t\r]/.test(text) ? `'${text}` : text;
		return `"${safeText.replaceAll('"', '""')}"`;
	};
	const lines = [
		["销售订单", "客户", "最早交期", "订购", "已发", "待交", "有效预留", "成品覆盖", "需生产", "已安排", "未安排", "风险"],
		...(orders || []).map((order) => [
			order.name,
			order.customer_name || order.customer,
			order.delivery_date,
			order.order_qty,
			order.delivered_qty,
			order.pending_qty,
			order.reserved_qty,
			order.finished_stock_coverage_qty,
			order.production_required_qty,
			order.active_work_order_qty,
			order.unplanned_production_qty ?? order.uncovered_qty,
			order.risk_label,
		]),
	].map((row) => row.map(quote).join(","));
	return { filename: "订单履约总览.csv", content: "\uFEFF" + lines.join("\r\n") };
}

function orderOverviewHtml(order, helpers) {
	const t = helpers.translate;
	const esc = helpers.escapeHtml;
	const number = helpers.formatNumber;
	const date = helpers.formatDate;
	const actions = (row) =>
		(row.next_actions || [])
			.map(
				(action) =>
					`<button class="btn btn-xs btn-default row-action" data-action="${esc(action.action)}" data-sales-order="${esc(order.name)}" data-row="${esc(row.sales_order_item)}" ${action.enabled ? "" : "disabled"}>${esc(t(action.label))}</button>`
			)
			.join(" ");
	const itemRows = (order.rows || [])
		.map(
			(row) => `
				<tr class="${row.unsupported ? "text-muted" : ""}">
					<td data-label="${esc(t("产品"))}"><a href="/app/item/${encodeURIComponent(row.item_code || "")}">${esc(row.item_code || "")}</a><br><small>${esc(row.item_name || "")}</small></td>
					<td data-label="${esc(t("交期"))}">${esc(date(row.delivery_date)) || esc(t("未设置"))}</td>
					<td class="fulfillment-number" data-label="${esc(t("待交"))}">${number(row.pending_qty)}</td>
					<td class="fulfillment-number" data-label="${esc(t("有效预留"))}">${number(row.reserved_qty)}</td>
					<td class="fulfillment-number" data-label="${esc(t("可用成品"))}">${number(row.available_to_reserve)}</td>
					<td class="fulfillment-number" data-label="${esc(t("成品覆盖"))}">${number(row.finished_stock_coverage_qty)}</td>
					<td class="fulfillment-number" data-label="${esc(t("需生产"))}">${number(row.production_required_qty)}</td>
					<td class="fulfillment-number" data-label="${esc(t("已安排"))}">${number(row.active_work_order_qty)}</td>
					<td class="fulfillment-number" data-label="${esc(t("未安排"))}">${number(row.unplanned_production_qty ?? row.uncovered_qty)}</td>
					<td data-label="${esc(t("状态"))}"><span class="indicator-pill gray">${esc(row.status || "")}</span>${row.unsupported_reason ? `<br><small>${esc(row.unsupported_reason)}</small>` : ""}</td>
					<td class="fulfillment-item-actions" data-label="${esc(t("下一步"))}">${actions(row)}</td>
				</tr>`
		)
		.join("");
	const statusLabel = order.status_label || "";
	const riskLabel = order.risk_label || "";
	const statusPill = statusLabel
		? `<span class="indicator-pill ${esc(fulfillmentStatusColor(order.status_code))} fulfillment-order-status">${esc(statusLabel)}</span>`
		: "";
	const riskPill = riskLabel && riskLabel !== statusLabel
		? `<span class="indicator-pill ${esc(order.risk_level || "gray")} fulfillment-order-risk-pill">${esc(riskLabel)}</span>`
		: "";
	return `
		<details class="fulfillment-order fulfillment-risk-${esc(order.risk_level || "gray")}" data-sales-order="${esc(order.name)}">
			<summary>
				<div class="fulfillment-order-primary">
					<strong>${esc(order.name)}</strong>
					<span>${esc(order.customer_name || order.customer || "")}</span>
				</div>
				<div class="fulfillment-order-fact"><span>${esc(t("最早交期"))}</span><strong>${esc(date(order.delivery_date)) || esc(t("未设置"))}</strong>${order.has_multiple_delivery_dates ? ` <span class="indicator-pill gray">${esc(t("多交期"))}</span>` : ""}</div>
				<div class="fulfillment-order-fact fulfillment-number"><span>${esc(t("已发 / 订购"))}</span><strong>${number(order.delivered_qty)} / ${number(order.order_qty)}</strong></div>
				<div class="fulfillment-order-fact fulfillment-number"><span>${esc(t("成品覆盖 / 待交"))}</span><strong>${number(order.finished_stock_coverage_qty ?? order.reserved_qty)} / ${number(order.pending_qty)}</strong></div>
				<div class="fulfillment-order-fact fulfillment-number"><span>${esc(t("已安排 / 未安排"))}</span><strong>${number(order.active_work_order_qty)} / ${number(order.unplanned_production_qty ?? order.uncovered_qty)}</strong></div>
				<div class="fulfillment-order-risk">${statusPill}${statusPill && riskPill ? " " : ""}${riskPill}</div>
				<span class="fulfillment-toggle">${esc(t("查看并处理"))}</span>
			</summary>
			<div class="fulfillment-order-details">
				<div class="fulfillment-order-actions" aria-label="${esc(t("订单操作"))}">
					<strong class="fulfillment-order-actions-title">${esc(t("订单操作"))}</strong>
					<button class="btn btn-default btn-sm row-action" data-action="view_sales_order" data-sales-order="${esc(order.name)}">${esc(t("查看销售订单"))}</button>
				</div>
				<div class="fulfillment-item-table-wrap">
					<table class="table table-bordered fulfillment-item-table">
						<thead><tr><th>${esc(t("产品"))}</th><th>${esc(t("交期"))}</th><th>${esc(t("待交"))}</th><th>${esc(t("有效预留"))}</th><th>${esc(t("可用成品"))}</th><th>${esc(t("成品覆盖"))}</th><th>${esc(t("需生产"))}</th><th>${esc(t("已安排"))}</th><th>${esc(t("未安排"))}</th><th>${esc(t("状态"))}</th><th>${esc(t("下一步"))}</th></tr></thead>
						<tbody>${itemRows}</tbody>
					</table>
				</div>
			</div>
		</details>`;
}

function refreshFulfillmentOverview(page, salesOrder) {
	if (!page || !page.fulfillment_overview) return;
	const { state, loadOverview } = page.fulfillment_overview;
	state.filters.search = salesOrder || "";
	state.expandedOrders.clear();
	if (salesOrder) state.expandedOrders.add(salesOrder);
	return loadOverview();
}

const fulfillmentOverviewApi = {
	filterFulfillmentOrders,
	overviewSummary,
	fulfillmentCsv,
	orderOverviewHtml,
	refreshFulfillmentOverview,
	productionWorkbenchRoute,
	deliveryNoteRouteFromResponse,
};

if (typeof module !== "undefined" && module.exports) {
	module.exports = fulfillmentOverviewApi;
}

if (typeof frappe !== "undefined") {
	frappe.pages["order-workbench"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("订单工作台"),
			single_column: true,
		});
		page.main.html(`
			<div class="process-simplification-page order-workbench fulfillment-overview">
				<div class="fulfillment-kpis"></div>
				<div class="fulfillment-filter-bar">
					<input class="form-control fulfillment-search" data-filter="search" placeholder="${__("搜索销售订单、客户或产品")}">
					<select class="form-control" data-filter="deliveryWindow">
						<option value="">${__("全部交期")}</option>
						<option value="overdue">${__("已逾期")}</option>
						<option value="today">${__("今日交期")}</option>
						<option value="within_7_days">${__("7 天内交期")}</option>
						<option value="later">${__("稍后交期")}</option>
						<option value="missing">${__("未设置交期")}</option>
					</select>
					<select class="form-control" data-filter="status">
						<option value="">${__("全部状态")}</option>
						<option value="ready_to_ship">${__("可发货")}</option>
						<option value="needs_production">${__("需生产")}</option>
						<option value="awaiting_stock">${__("待预留")}</option>
						<option value="awaiting_fulfillment">${__("待处理")}</option>
					</select>
					<select class="form-control" data-filter="customer">
						<option value="">${__("全部客户")}</option>
					</select>
					<label class="fulfillment-risk-filter"><input type="checkbox" data-filter="riskOnly"> ${__("仅看风险")}</label>
					<button class="btn btn-default fulfillment-export">${__("导出当前可见 CSV")}</button>
				</div>
				<p class="text-muted fulfillment-sort-note">${__("默认排序：最早交期、最高风险、创建时间。")}</p>
				<div class="fulfillment-order-list"></div>
			</div>
		`);

		const $root = page.main.find(".fulfillment-overview");
		const state = { data: { orders: [] }, filters: {}, expandedOrders: new Set(), inFlightActions: new Set() };
		page.fulfillment_overview = { state, loadOverview };

		function browserHelpers() {
			return {
				translate: __,
				escapeHtml: frappe.utils.escape_html,
				formatNumber: (value) => format_number(flt(value), null, 2),
				formatDate: (value) => (value ? frappe.datetime.str_to_user(value) : ""),
			};
		}

		function renderKpis(summary) {
			const cards = [
				[__("当前订单"), summary.total_orders, "gray"],
				[__("已逾期"), summary.overdue_orders, "red"],
				[__("7 天内交期"), summary.due_within_7_days, "orange"],
				[__("需生产"), summary.needs_production_orders, "blue"],
				[__("可发货"), summary.direct_ship_orders, "green"],
			];
			$root.find(".fulfillment-kpis").html(
				cards.map(([label, value, color]) => `<div class="fulfillment-kpi fulfillment-kpi-${color}"><span>${frappe.utils.escape_html(label)}</span><strong>${value}</strong></div>`).join("")
			);
		}

		function visibleOrders() {
			return filterFulfillmentOrders(state.data.orders || [], state.filters);
		}

		function render() {
			const orders = visibleOrders();
			renderKpis(overviewSummary(orders));
			$root.find(".fulfillment-search").val(state.filters.search || "");
			$root.find('[data-filter="deliveryWindow"]').val(state.filters.deliveryWindow || "");
			$root.find('[data-filter="status"]').val(state.filters.status || "");
			$root.find('[data-filter="customer"]').val(state.filters.customer || "");
			$root.find('[data-filter="riskOnly"]').prop("checked", Boolean(state.filters.riskOnly));
			const html = orders.length
				? orders.map((order) => orderOverviewHtml(order, browserHelpers())).join("")
				: `<div class="text-muted fulfillment-empty">${frappe.utils.escape_html(__("没有符合当前筛选条件的订单。"))}</div>`;
			$root.find(".fulfillment-order-list").html(html);
			$root.find(".fulfillment-order").each((_, element) => {
				const $order = $(element);
				$order.prop("open", state.expandedOrders.has($order.data("sales-order")));
				$order.on("toggle", () => {
					const name = $order.data("sales-order");
					if ($order.prop("open")) state.expandedOrders.add(name);
					else state.expandedOrders.delete(name);
				});
			});
		}

		function loadOverview() {
			return frappe.call({
				method: "process_simplification.api.workbench.get_fulfillment_overview",
				freeze: true,
				freeze_message: __("正在读取订单履约总览..."),
			}).then((response) => {
				state.data = response.message || { orders: [] };
				const customers = [...new Map(
					(state.data.orders || []).map((order) => [order.customer, order.customer_name || order.customer])
				).entries()]
					.filter(([customer]) => customer)
					.sort((left, right) => left[1].localeCompare(right[1]));
				$root.find('[data-filter="customer"]').html(
					[`<option value="">${frappe.utils.escape_html(__("全部客户"))}</option>`]
						.concat(
							customers.map(
								([customer, label]) => `<option value="${frappe.utils.escape_html(customer)}">${frappe.utils.escape_html(label)}</option>`
							)
						)
						.join("")
				);
				render();
			});
		}

		function runRowAction(action, salesOrder, salesOrderItem, $button) {
			const methodMap = {
				reserve_stock: "process_simplification.api.actions.reserve_stock",
				reserve_completed_stock: "process_simplification.api.actions.reserve_completed_stock",
				create_delivery_note: "process_simplification.api.actions.create_delivery_note",
			};
			if (action === "view_sales_order") {
				frappe.set_route("Form", "Sales Order", salesOrder);
				return;
			}
			if (action === "open_production_workbench") {
				frappe.set_route(...productionWorkbenchRoute(salesOrder, salesOrderItem));
				return;
			}
			const method = methodMap[action];
			if (!method) return;
			const actionKey = `${action}:${salesOrder}:${salesOrderItem || ""}`;
			if (state.inFlightActions.has(actionKey)) return;
			const finish = () => {
				state.inFlightActions.delete(actionKey);
				$button?.prop("disabled", false);
			};
			state.inFlightActions.add(actionKey);
			$button?.prop("disabled", true);
			frappe.confirm(
				__("确认执行该操作？"),
				() => {
					frappe.call({
						method,
						args: { sales_order: salesOrder, sales_order_item: salesOrderItem },
						freeze: true,
						callback: (response) => {
							if (action === "create_delivery_note") {
								const route = deliveryNoteRouteFromResponse(response);
								if (route) {
									frappe.show_alert({
										message: response.message.reused ? __("已存在草稿发货单，正在打开") : __("发货单已创建，正在打开"),
										indicator: "green",
									});
									frappe.set_route(...route);
									return;
								}
							}
							frappe.show_alert({ message: __("操作完成"), indicator: "green" });
							loadOverview();
						},
						always: finish,
					});
				},
				finish
			);
		}

		function exportVisibleOrders() {
			const csv = fulfillmentCsv(visibleOrders());
			const link = document.createElement("a");
			const url = URL.createObjectURL(new Blob([csv.content], { type: "text/csv;charset=utf-8" }));
			link.href = url;
			link.download = csv.filename;
			link.click();
			setTimeout(() => URL.revokeObjectURL(url), 0);
		}

		$root.on("input change", "[data-filter]", (event) => {
			const $input = $(event.currentTarget);
			state.filters[$input.data("filter")] = $input.is(":checkbox") ? $input.prop("checked") : $input.val();
			render();
		});
		$root.on("click", ".row-action", (event) => {
			const $button = $(event.currentTarget);
			runRowAction($button.data("action"), $button.data("sales-order"), $button.data("row"), $button);
		});
		$root.on("click", ".fulfillment-export", exportVisibleOrders);
		page.add_inner_button(__("刷新"), loadOverview);

	};

	frappe.pages["order-workbench"].refresh = function (wrapper) {
		const page = wrapper.page;
		const routeSalesOrder = frappe.get_route()[1] || null;
		return refreshFulfillmentOverview(page, routeSalesOrder);
	};
}
