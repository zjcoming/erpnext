const test = require("node:test");
const assert = require("node:assert/strict");

global.frappe = { pages: { "order-workbench": {} } };
global.__ = (message) => message;

const {
	filterFulfillmentOrders,
	overviewSummary,
	orderOverviewHtml,
	fulfillmentCsv,
	workbenchPaginationHtml,
	refreshFulfillmentOverview,
	productionWorkbenchRoute,
	deliveryNoteRouteFromResponse,
} = require("../../process_simplification/page/order_workbench/order_workbench.js");

const escapeHtml = (value) =>
	String(value ?? "")
		.replaceAll("&", "&amp;")
		.replaceAll("<", "&lt;")
		.replaceAll(">", "&gt;")
		.replaceAll('"', "&quot;")
		.replaceAll("'", "&#39;");

const helpers = {
	translate: (message) => message,
	escapeHtml,
	formatNumber: (value) => Number(value || 0).toFixed(2),
	formatDate: (value) => value || "",
};

function order(name, overrides = {}) {
	return {
		name,
		customer: `Customer ${name}`,
		customer_name: `Customer ${name}`,
		delivery_date: "2026-08-08",
		order_qty: 10,
		delivered_qty: 2,
		pending_qty: 8,
		reserved_qty: 3,
		available_to_reserve: 1,
		finished_stock_coverage_qty: 4,
		production_required_qty: 4,
		active_work_order_qty: 2,
		unplanned_production_qty: 2,
		completed_qty: 1,
		uncovered_qty: 3,
		delivery_timing: "later",
		status_code: "awaiting_stock",
		status_label: "Awaiting stock",
		risk_level: "orange",
		risk_label: "Needs attention",
		direct_ship: false,
		needs_production: false,
		rows: [
			{
				sales_order_item: `${name}-ITEM-1`,
				item_code: `FG-${name}`,
				item_name: `Finished ${name}`,
				delivery_date: "2026-08-08",
				order_qty: 10,
				delivered_qty: 2,
				pending_qty: 8,
				reserved_qty: 3,
				available_to_reserve: 1,
				finished_stock_coverage_qty: 4,
				production_required_qty: 4,
				active_work_order_qty: 2,
				unplanned_production_qty: 2,
				completed_qty: 1,
				uncovered_qty: 3,
				material_status: "Ready",
				status: "Awaiting stock",
				next_actions: [],
			},
		],
		...overrides,
	};
}

const fixture = [
	order("SO-OVERDUE", { delivery_date: "2026-08-01", delivery_timing: "overdue", risk_level: "red", risk_label: "Overdue" }),
	order("SO-BLOCKED", { status_code: "awaiting_stock", risk_level: "orange", risk_label: "Awaiting stock" }),
	order("SO-PRODUCTION", { customer_name: "Acme Works", status_code: "needs_production", needs_production: true, risk_level: "blue" }),
	order("SO-READY", { customer_name: "Bright Co", status_code: "ready_to_ship", direct_ship: true, risk_level: "green" }),
];

test("risk-only filtering keeps overdue and blocked orders", () => {
	const visible = filterFulfillmentOrders(fixture, { riskOnly: true });

	assert.deepEqual(visible.map((row) => row.name), ["SO-OVERDUE", "SO-BLOCKED"]);
});

test("filters by order, customer, and product search", () => {
	assert.deepEqual(filterFulfillmentOrders(fixture, { search: "production" }).map((row) => row.name), ["SO-PRODUCTION"]);
	assert.deepEqual(filterFulfillmentOrders(fixture, { search: "bright" }).map((row) => row.name), ["SO-READY"]);
	assert.deepEqual(filterFulfillmentOrders(fixture, { search: "fg-so-blocked" }).map((row) => row.name), ["SO-BLOCKED"]);
});

test("dedicated customer filter matches the customer identity", () => {
	const customers = [
		order("SO-CUSTOMER-1", { customer: "CUST-001", customer_name: "Shared Name" }),
		order("SO-CUSTOMER-2", { customer: "CUST-002", customer_name: "Shared Name" }),
	];

	assert.deepEqual(filterFulfillmentOrders(customers, { customer: "CUST-002" }).map((row) => row.name), [
		"SO-CUSTOMER-2",
	]);
});

test("filters by delivery window and fulfillment status", () => {
	assert.deepEqual(filterFulfillmentOrders(fixture, { deliveryWindow: "overdue" }).map((row) => row.name), ["SO-OVERDUE"]);
	assert.deepEqual(filterFulfillmentOrders(fixture, { status: "needs_production" }).map((row) => row.name), ["SO-PRODUCTION"]);
});

test("7 day delivery filter uses the KPI predicate and includes today", () => {
	const deliveries = [
		order("SO-TODAY", { delivery_timing: "today" }),
		order("SO-WITHIN-7", { delivery_timing: "within_7_days" }),
		order("SO-LATER", { delivery_timing: "later" }),
	];
	const visible = filterFulfillmentOrders(deliveries, { deliveryWindow: "within_7_days" });

	assert.deepEqual(visible.map((row) => row.name), ["SO-TODAY", "SO-WITHIN-7"]);
	assert.equal(overviewSummary(visible).due_within_7_days, 2);
});

test("visible counters recalculate from filtered orders", () => {
	assert.deepEqual(overviewSummary(filterFulfillmentOrders(fixture, { status: "ready_to_ship" })), {
		total_orders: 1,
		overdue_orders: 0,
		due_within_7_days: 0,
		needs_production_orders: 0,
		direct_ship_orders: 1,
	});
});

test("order HTML escapes customer and item labels", () => {
	const unsafeOrder = order("SO-UNSAFE", {
		customer_name: '<img src=x onerror="alert(1)">',
		rows: [
			{
				...order("SO-UNSAFE").rows[0],
				item_code: '<img src=x onerror="alert(1)">',
				item_name: '<img src=x onerror="alert(1)">',
			},
		],
	});
	const html = orderOverviewHtml(unsafeOrder, helpers);

	assert.doesNotMatch(html, /<img/);
	assert.match(html, /&lt;img/);
});

test("expanded product rows explain stock coverage and production demand", () => {
	const html = orderOverviewHtml(order("SO-COMPLETED"), helpers);

	for (const label of ["成品覆盖", "需生产", "已安排", "未安排"]) {
		assert.match(html, new RegExp(label));
	}
});

test("expanded product rows link their Production Plans and priority dates", () => {
	const plannedOrder = order("SO-PLANNED");
	plannedOrder.rows[0].production_plans = [
		{
			name: "PP-001",
			planned_date: "2026-08-20 08:00:00",
			work_order_count: 2,
			summary: { ready_work_order_count: 1 },
		},
	];
	const html = orderOverviewHtml(plannedOrder, helpers);

	assert.match(html, /\/app\/production-plan\/PP-001/);
	assert.match(html, /计划优先日期/);
	assert.match(html, /2 个工单/);
});

test("order HTML shows the multiple delivery dates badge", () => {
	const html = orderOverviewHtml(order("SO-MULTI", { has_multiple_delivery_dates: true }), helpers);

	assert.match(html, /多交期/);
});

test("collapsed order shows ready-to-ship status alongside a higher delivery risk", () => {
	const html = orderOverviewHtml(
		order("SO-READY-OVERDUE", {
			status_code: "ready_to_ship",
			status_label: "可发货",
			direct_ship: true,
			risk_level: "red",
			risk_label: "已逾期",
		}),
		helpers
	);

	assert.match(html, /class="indicator-pill green fulfillment-order-status">可发货<\/span>/);
	assert.match(html, /class="indicator-pill red fulfillment-order-risk-pill">已逾期<\/span>/);
});

test("expanded product rows show their own delivery date", () => {
	const itemDateOrder = order("SO-ITEM-DATE");
	itemDateOrder.rows[0].delivery_date = "2026-08-09";
	const html = orderOverviewHtml(itemDateOrder, helpers);

	assert.match(html, /2026-08-09/);
});

test("expanded product rows expose labels for the mobile card layout", () => {
	const html = orderOverviewHtml(order("SO-MOBILE"), helpers);

	for (const label of [
		"产品",
		"交期",
		"待交",
		"有效预留",
		"可用成品",
		"成品覆盖",
		"需生产",
		"已安排",
		"未安排",
		"状态",
		"下一步",
	]) {
		assert.match(html, new RegExp(`data-label="${label}"`));
	}
	assert.match(html, /class="fulfillment-order-actions" aria-label="订单操作"/);
	assert.match(html, /class="fulfillment-order-actions-title">订单操作</);
});

test("production actions route to the production workbench by Sales Order Item", () => {
	assert.deepEqual(productionWorkbenchRoute("SO-001", "SOI-001"), ["production-workbench", "SOI-001"]);
});

test("delivery note creation response resolves the created document route", () => {
	assert.equal(typeof deliveryNoteRouteFromResponse, "function");
	assert.deepEqual(deliveryNoteRouteFromResponse({ message: { delivery_note: "DN-001" } }), [
		"Form",
		"Delivery Note",
		"DN-001",
	]);
	assert.equal(deliveryNoteRouteFromResponse({ message: {} }), null);
});

test("CSV uses Chinese headers and filename", () => {
	const csv = fulfillmentCsv([order("SO-CSV")]);

	assert.equal(csv.filename, "订单履约总览.csv");
	assert.match(csv.content, /^\uFEFF"销售订单","客户","最早交期","订购","已发","待交","有效预留","成品覆盖","需生产","已安排","未安排","风险"/);
	assert.match(csv.content, /"SO-CSV"/);
});

test("CSV neutralizes every formula-leading cell before quoting", () => {
	const csv = fulfillmentCsv([
		order("=1+1", {
			customer_name: "+SUM(A1:A2)",
			delivery_date: "-1+1",
			order_qty: "@cmd",
			delivered_qty: "\tformula",
			pending_qty: "\rformula",
		}),
	]);

	for (const cell of ["'=1+1", "'+SUM(A1:A2)", "'-1+1", "'@cmd", "'\tformula", "'\rformula"]) {
		assert.ok(csv.content.includes(`"${cell}"`), `expected neutralized CSV cell ${JSON.stringify(cell)}`);
	}
});

test("pagination HTML exposes compact page controls for desktop and mobile", () => {
	const html = workbenchPaginationHtml(
		{ page: 2, page_size: 20, total_count: 45, total_pages: 3, has_next: true, has_prev: true },
		helpers
	);

	assert.match(html, /\u7b2c 2 \/ 3 \u9875/);
	assert.match(html, /\u5171 45 \u6761/);
	assert.match(html, /data-page="1"/);
	assert.match(html, /data-page="3"/);
	assert.match(html, /data-page-size="20" selected/);
});

test("every page refresh clears or sets route focus and reloads once", async () => {
	const loads = [];
	const state = { filters: { search: "SO-OLD" }, expandedOrders: new Set(["SO-OLD"]) };
	const page = {
		fulfillment_overview: {
			state,
			loadOverview: () => {
				loads.push("load");
				return Promise.resolve();
			},
		},
	};

	await refreshFulfillmentOverview(page, "SO-NEW");
	assert.equal(state.filters.search, "SO-NEW");
	assert.deepEqual([...state.expandedOrders], ["SO-NEW"]);
	assert.equal(loads.length, 1);

	await refreshFulfillmentOverview(page, null);
	assert.equal(state.filters.search, "");
	assert.deepEqual([...state.expandedOrders], []);
	assert.equal(loads.length, 2);
});
