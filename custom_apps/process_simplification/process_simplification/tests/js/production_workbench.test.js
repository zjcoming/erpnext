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
				material_priority_date: "2026-08-08",
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

function replenishmentDemand() {
	return demand("SUPPLEMENT", {
		production_required_qty: 10, active_work_order_qty: 10, unplanned_production_qty: 0,
		work_orders: [
			{ name: "TARGET", qty: 10, status: "In Process", issue_state: { code: "partially_issued" } },
			{ name: "LEAF", qty: 1, status: "Stock Reserved", is_replenishment: true,
				replenishment_root: "ROOT", replenishment_target: "TARGET", bom_level: 2,
				operation_state: { code: "pending" }, issue_state: { code: "ready" } },
			{ name: "MIDDLE", qty: 1, status: "Stock Reserved", is_replenishment: true,
				replenishment_root: "ROOT", replenishment_target: "TARGET", bom_level: 1,
				operation_state: { code: "pending" }, issue_state: { code: "waiting_material" } },
			{ name: "ROOT", qty: 1, status: "Stock Reserved", is_replenishment: true,
				replenishment_root: "ROOT", replenishment_target: "TARGET", bom_level: 0,
				supply_target_work_order: "TARGET", production_plan: "PP-SUPPLEMENT",
				operation_state: { code: "pending" }, issue_state: { code: "ready" },
				replenishment_progress: { code: "in_progress", received_qty: 0, reserved_qty: 0, fed_qty: 0 } },
		],
	});
}

test("existing supplement chain is visible once under its original target with executable cards", () => {
	const row = replenishmentDemand();
	const html = productionWorkbench.productionDemandHtml(row, { ...helpers, canManageAssignments: true });
	assert.ok(html.indexOf('data-work-order-card="TARGET"') < html.indexOf('data-replenishment-root="ROOT"'));
	for (const name of ["TARGET", "LEAF", "MIDDLE", "ROOT"]) {
		assert.equal((html.match(new RegExp(`data-work-order-card="${name}"`, "g")) || []).length, 1);
	}
	assert.match(html, /data-action="request_material_issue" data-work-order="ROOT"/);
	assert.match(html, /核对尚未开工的上游补产任务/);
	assert.equal(row.production_required_qty, 10);
	assert.equal(row.active_work_order_qty, 10);
	assert.equal(productionWorkbench.filterProductionDemands([row], { search: "LEAF" }).length, 1);
	assert.doesNotMatch(productionWorkbench.productionDemandHtml(row, helpers), /data-action="request_material_issue"/);
});

test("ready final supplement is actionable before unnecessary upstream work", () => {
	const row = replenishmentDemand();
	const manage = { ...helpers, canManageAssignments: true };
	assert.equal(productionWorkbench.productionNextTask(row, manage).workOrder.name, "ROOT");
	row.work_orders[3].issue_state.code = "waiting_material";
	assert.equal(productionWorkbench.productionNextTask(row, manage).workOrder.name, "LEAF");
	row.work_orders[3].receipt_state = { code: "requestable" };
	assert.equal(productionWorkbench.productionNextTask(row, manage).action, "request_manufacture");
	row.work_orders[3].status = "Completed";
	row.work_orders[0].issue_state.code = "ready";
	assert.equal(productionWorkbench.productionNextTask(row, manage).workOrder.name, "TARGET");
	row.work_orders[0].issue_state.code = "issued";
	row.work_orders[0].can_dispatch = true;
	row.work_orders[3].replenishment_progress.code = "fed";
	assert.equal(productionWorkbench.productionNextTask(row, manage).action, "assign");
	assert.equal(productionWorkbench.productionNextTask(row, manage).workOrder.name, "TARGET");
});

test("supplement creation or repeated click locates the original demand despite stale filters", () => {
	for (const reused of [false, true]) {
		const state = { filters: { shortageOnly: true, unplannedOnly: true }, pagination: { page: 3 }, expandedDemands: new Set() };
		const message = productionWorkbench.replenishmentCreatedFeedback(
			{ work_order: "ROOT", production_plan: "PP-SUPPLEMENT", reused }, replenishmentDemand(), state, helpers.translate);
		assert.match(message, /ROOT/);
		assert.match(message, /PP-SUPPLEMENT/);
		assert.deepEqual(state.filters, { demandKey: "SUPPLEMENT" });
		assert.equal(state.pagination.page, 1);
		assert.equal(state.focusWorkOrder, "ROOT");
		assert.ok(state.expandedDemands.has("SUPPLEMENT"));
	}
});

test("receipt alone is never displayed as completed material handoff", () => {
	const row = replenishmentDemand();
	row.work_orders[3].replenishment_progress = { code: "ready_to_feed", received_qty: 1, reserved_qty: 1, fed_qty: 0 };
	const html = productionWorkbench.productionDemandHtml(row, helpers);
	assert.match(html, /已入库并预留，待原工单领料/);
	assert.match(html, /原工单已领用补产 0.00/);
	assert.doesNotMatch(html, /补产已领用/);
	assert.match(productionWorkbench.replenishmentProgressMeta("unreserved_output", helpers.translate).label, /未定向预留/);
});

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
	const namedMaterial = demand("NAMED-MATERIAL");
	namedMaterial.work_orders[0].production_item_name = "插针骨架半成品";
	namedMaterial.work_orders[0].required_items = [{ item_code: "204001004", item_name: "PA6德尔隆" }];
	assert.deepEqual(
		productionWorkbench.filterProductionDemands([namedMaterial], { search: "PA6德尔隆" }).map((row) => row.demand_key),
		["NAMED-MATERIAL"]
	);
});

test("visible production counters recalculate from filtered demands", () => {
	assert.deepEqual(productionWorkbench.productionSummary([fixture[1]]), {
		total_demands: 1,
		unplanned_demands: 0,
		overdue_demands: 0,
		due_within_7_days: 1,
		material_shortage_demands: 0,
		unchecked_material_demands: 0,
		in_production_demands: 1,
		awaiting_order_reservation_demands: 0,
	});
});

test("production chain shows readable item names before labeled trace codes", () => {
	const namedDemand = demand("ITEM-IDENTITY", {
		item_code: "901000790",
		item_name: "传感器成品",
	});
	namedDemand.work_orders[0] = {
		...namedDemand.work_orders[0],
		production_item: "301008201014",
		production_item_name: "插针骨架半成品",
		required_items: [
			{
				item_code: "204001004",
				item_name: "PA6德尔隆",
				stock_uom: "Gram",
				required_qty: 5,
				status: "ready_now",
				supply_type: "purchased",
			},
		],
	};
	const html = productionWorkbench.productionDemandHtml(namedDemand, helpers);

	assert.ok(html.indexOf("传感器成品") < html.indexOf("产品编码：901000790"));
	assert.ok(html.indexOf("插针骨架半成品") < html.indexOf("物料编码：301008201014"));
	assert.ok(html.indexOf("PA6德尔隆") < html.indexOf("物料编码：204001004"));
	assert.match(html, /<span>客户交期<\/span><strong>2026-08-08<\/strong>/);
	assert.match(html, /<span>计划开工<\/span><strong>2026-08-07 08:00:00<\/strong>/);
	assert.match(html, /class="production-demand-next-action"/);
	assert.match(html, /<small>下一步<\/small><strong>创建生产计划<\/strong>/);
});

test("material rows show authoritative net issue instead of repeated gross child quantity", () => {
	const issuedDemand = demand("NET-ISSUE");
	issuedDemand.work_orders[0] = {
		...issuedDemand.work_orders[0],
		issue_state: { code: "partial" },
		required_items: [
			{
				item_code: "RM-001",
				item_name: "原料 001",
				stock_uom: "Nos",
				original_required_qty: 10,
				required_qty: 4,
				transferred_qty: 10,
				returned_qty: 4,
				net_transferred_qty: 6,
				available_qty: 4,
				current_gap_qty: 0,
				status: "ready_now",
				supply_type: "purchased",
			},
		],
	};

	const html = productionWorkbench.productionDemandHtml(issuedDemand, helpers);

	assert.match(html, /data-label="净发料">6\.00/);
	assert.doesNotMatch(html, /data-label="净发料">10\.00/);

	issuedDemand.work_orders[0].issue_state = { code: "not_required" };
	issuedDemand.work_orders[0].required_items[0].consumed_qty = 10;
	issuedDemand.work_orders[0].required_items[0].net_transferred_qty = 6;
	const directHtml = productionWorkbench.productionDemandHtml(issuedDemand, helpers);
	assert.match(directHtml, /data-label="已消耗">6\.00/);
	assert.doesNotMatch(directHtml, /data-label="已消耗">10\.00/);
});

test("stock-only coverage displays reservation and does not claim a missing child", () => {
	const row = demand("STOCK-COVERED");
	row.work_orders[0].required_items = [{
		item_code: "SEMI", supply_type: "manufactured", status: "ready_now",
		original_required_qty: 10, required_qty: 10, available_qty: 10,
		effective_reserved_qty: 10, current_gap_qty: 0,
		linked_pending_output_qty: 0, uncovered_supply_qty: 0,
	}];
	const html = productionWorkbench.productionDemandHtml(row, helpers);
	assert.match(html, /库存覆盖，无需下级工单/);
	assert.match(html, /其中已预留.*10\.00/);
	assert.doesNotMatch(html, /缺少下级工单/);
});

test("material shortage distinguishes pending output from uncovered supply", () => {
	const row = demand("PENDING-OUTPUT");
	row.work_orders[0].required_items = [{
		item_code: "SEMI", supply_type: "manufactured", status: "waiting_subassembly",
		original_required_qty: 10, required_qty: 10, available_qty: 1,
		effective_reserved_qty: 1, current_gap_qty: 9, child_work_order: "WO-CHILD",
		linked_pending_output_qty: 8, uncovered_supply_qty: 1,
	}];
	const html = productionWorkbench.productionDemandHtml(row, helpers);
	assert.match(html, /关联工单待入库.*8\.00/);
	assert.match(html, /未覆盖缺口.*1\.00/);
});

test("completed child work orders are shown as supply history instead of missing", () => {
	const suppliedDemand = demand("COMPLETED-CHILD");
	const material = {
		item_code: "SA-001",
		item_name: "半成品 001",
		stock_uom: "Nos",
		original_required_qty: 99,
		required_qty: 0,
		net_transferred_qty: 99,
		available_qty: 0,
		current_gap_qty: 0,
		status: "ready_now",
		supply_type: "manufactured",
		child_work_order: null,
		completed_child_work_orders: ["WO-CHILD-COMPLETED"],
	};
	suppliedDemand.work_orders[0] = {
		...suppliedDemand.work_orders[0],
		issue_state: { code: "issued" },
		required_items: [material],
	};

	const suppliedHtml = productionWorkbench.productionDemandHtml(suppliedDemand, helpers);
	assert.match(suppliedHtml, /下级工单已完成，已发料/);
	assert.ok(suppliedHtml.includes("/app/work-order/WO-CHILD-COMPLETED"));
	assert.doesNotMatch(suppliedHtml, /缺少下级工单/);

	material.required_qty = 99;
	material.net_transferred_qty = 0;
	material.available_qty = 99;
	const readyHtml = productionWorkbench.productionDemandHtml(suppliedDemand, helpers);
	assert.match(readyHtml, /下级工单已完成，可发料/);
	assert.doesNotMatch(readyHtml, /缺少下级工单/);

	material.required_qty = 1;
	material.available_qty = 0;
	material.current_gap_qty = 1;
	const shortageHtml = productionWorkbench.productionDemandHtml(suppliedDemand, helpers);
	assert.match(shortageHtml, /原下级工单已完成，待补产/);
	assert.doesNotMatch(shortageHtml, /缺少下级工单/);
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
		"优先获配成品",
		"成品覆盖",
		"需要生产",
		"工单覆盖",
		"未安排",
		"已完工",
		"当前可回补",
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

test("ready material requests issue and never exposes formal or fake assignment", () => {
	const readyDemand = demand("ASSIGN");
	readyDemand.work_orders[0].readiness_status = "ready_now";
	const html = productionWorkbench.productionDemandHtml(
		readyDemand,
		{ ...helpers, canManageAssignments: true }
	);
	assert.match(html, /class="btn btn-xs btn-primary production-work-order-action"/);
	assert.match(html, /data-action="request_material_issue"/);
	assert.match(html, />申请发料<\/button>/);
	assert.doesNotMatch(html, /production-assignment-action/);
	const preAssignDemand = demand("PRE-ASSIGN");
	preAssignDemand.work_orders[0].readiness_status = "waiting_subassembly";
	const waitingHtml = productionWorkbench.productionDemandHtml(preAssignDemand, {
		...helpers,
		canManageAssignments: true,
	});
	assert.doesNotMatch(waitingHtml, /production-assignment-action/);
	assert.doesNotMatch(waitingHtml, />预派工<\/button>/);
	assert.doesNotMatch(
		productionWorkbench.productionDemandHtml(demand("NO-ASSIGN"), helpers),
		/production-assignment-action/
	);
});

test("submitted material transfer is the formal dispatch gate", () => {
	assert.equal(
		productionWorkbench.workOrderAssignmentActionMeta(
			{ name: "WO-READY", readiness_status: "ready_now", can_dispatch: false },
			true,
			helpers.translate
		),
		null
	);
	assert.deepEqual(
		productionWorkbench.workOrderAssignmentActionMeta(
			{ name: "WO-ISSUED", readiness_status: "materials_transferred", can_dispatch: true },
			true,
			helpers.translate
		),
		{ label: "派工", primary: true, mode: "assign" }
	);
});

test("operation receipt and issue states remain visibly separate", () => {
	const stateDemand = demand("FLOW-STATES");
	stateDemand.work_orders[0] = {
		...stateDemand.work_orders[0],
		readiness_status: "in_progress",
		operation_state: { code: "completed" },
		receipt_state: { code: "requestable", remaining_qty: 10 },
		issue_state: { code: "not_required" },
	};
	const html = productionWorkbench.productionDemandHtml(stateDemand, {
		...helpers,
		canManageAssignments: true,
	});
	assert.match(html, /所有工序已完成/);
	assert.match(html, /待申请入库/);
	assert.match(html, /无需发料/);
	assert.match(html, /data-action="request_manufacture"/);
	assert.match(html, />申请入库<\/button>/);
});

test("existing stock drafts open instead of creating duplicates", () => {
	assert.deepEqual(
		productionWorkbench.workOrderNextActionMeta(
			{
				name: "WO-RECEIPT",
				status: "In Process",
				receipt_state: { code: "draft_pending", draft_entry: { name: "STE-RECEIPT" } },
			},
			helpers.translate
		),
		{
			label: "查看入库申请",
			action: "open_stock_entry",
			document: "STE-RECEIPT",
			withdrawalDocument: "STE-RECEIPT",
		}
	);
	assert.deepEqual(
		productionWorkbench.workOrderNextActionMeta(
			{
				name: "WO-ISSUE",
				status: "Not Started",
				receipt_state: { code: "not_ready" },
				issue_state: { code: "draft_pending", draft_entry: { name: "STE-ISSUE" } },
			},
			helpers.translate
		),
		{
			label: "查看发料申请",
			action: "open_stock_entry",
			document: "STE-ISSUE",
			withdrawalDocument: "STE-ISSUE",
		}
	);
	const draftDemand = demand("WITHDRAW");
	draftDemand.work_orders[0].receipt_state = {
		code: "draft_pending",
		draft_entry: { name: "STE-WITHDRAW" },
	};
	const html = productionWorkbench.productionDemandHtml(draftDemand, {
		...helpers,
		canManageAssignments: true,
	});
	assert.match(html, /data-action="withdraw_stock_request"/);
	assert.match(html, />撤回申请<\/button>/);
});

test("warehouse can open issue and receipt drafts without dispatch or withdrawal privileges", () => {
	for (const field of ["issue_state", "receipt_state"]) {
		const row = demand("WAREHOUSE");
		row.work_orders[0][field] = { code: "draft_pending", draft_entry: { name: "STE-WAREHOUSE" } };
		const html = productionWorkbench.productionDemandHtml(row, {
			...helpers, canManageAssignments: false, canReadStockEntries: true,
		});
		assert.match(html, /data-action="open_stock_entry"/);
		assert.match(html, /data-document="STE-WAREHOUSE"/);
		assert.doesNotMatch(html, /data-action="withdraw_stock_request"/);
		assert.doesNotMatch(html, /class="[^"]*production-assignment-action/);
		const hidden = productionWorkbench.productionDemandHtml(row, {
			...helpers, canManageAssignments: false, canReadStockEntries: false,
		});
		assert.doesNotMatch(hidden, /data-action="open_stock_entry"/);
	}
});

test("stock viewing permission does not allow creating production requests", () => {
	const row = demand("WAREHOUSE-REQUEST");
	row.work_orders[0].issue_state = { code: "ready" };
	const html = productionWorkbench.productionDemandHtml(row, {
		...helpers, canManageAssignments: false, canReadStockEntries: true,
	});
	assert.doesNotMatch(html, /data-action="request_material_issue"/);
});

test("completed operations waiting for receipt show assignment history rather than new dispatch", () => {
	assert.deepEqual(productionWorkbench.workOrderAssignmentActionMeta({ name: "WO", can_dispatch: true, operation_state: { code: "completed" }, receipt_state: { code: "requestable" }, worker_assignment_history_count: 1 }), { label: "查看派工记录", primary: false, mode: "history" });
});

test("next task points to receipt work ahead of generic material checks and completed steps", () => {
	const row = demand("NEXT-RECEIPT");
	row.next_actions = [{ action: "check_materials", label: "检查工单物料" }];
	row.work_orders = [
		{ ...row.work_orders[0], name: "DONE", status: "Completed", readiness_status: "completed" },
		{ ...row.work_orders[0], name: "RECEIVE", production_item_name: "线圈", receipt_state: { code: "requestable" } },
	];
	const options = { ...helpers, canManageAssignments: true };
	assert.equal(productionWorkbench.productionNextTask(row, options).workOrder.name, "RECEIVE");
	const html = productionWorkbench.productionDemandHtml(row, options);
	assert.match(html, /线圈 · 申请入库/);
	assert.match(html, /data-work-order-card="DONE">/);
	assert.match(html, /data-work-order-card="RECEIVE" open>/);
	assert.ok(html.indexOf("production-current-task") < html.indexOf("production-work-order-list"));
	assert.equal(productionWorkbench.productionNextTask(row, { ...options, canManageAssignments: false, canReadStockEntries: true }), null);
});

test("unchecked material demands remain distinct from checked shortages", () => {
	const row = demand("UNCHECKED", { material_summary: { status_code: "not_checked", shortage_item_count: 0 } });
	const summary = productionWorkbench.productionSummary([row]);
	assert.equal(summary.material_shortage_demands, 0);
	assert.equal(summary.unchecked_material_demands, 1);
});

test("workflow confirmations explain when inventory and worker notifications change", () => {
	assert.match(
		productionWorkbench.workOrderActionConfirmation("request_manufacture", {}, helpers.translate),
		/库房提交入库单后增加/
	);
	assert.match(
		productionWorkbench.workOrderActionConfirmation("request_material_issue", {}, helpers.translate),
		/库房完成发料前不会通知工人/
	);
	assert.match(
		productionWorkbench.workOrderActionConfirmation("create_replenishment", {}, helpers.translate),
		/不会重新打开已经完成的原工单/
	);
	assert.match(
		productionWorkbench.workOrderActionConfirmation("request_material_issue_override", {}, helpers.translate),
		/硬预留仍不会被抢用/
	);
	assert.match(
		productionWorkbench.workOrderActionConfirmation("withdraw_stock_request", {}, helpers.translate),
		/硬预留会立即释放/
	);
});

test("returned material allocated to an urgent order exposes reissue and replenishment choices", () => {
	const conflictDemand = demand("ALLOCATION-CONFLICT");
	conflictDemand.work_orders[0] = {
		...conflictDemand.work_orders[0],
		name: "WO-CURRENT",
		readiness_status: "waiting_subassembly",
		issue_state: { code: "partially_issued", additional_issueable_qty: 0 },
		required_items: [
			{
				name: "WOI-CURRENT",
				item_code: "SEMI",
				item_name: "半成品",
				required_qty: 10,
				returned_qty: 1,
				remaining_issue_qty: 1,
				current_gap_qty: 4,
				status: "waiting_subassembly",
				supply_type: "manufactured",
				replenishment_required: true,
				allocation_conflict: {
					sources: [{
						source_type: "priority_allocation",
						work_order: '<WO-URGENT onmouseover="x">',
						impact_qty: 4,
					}],
				},
			},
		],
	};
	const html = productionWorkbench.productionDemandHtml(conflictDemand, {
		...helpers,
		canManageAssignments: true,
	});
	assert.match(html, /优先分配影响/);
	assert.match(html, /&lt;WO-URGENT/);
	assert.doesNotMatch(html, /<WO-URGENT/);
	assert.match(html, /data-action="request_material_issue_override"/);
	assert.match(html, />调整优先级并申请补发<\/button>/);
	assert.match(html, /data-action="create_replenishment"/);
	assert.match(html, />保留优先分配并生成补产任务<\/button>/);
});

test("terminal, blocked, missing-task, unknown, and legacy Work Orders expose no assignment action", () => {
	const hiddenCases = [
		{ readiness_status: "completed", status: "Completed" },
		{ readiness_status: "blocked", status: "Not Started" },
		{ readiness_status: "production_task_missing", status: "Not Started" },
		{ readiness_status: "unknown", status: "Not Started" },
		{ readiness_status: "ready_now", status: "Stopped" },
		{ readiness_status: "ready_now", status: "Closed" },
		{ readiness_status: "ready_now", status: "Cancelled", docstatus: 2 },
	];
	for (const workOrder of hiddenCases) {
		assert.equal(
			productionWorkbench.workOrderAssignmentActionMeta(
				{ name: "WO-HIDDEN", ...workOrder },
				true,
				helpers.translate
			),
			null
		);
	}
	assert.equal(
		productionWorkbench.workOrderAssignmentActionMeta(
			{ name: "WO-LEGACY", readiness_status: "waiting_subassembly", status: "Not Started" },
			false,
			helpers.translate
		),
		null
	);

	const completedDemand = demand("COMPLETED-ACTION");
	completedDemand.work_orders[0] = {
		...completedDemand.work_orders[0],
		readiness_status: "completed",
		status: "Completed",
	};
	const html = productionWorkbench.productionDemandHtml(completedDemand, {
		...helpers,
		canManageAssignments: true,
	});
	assert.match(html, /已完成/);
	assert.doesNotMatch(html, /production-assignment-action/);
});

test("terminal Work Orders with visible history expose a read-only assignment record action", () => {
	assert.deepEqual(
		productionWorkbench.workOrderAssignmentActionMeta(
			{
				name: "WO-COMPLETED",
				status: "Completed",
				readiness_status: "completed",
				worker_assignment_history_count: 2,
			},
			true,
			helpers.translate
		),
		{ label: "查看派工记录", primary: false, mode: "history" }
	);

	const completedDemand = demand("COMPLETED-HISTORY");
	completedDemand.work_orders[0] = {
		...completedDemand.work_orders[0],
		status: "Completed",
		readiness_status: "completed",
		worker_assignment_history_count: 1,
	};
	const html = productionWorkbench.productionDemandHtml(completedDemand, {
		...helpers,
		canManageAssignments: true,
	});
	assert.match(html, /data-assignment-mode="history"/);
	assert.match(html, />查看派工记录<\/button>/);
});

test("active Work Orders keep assignment history visible while material dispatch is blocked", () => {
	const blockedWithHistory = {
		name: "WO-PARTIAL-ISSUE",
		status: "In Process",
		readiness_status: "in_progress",
		can_dispatch: false,
		worker_assignment_history_count: 1,
	};
	assert.deepEqual(
		productionWorkbench.workOrderAssignmentActionMeta(
			blockedWithHistory,
			true,
			helpers.translate
		),
		{ label: "查看派工记录", primary: false, mode: "history" }
	);
	assert.deepEqual(
		productionWorkbench.workOrderAssignmentActionMeta(
			blockedWithHistory,
			false,
			helpers.translate
		),
		{ label: "查看派工记录", primary: false, mode: "history" }
	);

	const activeDemand = demand("ACTIVE-HISTORY");
	activeDemand.work_orders[0] = {
		...activeDemand.work_orders[0],
		...blockedWithHistory,
	};
	const html = productionWorkbench.productionDemandHtml(activeDemand, {
		...helpers,
		canManageAssignments: true,
	});
	assert.match(html, /data-assignment-mode="history"/);
	assert.match(html, />查看派工记录<\/button>/);
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
					material_priority_date: "2026-08-10",
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
	assert.match(html, /计划开始.*2026-08-20 08:00:00/);
	assert.match(html, /物料优先依据.*2026-08-10/);
	assert.match(html, /订单行交付日期/);
	assert.doesNotMatch(html, /计划优先日期/);
	assert.match(html, /物料已齐，待发料/);
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
	const purchaseSummary = html.match(/<details class="production-purchase-summary production-secondary-details">([\s\S]*?)<\/details>/)?.[1] || "";
	assert.match(purchaseSummary, /物料编码：RM/);
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
	assert.match(html, /晚于订单交期/);

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
	assert.doesNotMatch(html, /晚于订单交期/);
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
	assert.match(html, /晚于订单交期/);
});

test("unverifiable supply deadline remains visible without promising coverage", () => {
	const html = productionWorkbench.productionDemandHtml(
		demand("UNKNOWN-DEADLINE", {
			materials: [{
				item_code: "RM-UNKNOWN",
				current_gap_qty: 2,
				shortage_qty: 2,
				status: "new_purchase_required",
				supply_documents: [{
					doctype: "Purchase Order",
					name: "PORD-UNKNOWN",
					outstanding_qty: 8,
					allocated_qty: 0,
					deadline_unknown: true,
				}],
			}],
		}),
		helpers
	);

	assert.match(html, /PORD-UNKNOWN/);
	assert.match(html, /交期不可核验/);
	assert.match(html, /未分配给本单/);
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

test("route focus replaces stale filters, keeps the internal key out of search and reloads once", async () => {
	const loads = [];
	const state = { filters: { search: "old", shortageOnly: true, status: "in_production" }, pagination: { page: 3, page_size: 50 }, expandedDemands: new Set(["OLD"]) };
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
	assert.deepEqual(state.filters, { demandKey: "SOI-FOCUS" });
	assert.equal(state.pagination.page, 1);
	assert.equal(state.pagination.page_size, 50);
	assert.deepEqual([...state.expandedDemands], ["SOI-FOCUS"]);
	assert.equal(loads.length, 1);
});

test("order-line focus matches exactly and combines with manual filters", () => {
	const selected = demand("FOCUS", { sales_order: "SO-SHARED" });
	const sibling = demand("FOCUS-OTHER", { sales_order: "SO-SHARED", item_name: "FOCUS" });
	assert.deepEqual(productionWorkbench.filterProductionDemands([selected, sibling], { demandKey: "FOCUS" }), [selected]);
	assert.deepEqual(productionWorkbench.filterProductionDemands([selected, sibling], { demandKey: "FOCUS", search: "no match" }), []);
	assert.deepEqual(productionWorkbench.filterProductionDemands([selected, sibling], { demandKey: "MISSING" }), []);
});

test("active filter notice explains the order and result count without exposing its internal key", () => {
	const row = demand("internal-row-id", { sales_order: "SO-001", item_name: "传感器 <A>" });
	const label = productionWorkbench.productionFocusLabel(row);
	const html = productionWorkbench.productionActiveFiltersHtml([label, "搜索：<script>"], 1, helpers);
	assert.match(html, /已筛选.*共 1 条生产需求/);
	assert.match(html, /仅看订单明细：SO-001 · 传感器 &lt;A&gt;/);
	assert.match(html, /清除筛选，查看全部/);
	assert.doesNotMatch(html, /internal-row-id|<script>/);
	assert.match(productionWorkbench.productionActiveFiltersHtml([label], 0, helpers), /共 0 条生产需求/);
	assert.match(productionWorkbench.productionActiveFiltersHtml([label], null, helpers), /正在更新结果/);
	assert.equal(productionWorkbench.productionActiveFiltersHtml([], 12, helpers), "");
	assert.equal(productionWorkbench.productionFocusLabel(null), "仅看订单明细：已指定的订单明细");
});

test("clear filters restores all demands and resets focus and pagination", () => {
	const state = {
		filters: { demandKey: "OVERDUE", search: "old", shortageOnly: true, showOther: true, customer: "CUST-OTHER" },
		focusedDemand: fixture[0], focusWorkOrder: "WO-OLD",
		pagination: { page: 3, page_size: 50 }, expandedDemands: new Set(["OVERDUE"]),
	};
	productionWorkbench.clearProductionFilters(state);
	assert.deepEqual(state.filters, {});
	assert.equal(state.focusedDemand, null);
	assert.equal(state.focusWorkOrder, null);
	assert.equal(state.pagination.page, 1);
	assert.equal(state.pagination.page_size, 50);
	assert.equal(state.expandedDemands.size, 0);
	assert.deepEqual(productionWorkbench.filterProductionDemands(fixture, state.filters), fixture);
});
