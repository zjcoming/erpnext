const test = require("node:test");
const assert = require("node:assert/strict");

global.frappe = { pages: { "order-workbench": {} } };
global.__ = (message) => message;

const {
	filterFulfillmentOrders,
	overviewSummary,
	orderOverviewHtml,
	fulfillmentCsv,
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
		active_work_order_qty: 2,
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
				order_qty: 10,
				delivered_qty: 2,
				pending_qty: 8,
				reserved_qty: 3,
				active_work_order_qty: 2,
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

test("filters by delivery window and fulfillment status", () => {
	assert.deepEqual(filterFulfillmentOrders(fixture, { deliveryWindow: "overdue" }).map((row) => row.name), ["SO-OVERDUE"]);
	assert.deepEqual(filterFulfillmentOrders(fixture, { status: "needs_production" }).map((row) => row.name), ["SO-PRODUCTION"]);
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

test("expanded product rows include completed quantity", () => {
	const html = orderOverviewHtml(order("SO-COMPLETED"), helpers);

	assert.match(html, /已完工/);
	assert.match(html, /1\.00/);
});

test("CSV uses Chinese headers and filename", () => {
	const csv = fulfillmentCsv([order("SO-CSV")]);

	assert.equal(csv.filename, "订单履约总览.csv");
	assert.match(csv.content, /^\uFEFF"销售订单","客户","最早交期","订购","已发","待交","已预留","生产中","已完工","未覆盖","风险"/);
	assert.match(csv.content, /"SO-CSV"/);
});
