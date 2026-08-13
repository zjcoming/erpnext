const test = require("node:test");
const assert = require("node:assert/strict");

global.frappe = {
	pages: { "production-workbench": {} },
	router: { slug: (value) => String(value).toLowerCase().replaceAll(" ", "-") },
	datetime: { str_to_user: (value) => value },
};
global.__ = (message) => message;

const productionWorkbench = require("../../process_simplification/page/production_workbench/production_workbench.js");

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

function demand(key, overrides = {}) {
	return {
		demand_key: key,
		sales_order: `SO-${key}`,
		sales_order_item: key,
		customer: `CUST-${key}`,
		customer_name: `客户 ${key}`,
		item_code: `FG-${key}`,
		item_name: `成品 ${key}`,
		delivery_date: "2026-08-08",
		delivery_timing: "within_7_days",
		pending_qty: 100,
		reserved_qty: 20,
		available_to_reserve: 30,
		finished_stock_coverage_qty: 50,
		production_required_qty: 50,
		active_work_order_qty: 40,
		unplanned_production_qty: 10,
		overplanned_qty: 0,
		completed_qty: 5,
		completed_unreserved_qty: 5,
		status_code: "unplanned",
		status_label: "待安排",
		risk_level: "orange",
		risk_label: "临近交期",
		material_summary: {
			status_code: "shortage",
			material_count: 1,
			shortage_item_count: 1,
			blocked_item_count: 0,
			awaiting_supply_item_count: 0,
		},
		materials: [
			{
				item_code: "RM-001",
				item_name: "原料 001",
				stock_uom: "Nos",
				warehouse: "Stores - TC",
				required_qty: 30,
				allocated_qty: 3,
				intransit_qty: 0,
				shortage_qty: 27,
				status: "new_purchase_required",
			},
		],
		work_orders: [
			{
				name: "WO-001",
				status: "Not Started",
				qty: 40,
				produced_qty: 5,
				bom_no: "BOM-FG-001",
				source_warehouse: "Stores - TC",
				wip_warehouse: "WIP - TC",
				fg_warehouse: "Finished Goods - TC",
			},
		],
		next_actions: [
			{ label: "创建生产任务", action: "create_work_order", enabled: true },
			{ label: "处理缺料", action: "handle_shortage", enabled: true },
		],
		...overrides,
	};
}

const fixture = [
	demand("OVERDUE", { delivery_timing: "overdue", risk_level: "red", risk_label: "已逾期" }),
	demand("ACTIVE", {
		status_code: "in_production",
		status_label: "生产中",
		unplanned_production_qty: 0,
		material_summary: { status_code: "ready", material_count: 1, shortage_item_count: 0 },
	}),
	demand("HANDOFF", {
		status_code: "awaiting_order_reservation",
		status_label: "待回补订单",
		unplanned_production_qty: 0,
		production_required_qty: 0,
	}),
];

test("filters production demand by search, status, shortage, and unplanned state", () => {
	assert.deepEqual(
		productionWorkbench.filterProductionDemands(fixture, { search: "active" }).map((row) => row.demand_key),
		["ACTIVE"]
	);
	assert.deepEqual(
		productionWorkbench.filterProductionDemands(fixture, { status: "awaiting_order_reservation" }).map((row) => row.demand_key),
		["HANDOFF"]
	);
	assert.deepEqual(
		productionWorkbench.filterProductionDemands(fixture, { shortageOnly: true }).map((row) => row.demand_key),
		["OVERDUE", "HANDOFF"]
	);
	assert.deepEqual(
		productionWorkbench.filterProductionDemands(fixture, { unplannedOnly: true }).map((row) => row.demand_key),
		["OVERDUE"]
	);
});

test("visible production counters recalculate from filtered demands", () => {
	assert.deepEqual(productionWorkbench.productionSummary([fixture[1]]), {
		total_demands: 1,
		unplanned_demands: 0,
		overdue_demands: 0,
		due_within_7_days: 1,
		material_shortage_demands: 0,
		in_production_demands: 1,
		awaiting_order_reservation_demands: 0,
	});
});

test("production demand HTML escapes server values and exposes complete labelled details", () => {
	const unsafe = demand("UNSAFE", {
		customer_name: '<img src=x onerror="alert(1)">',
		item_name: '<img src=x onerror="alert(1)">',
	});
	const html = productionWorkbench.productionDemandHtml(unsafe, helpers);

	assert.doesNotMatch(html, /<img/);
	assert.match(html, /&lt;img/);
	for (const label of [
		"订单待交",
		"有效预留",
		"可用成品",
		"成品覆盖",
		"需要生产",
		"工单覆盖",
		"未安排",
		"已完工",
		"待回补",
	]) {
		assert.match(html, new RegExp(`data-label="${label}"`));
	}
	for (const materialLabel of [
		"本单需求",
		"已分配库存",
		"在途\\(本单\\)",
		"采购缺口",
	]) {
		assert.match(html, new RegExp(`data-label="${materialLabel}"`));
	}
	assert.match(html, /需新采购/);
	assert.doesNotMatch(html, />new_purchase_required</);
	assert.match(html, /\/app\/work-order\/WO-001/);
});

test("material rows show linked purchase documents and status, or a no-purchase hint", () => {
	const withDocs = demand("DOCS", {
		materials: [
			{
				item_code: "RM-DOC",
				item_name: "原料 DOC",
				warehouse: "Stores - TC",
				required_qty: 15,
				allocated_qty: 0,
				intransit_qty: 15,
				shortage_qty: 0,
				status: "awaiting_purchase_receipt",
				supply_documents: [
					{ doctype: "Material Request", name: "MREQ-1", status: "Pending", outstanding_qty: 10, schedule_date: "2026-08-05", is_late: false },
					{ doctype: "Purchase Order", name: "PORD-1", status: "To Receive", outstanding_qty: 5, schedule_date: "2026-08-27", is_late: true },
				],
			},
		],
	});
	const html = productionWorkbench.productionDemandHtml(withDocs, helpers);
	assert.match(html, /\/app\/material-request\/MREQ-1/);
	assert.match(html, /\/app\/purchase-order\/PORD-1/);
	assert.match(html, /To Receive/);
	// The late Purchase Order is still shown and flagged.
	assert.match(html, /迟于交期/);

	// A material with no supply documents shows the no-purchase hint.
	const noDocs = demand("NODOC");
	const noDocHtml = productionWorkbench.productionDemandHtml(noDocs, helpers);
	assert.match(noDocHtml, /尚未发起采购/);
});

test("route focus expands the selected Sales Order Item and reloads once", async () => {
	const loads = [];
	const state = { filters: {}, expandedDemands: new Set() };
	const page = {
		production_workbench: {
			state,
			loadOverview: () => {
				loads.push("load");
				return Promise.resolve();
			},
		},
	};

	await productionWorkbench.refreshProductionOverview(page, "SOI-FOCUS");
	assert.equal(state.filters.search, "SOI-FOCUS");
	assert.deepEqual([...state.expandedDemands], ["SOI-FOCUS"]);
	assert.equal(loads.length, 1);
});
