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
		production_plans: [
			{
				name: `PP-${key}`,
				planned_date: "2026-08-07 08:00:00",
				summary: { ready_work_order_count: 0, waiting_subassembly_count: 0 },
			},
		],
		materials: [
			{
				item_code: "RM-001",
				item_name: "原料 001",
				stock_uom: "Nos",
				warehouse: "Stores - TC",
				source_required_qty: 10,
				total_required_qty: 30,
				actual_qty: 5,
				committed_qty: 2,
				available_qty: 3,
				open_material_request_qty: 4,
				open_purchase_order_qty: 5,
				current_gap_qty: 27,
				shortage_qty: 18,
				status: "new_purchase_required",
				supply_type: "purchased",
				work_order: "WO-001",
				is_shared: true,
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
			{ label: "创建生产计划", action: "create_work_order", enabled: true },
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
		"需求数量",
		"来源工单",
		"仓库库存",
		"已分配库存",
		"采购申请",
		"在途采购",
		"即时缺口",
		"采购缺口",
	]) {
		assert.match(html, new RegExp(`data-label="${materialLabel}"`));
	}
	assert.match(html, /多工单共用/);
	assert.match(html, /需新采购/);
	assert.doesNotMatch(html, />new_purchase_required</);
	assert.match(html, /\/app\/work-order\/WO-001/);
});

test("pagination HTML exposes compact production page controls", () => {
	const html = productionWorkbench.workbenchPaginationHtml(
		{ page: 3, page_size: 20, total_count: 61, total_pages: 4, has_next: true, has_prev: true },
		helpers
	);

	assert.match(html, /\u7b2c 3 \/ 4 \u9875/);
	assert.match(html, /\u5171 61 \u6761/);
	assert.match(html, /data-page="2"/);
	assert.match(html, /data-page="4"/);
	assert.match(html, /data-page-size="20" selected/);
});

test("production status meta uses colors for the actual production state", () => {
	assert.deepEqual(productionWorkbench.productionStatusMeta("ready_to_start"), { indicator: "green" });
	assert.deepEqual(productionWorkbench.productionStatusMeta("in_production"), { indicator: "blue" });
	assert.deepEqual(productionWorkbench.productionStatusMeta("partially_completed"), { indicator: "blue" });
	assert.deepEqual(productionWorkbench.productionStatusMeta("unplanned"), { indicator: "orange" });
	assert.deepEqual(productionWorkbench.productionStatusMeta("planning_required"), { indicator: "orange" });
	assert.deepEqual(productionWorkbench.productionStatusMeta("legacy_work_order"), { indicator: "red" });
	assert.deepEqual(productionWorkbench.productionStatusMeta("material_shortage"), { indicator: "red" });
	assert.deepEqual(productionWorkbench.productionStatusMeta("awaiting_supply"), { indicator: "blue" });
	assert.deepEqual(productionWorkbench.productionStatusMeta("waiting_subassembly"), { indicator: "blue" });
	assert.deepEqual(productionWorkbench.productionStatusMeta("master_data_blocked"), { indicator: "red" });
	assert.deepEqual(productionWorkbench.productionStatusMeta("awaiting_order_reservation"), { indicator: "gray" });
	assert.deepEqual(productionWorkbench.productionStatusMeta("overplanned"), { indicator: "gray" });
	assert.deepEqual(productionWorkbench.productionStatusMeta("unknown"), { indicator: "gray" });
});

test("purchase summary excludes manufactured items and aggregates their source Work Orders", () => {
	const result = productionWorkbench.aggregatePurchasedMaterials([
		{
			item_code: "RM-SHARED",
			item_name: "共享原料",
			warehouse: "Stores - TC",
			stock_uom: "Kg",
			supply_type: "purchased",
			work_order: "WO-SA-1",
			production_item: "SA-1",
			required_qty: 5,
			actual_qty: 20,
			available_qty: 5,
			current_gap_qty: 0,
			shortage_qty: 0,
			status: "ready_now",
		},
		{
			item_code: "RM-SHARED",
			item_name: "共享原料",
			warehouse: "Stores - TC",
			stock_uom: "Kg",
			supply_type: "purchased",
			work_order: "WO-SA-2",
			production_item: "SA-2",
			required_qty: 7,
			actual_qty: 20,
			available_qty: 3,
			current_gap_qty: 4,
			shortage_qty: 4,
			status: "new_purchase_required",
		},
		{
			item_code: "SA-1",
			supply_type: "manufactured",
			work_order: "WO-FG",
			required_qty: 5,
		},
	]);

	assert.equal(result.length, 1);
	assert.equal(result[0].item_code, "RM-SHARED");
	assert.equal(result[0].required_qty, 12);
	assert.equal(result[0].actual_qty, 20);
	assert.equal(result[0].available_qty, 8);
	assert.equal(result[0].current_gap_qty, 4);
	assert.equal(result[0].shortage_qty, 4);
	assert.equal(result[0].status, "new_purchase_required");
	assert.deepEqual(result[0].source_work_orders, [
		{ name: "WO-SA-1", production_item: "SA-1" },
		{ name: "WO-SA-2", production_item: "SA-2" },
	]);
});

test("demand without a Production Plan explains the prerequisite instead of showing material checks", () => {
	const html = productionWorkbench.productionDemandHtml(
		demand("NO-PLAN", {
			status_code: "planning_required",
			status_label: "待创建生产计划",
			production_plans: [],
			work_orders: [],
			materials: [],
			next_actions: [{ label: "创建生产计划", action: "create_work_order", enabled: true }],
		}),
		helpers
	);

	assert.match(html, />创建生产计划<\/button>/);
	assert.match(html, /请先创建生产计划/);
	assert.doesNotMatch(html, />检查工单物料<\/button>/);
});

test("legacy Work Order explains that it must be handled before plan readiness is calculated", () => {
	const html = productionWorkbench.productionDemandHtml(
		demand("LEGACY-WO", {
			status_code: "legacy_work_order",
			status_label: "旧工单未纳入计划",
			production_plans: [],
			work_orders: [{ name: "WO-LEGACY", production_item: "FG" }],
			materials: [],
			next_actions: [{ label: "查看销售订单", action: "view_sales_order", enabled: true }],
		}),
		helpers
	);

	assert.match(html, /未关联 Production Plan 的旧工单/);
	assert.match(html, /请先完成、停止或迁移旧工单/);
	assert.match(html, /<span class="indicator-pill red">未纳入生产计划<\/span>/);
	assert.doesNotMatch(html, />创建生产计划<\/button>/);
	assert.doesNotMatch(html, />检查工单物料<\/button>/);
});

test("planned demand HTML shows Production Plan priority and Work Order readiness", () => {
	const html = productionWorkbench.productionDemandHtml(
		demand("PLANNED", {
			production_plans: [
				{
					name: "PP-001",
					planned_date: "2026-08-20 08:00:00",
					summary: { ready_work_order_count: 1, waiting_subassembly_count: 1 },
				},
			],
			work_orders: [
				{
					name: "WO-SA",
					production_item: "SA",
					bom_no: "BOM-SA-001",
					parent_work_order: "WO-FG",
					readiness_status: "ready_now",
					required_items: [
						{
							item_code: "RM",
							item_name: "原材料",
							required_qty: 10,
							available_qty: 10,
							current_gap_qty: 0,
							source_warehouse: "Stores - TC",
							supply_type: "purchased",
							status: "ready_now",
						},
					],
				},
				{
					name: "WO-FG",
					production_item: "FG",
					bom_no: "BOM-FG-001",
					readiness_status: "waiting_subassembly",
					required_items: [
						{
							item_code: "SA",
							item_name: "半成品",
							required_qty: 5,
							available_qty: 0,
							current_gap_qty: 5,
							source_warehouse: "Stores - TC",
							supply_type: "manufactured",
							child_work_order: "WO-SA",
							status: "waiting_subassembly",
						},
					],
				},
			],
			materials: [
				{
					item_code: "RM",
					item_name: "原材料",
					warehouse: "Stores - TC",
					supply_type: "purchased",
					work_order: "WO-SA",
					production_item: "SA",
					required_qty: 10,
					available_qty: 10,
					current_gap_qty: 0,
					shortage_qty: 0,
					status: "ready_now",
				},
				{
					item_code: "SA",
					supply_type: "manufactured",
					work_order: "WO-FG",
					required_qty: 5,
				},
			],
		}),
		helpers
	);

	assert.match(html, /\/app\/production-plan\/PP-001/);
	assert.match(html, /计划优先日期/);
	assert.match(html, /当前可开工/);
	assert.match(html, /等待半成品/);
	assert.match(html, /生产执行链/);
	assert.match(html, /第 1 步/);
	assert.ok(html.includes("/app/bom/BOM-SA-001"));
	assert.match(html, /供给上级工单/);
	assert.ok(html.includes("/app/work-order/WO-FG"));
	assert.match(html, /本工单直接用料/);
	assert.match(html, /采购件/);
	assert.match(html, /由下级工单/);
	assert.ok(html.includes("/app/work-order/WO-SA"));
	assert.match(html, /底层采购物料汇总/);
	const purchaseSummary = html.match(/<section class="production-purchase-summary">([\s\S]*?)<\/section>/)?.[1] || "";
	assert.match(purchaseSummary, />RM</);
	assert.doesNotMatch(purchaseSummary, /<td data-label="物料"><strong>SA<\/strong>/);
});

test("overdue ready demand keeps delivery risk red and production state green", () => {
	const html = productionWorkbench.productionDemandHtml(
		demand("OVERDUE-READY", {
			delivery_timing: "overdue",
			risk_level: "red",
			risk_label: "\u5df2\u903e\u671f",
			status_code: "ready_to_start",
			status_label: "\u53ef\u5f00\u5de5",
		}),
		helpers
	);

	assert.match(html, /indicator-pill red">\u5df2\u903e\u671f/);
	assert.match(html, /indicator-pill green">\u53ef\u5f00\u5de5/);
});

test("material rows show linked purchase documents and status, or a no-purchase hint", () => {
	const withDocs = demand("DOCS", {
		materials: [
			{
				item_code: "RM-DOC",
				item_name: "原料 DOC",
				warehouse: "Stores - TC",
				current_gap_qty: 5,
				open_material_request_qty: 10,
				open_purchase_order_qty: 5,
				shortage_qty: 0,
				status: "purchase_request_pending",
				supply_documents: [
					{ doctype: "Material Request", name: "MREQ-1", status: "Pending", outstanding_qty: 10, allocated_qty: 4, schedule_date: "2026-08-05", is_late: false },
					{ doctype: "Purchase Order", name: "PORD-1", status: "To Receive", outstanding_qty: 5, schedule_date: "2026-08-27", is_late: true },
				],
			},
		],
	});
	const html = productionWorkbench.productionDemandHtml(withDocs, helpers);
	assert.match(html, /\/app\/material-request\/MREQ-1/);
	assert.match(html, /\/app\/purchase-order\/PORD-1/);
	assert.match(html, /To Receive/);
	assert.match(html, /未完成 10\.00 · 已分配给本单 4\.00/);
	// The late Purchase Order is still shown and flagged.
	assert.match(html, /晚于计划日期/);

	// A material with no supply documents shows the no-purchase hint.
	const noDocs = demand("NODOC");
	const noDocHtml = productionWorkbench.productionDemandHtml(noDocs, helpers);
	assert.match(noDocHtml, /尚未发起采购/);
});

test("material-ready rows hide unallocated supply documents and their late markers", () => {
	const html = productionWorkbench.productionDemandHtml(
		demand("READY-MATERIAL", {
			materials: [
				{
					item_code: "RM-READY",
					current_gap_qty: 0,
					shortage_qty: 0,
					status: "ready_now",
					supply_documents: [
						{ doctype: "Purchase Order", name: "PORD-UNALLOCATED", status: "To Receive", outstanding_qty: 8, allocated_qty: 0, is_late: true },
					],
				},
			],
		}),
		helpers
	);

	assert.doesNotMatch(html, /PORD-UNALLOCATED/);
	assert.doesNotMatch(html, /晚于计划日期/);
	assert.doesNotMatch(html, /尚未发起采购/);
});

test("current material gaps retain unallocated documents with allocation and deadline context", () => {
	const html = productionWorkbench.productionDemandHtml(
		demand("GAP-MATERIAL", {
			materials: [
				{
					item_code: "RM-GAP",
					current_gap_qty: 2,
					shortage_qty: 2,
					status: "new_purchase_required",
					supply_documents: [
						{ doctype: "Purchase Order", name: "PORD-GAP", status: "To Receive", outstanding_qty: 8, allocated_qty: 0, is_late: true },
					],
				},
			],
		}),
		helpers
	);

	assert.match(html, /PORD-GAP/);
	assert.match(html, /未分配给本单/);
	assert.match(html, /晚于计划日期/);
});

test("allocated supply documents keep total outstanding quantity and identify their allocation", () => {
	const html = productionWorkbench.productionDemandHtml(
		demand("ALLOCATED-MATERIAL", {
			materials: [
				{
					item_code: "RM-ALLOCATED",
					current_gap_qty: 0,
					shortage_qty: 0,
					status: "ready_now",
					supply_documents: [
						{ doctype: "Material Request", name: "MREQ-ALLOCATED", status: "Pending", outstanding_qty: 10, allocated_qty: 4, is_late: false },
					],
				},
			],
		}),
		helpers
	);

	assert.match(html, /MREQ-ALLOCATED/);
	assert.match(html, /未完成 10\.00/);
	assert.match(html, /已分配给本单 4\.00/);
});

test("no-purchase hint appears only for an actual purchase shortage", () => {
	const shortageHtml = productionWorkbench.productionDemandHtml(
		demand("PURCHASE-SHORTAGE", {
			materials: [{ item_code: "RM-SHORT", current_gap_qty: 2, shortage_qty: 2, status: "new_purchase_required" }],
		}),
		helpers
	);
	const readyHtml = productionWorkbench.productionDemandHtml(
		demand("NO-PURCHASE-NEEDED", {
			materials: [{ item_code: "RM-COVERED", current_gap_qty: 0, shortage_qty: 0, status: "ready_now" }],
		}),
		helpers
	);

	assert.match(shortageHtml, /尚未发起采购/);
	assert.doesNotMatch(readyHtml, /尚未发起采购/);
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
