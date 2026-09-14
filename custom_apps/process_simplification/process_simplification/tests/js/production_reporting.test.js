const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const appDirectory = path.resolve(__dirname, "../..");
const workerPage = require(path.join(
	appDirectory,
	"process_simplification/page/my_production_reporting/my_production_reporting.js"
));
const workerHistoryPage = require(path.join(
	appDirectory,
	"process_simplification/page/production_report_history/production_report_history.js"
));
const reviewPage = require(path.join(
	appDirectory,
	"process_simplification/page/production_report_review/production_report_review.js"
));
const exceptionReviewPage = require(path.join(
	appDirectory,
	"process_simplification/page/production_exception_review/production_exception_review.js"
));
const orderWorkbench = require(path.join(
	appDirectory,
	"process_simplification/page/order_workbench/order_workbench.js"
));
const productionWorkbench = require(path.join(
	appDirectory,
	"process_simplification/page/production_workbench/production_workbench.js"
));
const workerAssignment = require(path.join(
	appDirectory,
	"public/js/worker_assignment.js"
));
const wageSummaryList = require(path.join(
	appDirectory,
	"process_simplification/doctype/monthly_worker_wage_summary/monthly_worker_wage_summary_list.js"
));

test("wage month controls use human month labels while preserving the first-day API value", () => {
	assert.equal(wageSummaryList.wageSummaryMonthLabel("2026-08-01"), "2026年08月");
	assert.deepEqual(
		wageSummaryList.wageSummaryMonthOptions("2026-08-30", { includeAll: true, count: 3 }),
		[
			{ value: "", label: "全部月份" },
			{ value: "2026-08-01", label: "2026年08月" },
			{ value: "2026-07-01", label: "2026年07月" },
			{ value: "2026-06-01", label: "2026年06月" },
		]
	);
	assert.deepEqual(
		wageSummaryList.wageSummaryFilterMonthOptions("2026-08-30", {
			includeAll: true,
			count: 2,
		}),
		[
			{ value: "", label: "全部月份" },
			{ value: "2026年08月", label: "2026年08月" },
			{ value: "2026年07月", label: "2026年07月" },
		]
	);
});

test("v16 toolbar refresh wrappers start loading without returning a thenable", () => {
	const calls = [];
	const pending = Promise.resolve();
	const loaders = [
		[workerPage.runWorkerReportingToolbarLoad, "worker"],
		[reviewPage.runReviewToolbarLoad, "review"],
		[exceptionReviewPage.runExceptionReviewToolbarLoad, "exception"],
		[orderWorkbench.runOrderWorkbenchToolbarLoad, "order"],
		[productionWorkbench.runProductionWorkbenchToolbarLoad, "production"],
	];
	for (const [run, name] of loaders) {
		const result = run(() => {
			calls.push(name);
			return pending;
		});
		assert.equal(result, undefined);
	}
	assert.deepEqual(calls, ["worker", "review", "exception", "order", "production"]);
});

test("worker report exposes active timing plus the three review statuses", () => {
	assert.deepEqual(workerPage.workReportStatusMeta("In Progress"), {
		label: "计时中",
		indicator: "blue",
	});
	assert.deepEqual(workerPage.workReportStatusMeta("Pending Approval"), {
		label: "待审核",
		indicator: "orange",
	});
	assert.deepEqual(workerPage.workReportStatusMeta("Approved"), {
		label: "已通过",
		indicator: "green",
	});
	assert.deepEqual(workerPage.workReportStatusMeta("Rejected"), {
		label: "已驳回",
		indicator: "red",
	});
});

test("worker and reviewer exception labels keep approval separate from stock posting", () => {
	assert.deepEqual(workerPage.workerExceptionStatusMeta("Pending Approval"), {
		label: "待主管审核",
		indicator: "orange",
	});
	assert.deepEqual(workerPage.workerExceptionStatusMeta("Awaiting Stock Entry"), {
		label: "已批准，待库存过账",
		indicator: "blue",
	});
	assert.equal(workerPage.workerExceptionTypeLabel("Material Return"), "退回未用物料");
	assert.equal(exceptionReviewPage.exceptionTypeLabel("Material Scrap"), "物料转报废仓");
	assert.equal(exceptionReviewPage.exceptionCauseLabel("Operation Error"), "操作失误");
	assert.deepEqual(
		exceptionReviewPage.exceptionActionState({
			can_approve: 1,
			can_reject: 1,
			can_open_stock_entry: 1,
			stock_entry: "MAT-STE-0001",
		}),
		{ can_approve: true, can_reject: true, can_open_stock_entry: true }
	);
});

test("material exception choices show the name first and the item code on the detail line", () => {
	const option = workerPage.workerExceptionMaterialOption(
		{
			key: "route-key",
			item_code: "301008201014",
			item_name: "插片骨架173",
			source_warehouse: "在制品仓 - 恒",
			return_warehouse: "原材料仓 - 恒",
			requestable_qty: 1,
			stock_uom: "Nos",
		},
		"Material Return"
	);

	assert.equal(option.label, "插片骨架173");
	assert.equal(option.value, "route-key");
	assert.match(option.description, /^物料编码：301008201014/);
	assert.match(option.description, /在制品仓 - 恒 → 原材料仓 - 恒/);
	assert.match(option.description, /个人可申请 1/);
	assert.doesNotMatch(option.label, /301008201014/);
});

test("material choices distinguish missing scrap config from no material and isolate defective returns", () => {
	const row = {key: "rm", item_code: "RM", requestable_qty: 4, source_warehouse: "WIP", return_warehouse: "Stores"};
	const missing = workerPage.workerExceptionMaterialState({materials: [row]}, "Material Scrap", "Other");
	assert.equal(missing.materials.length, 0);
	assert.match(missing.message, /公司设置/);
	const configured = {...row, scrap_warehouse: "Scrap", quarantine_warehouse: "Quarantine"};
	assert.equal(workerPage.workerExceptionMaterialState({materials: [configured]}, "Material Scrap", "Other").materials[0].target_warehouse, "Scrap");
	assert.equal(workerPage.workerExceptionMaterialState({materials: [configured]}, "Material Return", "Material Defect").materials[0].target_warehouse, "Quarantine");
	assert.equal(workerPage.workerExceptionMaterialState({materials: [configured]}, "Material Return", "Other").materials[0].target_warehouse, "Stores");
	assert.equal(workerPage.workerExceptionMaterialState({materials: [], empty_material_message: "没有剩余物料"}, "Material Scrap", "Other").message, "没有剩余物料");
	assert.equal(workerPage.workerExceptionStatusMeta("Withdrawn").label, "已撤回");
});

test("worker button state is driven by server block codes", () => {
	assert.deepEqual(workerPage.workReportButtonMeta({ can_start: true }), {
		action: "start",
		label: "开始计时",
		disabled: false,
	});
	assert.deepEqual(
		workerPage.workReportButtonMeta({ active_report: "JCWR-1", can_finish: true }),
		{ action: "finish", label: "结束并报工", disabled: false }
	);
	assert.deepEqual(
		workerPage.workReportTimerButtonMeta({ active_report: "JCWR-1" }),
		{ action: "pause", label: "暂停计时", disabled: false }
	);
	assert.deepEqual(
		workerPage.workReportTimerButtonMeta({ active_report: "JCWR-1", timer_paused_at: "2026-08-30 09:30:00" }),
		{ action: "resume", label: "继续计时", disabled: false }
	);
	assert.equal(workerPage.workReportTimerButtonMeta({}), null);
	assert.deepEqual(
		workerPage.workReportButtonMeta({ can_start: false, block_code: "PENDING_REPORT" }),
		{ action: null, label: "待主管审核", disabled: true }
	);
	assert.deepEqual(
		workerPage.workReportButtonMeta({ can_start: false, block_code: "RATE_MISSING" }),
		{ action: null, label: "缺少计价规则", disabled: true }
	);
	assert.deepEqual(
		workerPage.workReportButtonMeta({ can_start: false, block_code: "MATERIAL_NOT_TRANSFERRED" }),
		{ action: null, label: "等待发料", disabled: true }
	);
	assert.equal(
		workerPage.workReportBlockMessage({
			block_code: "MATERIAL_NOT_TRANSFERRED",
			block_message: "Materials have not been issued.",
		}),
		"物料尚未发放到在制仓，发料后才能开始报工。"
	);
	assert.equal(
		workerPage.workReportBlockMessage({ block_code: "UNKNOWN", block_message: "原始提示" }),
		"原始提示"
	);
});

test("worker assignments separate active work and demote material waits", () => {
	const partitions = workerPage.partitionWorkerAssignments([
		{ name: "WAITING", block_code: "MATERIAL_NOT_TRANSFERRED" },
		{ name: "READY", can_start: true },
		{ name: "ACTIVE", active_report: "JCWR-1" },
		{ name: "PENDING", block_code: "PENDING_REPORT" },
	]);
	assert.deepEqual(partitions.active.map((row) => row.name), ["ACTIVE"]);
	assert.deepEqual(partitions.ready.map((row) => row.name), ["READY"]);
	assert.deepEqual(partitions.blocked.map((row) => row.name), ["PENDING"]);
	assert.deepEqual(partitions.waitingMaterial.map((row) => row.name), ["WAITING"]);
	assert.ok(workerPage.workerAssignmentPriority({ can_start: true }) < workerPage.workerAssignmentPriority({ block_code: "MATERIAL_NOT_TRANSFERRED" }));
	assert.equal(workerPage.shouldOpenWaitingMaterialGroup(partitions), false);
	assert.equal(
		workerPage.shouldOpenWaitingMaterialGroup({
			active: [],
			ready: [],
			blocked: [],
			waitingMaterial: [{ name: "WAITING" }],
		}),
		true
	);
});

test("worker task navigation keeps active work first and cards hide audit facts behind details", () => {
	const nav = workerPage.workerTaskNavigationHtml("my-production-reporting", {
		translate: (message) => message,
		escapeHtml: (value) => String(value),
	});
	assert.ok(nav.indexOf("active-production-work") < nav.indexOf("my-production-reporting"));
	assert.match(nav, /data-route="my-production-reporting" aria-current="page">待开始/);

	const card = workerPage.workerAssignmentCardHtml(
		{
			name: "ASSIGN-1",
			operation: "装配",
			production_item: "FG-001",
			job_card: "JC-001",
			work_order: "WO-001",
			can_start: true,
			reportable_qty: 5,
			completed_qty: 2,
			assigned_qty: 10,
			job_card_completed_qty: 4,
			for_quantity: 20,
		},
		{
			translate: (message) => message,
			escapeHtml: (value) => String(value ?? ""),
			formatNumber: (value) => Number(value || 0).toFixed(2),
			formatDateTime: (value) => value || "-",
		}
	);
	assert.match(card, /class="worker-assignment-key-facts"/);
	assert.match(card, /当前可报/);
	assert.match(card, /<details class="worker-assignment-more">/);
	assert.ok(card.indexOf("开始计时") < card.indexOf("生产任务单：JC-001"));
});

test("my reporting removes recent history while the history card exposes escaped audit facts", () => {
	const myPageScript = fs.readFileSync(
		path.join(
			appDirectory,
			"process_simplification/page/my_production_reporting/my_production_reporting.js"
		),
		"utf8"
	);
	assert.doesNotMatch(myPageScript, /最近报工|worker-report-history/);
	const esc = (value) => String(value ?? "")
		.replaceAll("&", "&amp;")
		.replaceAll("<", "&lt;")
		.replaceAll(">", "&gt;")
		.replaceAll('"', "&quot;");
	const html = workerHistoryPage.workerReportHistoryCardHtml(
		{
			operation: '<img src=x onerror="alert(1)">',
			status: "Rejected",
			job_card: "PO-JOB00001",
			work_order: "MFG-WO-00001",
			completed_qty: 3,
			actual_minutes: 20,
			reported_minutes: 25,
			wage_amount: 6,
			reviewed_by: "supervisor@example.com",
			reviewed_at: "2026-08-30 18:00:00",
			rejection_reason: "数量不符",
		},
		{
			translate: (message) => message,
			escapeHtml: esc,
			formatNumber: (value) => Number(value || 0).toFixed(2),
			formatDateTime: (value) => value || "-",
		}
	);
	assert.doesNotMatch(html, /<img/);
	assert.match(html, /&lt;img/);
	assert.match(html, /审核人.*supervisor@example\.com/);
	assert.match(html, /审核时间.*2026-08-30 18:00:00/);
	assert.match(html, /驳回原因.*数量不符/);
});

test("plain text rejection reasons stay escaped in worker history and supervisor details", () => {
	const reason = '甲 & 乙 <b>仅作文字</b>；字面 &amp; 和 "引号" <img src=x onerror="alert(1)">';
	const esc = (value) => String(value ?? "")
		.replaceAll("&", "&amp;").replaceAll("<", "&lt;")
		.replaceAll(">", "&gt;").replaceAll('"', "&quot;");
	const helpers = {
		translate: (value) => value,
		escapeHtml: esc,
		formatNumber: (value) => Number(value || 0).toFixed(2),
		formatDateTime: (value) => value || "-",
	};
	const row = { status: "Rejected", rejection_reason: reason };
	for (const html of [
		workerHistoryPage.workerReportHistoryCardHtml(row, helpers),
		reviewPage.reviewDetailsHtml(row, helpers),
	]) {
		assert.ok(html.includes(esc(reason)));
		assert.doesNotMatch(html, /<img|<b>/);
		assert.match(html, /字面 &amp;amp;/);
	}
});

test("piecework and time wage previews use different quantities", () => {
	assert.equal(workerPage.workReportAmount("Piecework", 3, 120, 5), 15);
	assert.equal(workerPage.workReportAmount("Time", 3, 90, 20), 30);
});

test("dual-method operations expose both finish choices and default submission to piecework", () => {
	const assignment = {
		wage_options: [
			{ wage_type: "Time", rate: 30 },
			{ wage_type: "Piecework", rate: 5 },
		],
	};
	assert.deepEqual(workerPage.workReportWageOptions(assignment), [
		{ wage_type: "Piecework", rate: 5 },
		{ wage_type: "Time", rate: 30 },
	]);
	assert.equal(workerPage.workReportDefaultWageType(assignment), "Piecework");
	const translate = (message, values = []) => message.replace("{0}", values[0]);
	assert.equal(
		workerPage.workReportWageOptionLabel(
			{ wage_type: "Piecework", rate: 5 },
			String,
			translate
		),
		"计件 · 5/件"
	);
	assert.equal(
		workerPage.workReportWageLabel(assignment, String, translate),
		"计件 · 5/件；计时 · 30/小时"
	);
	assert.equal(
		workerPage.workReportWageLabel(
			{ ...assignment, active_report: "JCWR-DUAL", wage_type: "Piecework", rate: 5 },
			String,
			translate
		),
		"计件 · 5/件；计时 · 30/小时"
	);
	assert.equal(
		workerPage.workReportDefaultWageType({ wage_options: [{ wage_type: "Time", rate: 30 }] }),
		"Time"
	);
	assert.equal(
		workerPage.workReportWageLabel(
			{
				active_report: "JCWR-1",
				wage_type: "Time",
				rate: 30,
				wage_options: [{ wage_type: "Time", rate: 35 }],
			},
			String,
			translate
		),
		"计时 · 30/小时"
	);
});

test("worker and review pages strip database microseconds before Frappe datetime formatting", () => {
	const value = "2026-08-27 23:31:23.424982";
	assert.equal(workerPage.normalizeFrappeDateTime(value), "2026-08-27 23:31:23");
	assert.equal(
		workerPage.workReportFinishDialogStartedAt({ active_started_at: value }),
		"2026-08-27 23:31:23"
	);
	assert.equal(reviewPage.normalizeFrappeDateTime(value), "2026-08-27 23:31:23");
});

test("capacity conflict disables whole approval but still permits rejection", () => {
	assert.deepEqual(reviewPage.reviewActionState({ capacity_conflict: false }), {
		approve_disabled: false,
		reject_disabled: false,
		message: "",
	});
	assert.deepEqual(reviewPage.reviewActionState({ capacity_conflict: true }), {
		approve_disabled: true,
		reject_disabled: false,
		message: "数据已变化，请刷新；当前只能驳回重报。",
	});
	assert.deepEqual(
		reviewPage.reviewActionState({
			can_approve: false,
			can_reject: false,
			approve_block_message: "不能审核自己的报工",
		}),
		{
			approve_disabled: true,
			reject_disabled: true,
			message: "不能审核自己的报工",
		}
	);
});

test("review UI prefers employee names and exposes bounded pagination controls", () => {
	assert.equal(
		reviewPage.reviewEmployeeLabel({ employee: "HR-EMP-00002", employee_name: "张三" }),
		"张三"
	);
	assert.equal(reviewPage.reviewEmployeeLabel({ employee: "HR-EMP-00002" }), "HR-EMP-00002");
	const html = reviewPage.reviewPaginationHtml(
		{ page: 2, page_length: 20, total_count: 45, total_pages: 3, has_prev: true, has_next: true },
		"processed_today",
		{ translate: (message) => message, escapeHtml: (value) => String(value) }
	);
	assert.match(html, /第\s*2\s*\/\s*3\s*页/);
	assert.match(html, /共\s*45\s*条/);
	assert.match(html, /data-list="processed_today"/);
	assert.match(html, /data-page="1"/);
	assert.match(html, /data-page="3"/);
});

test("review history month filters cover the complete calendar month", () => {
	assert.deepEqual(reviewPage.reviewMonthRange("2026-02"), {
		from_date: "2026-02-01",
		to_date: "2026-02-28",
	});
	assert.deepEqual(reviewPage.reviewMonthRange("2024-02"), {
		from_date: "2024-02-01",
		to_date: "2024-02-29",
	});
	assert.deepEqual(exceptionReviewPage.exceptionMonthRange("2026-09"), {
		from_date: "2026-09-01",
		to_date: "2026-09-30",
	});
	assert.deepEqual(exceptionReviewPage.exceptionMonthRange("not-a-month"), {
		from_date: null,
		to_date: null,
	});
});

test("exception history exposes bounded pagination controls", () => {
	const html = exceptionReviewPage.exceptionPaginationHtml(
		{ page: 2, page_length: 50, total_count: 75, total_pages: 2, has_prev: true, has_next: false },
		{ translate: (message) => message, escapeHtml: (value) => String(value) }
	);
	assert.match(html, /第\s*2\s*\/\s*2\s*页/);
	assert.match(html, /共\s*75\s*条/);
	assert.match(html, /data-page="1"/);
	assert.match(html, /value="50" selected/);
});

test("review detail shows linked production documents and escapes audit facts", () => {
	const html = reviewPage.reviewDetailsHtml(
		{
			name: "JCWR-0001",
			status: "Approved",
			employee: "HR-EMP-00002",
			employee_name: "张三",
			job_card: "PO-JOB00054",
			work_order: "MFG-WO-2026-00031",
			operation: '<img src=x onerror="alert(1)">',
			wage_type: "Time",
			manual_time_entry: 1,
		},
		{
			translate: (message) => message,
			escapeHtml: (value) => String(value ?? "")
				.replaceAll("&", "&amp;")
				.replaceAll("<", "&lt;")
				.replaceAll(">", "&gt;")
				.replaceAll('"', "&quot;"),
			formatNumber: (value) => Number(value || 0).toFixed(2),
			formatDateTime: (value) => value || "-",
		}
	);
	assert.match(html, /张三/);
	assert.match(html, /data-doctype="Job Card" data-name="PO-JOB00054"/);
	assert.match(html, /data-doctype="Work Order" data-name="MFG-WO-2026-00031"/);
	assert.match(html, /工人手工填写/);
	assert.doesNotMatch(html, /<img/);
	assert.match(html, /&lt;img/);
});

test("supervisor assignment action exposes orphaned active-session cleanup", () => {
	assert.deepEqual(
		reviewPage.reviewAssignmentActionState({
			active_report: "JCWR-1",
			can_unassign: false,
		}),
		{
			action: "cancel_session",
			label: "取消活动计时",
			message: "工人仍在计时；可先取消活动计时，再处理派工。",
		}
	);
	assert.deepEqual(reviewPage.reviewAssignmentActionState({ can_unassign: true }), {
		action: "unassign",
		label: "取消派工",
		message: "",
	});
	assert.equal(
		reviewPage.reviewAssignmentActionState({
			can_unassign: false,
			unassign_block_message: "多人派工必须整体调整。",
		}).message,
		"多人派工必须整体调整。"
	);
});

test("contextual assignment chooses the first eligible operation and keeps role scope explicit", () => {
	assert.equal(
		workerAssignment.defaultAssignmentJobCard({
			job_cards: [
				{ name: "JC-BLOCKED", can_assign: false },
				{ name: "JC-READY", can_assign: true },
			],
		}),
		"JC-READY"
	);
	assert.equal(workerAssignment.canManageWorkerAssignments("Administrator", []), true);
	assert.equal(workerAssignment.canManageWorkerAssignments("manager@example.com", ["Process Simplification Production Manager"]), true);
	assert.equal(workerAssignment.canManageWorkerAssignments("legacy@example.com", ["Production Supervisor"]), false);
	assert.equal(workerAssignment.canManageWorkerAssignments("worker@example.com", ["Production Worker"]), false);
});

test("multi-worker dispatch plans require unique workers and an exact task total", () => {
	assert.equal(
		Object.values(workerAssignment.WORKER_ASSIGNMENT_GRID_COLUMNS).reduce(
			(total, columns) => total + columns,
			0
		),
		10,
		"Frappe editable grids reserve at most 10 columns for business fields"
	);
	assert.equal(
		workerAssignment.workerRedispatchPlanError(
			[{ employee: "EMP-003", assigned_qty: 1 }],
			1
		),
		""
	);
	assert.match(
		workerAssignment.workerRedispatchPlanError(
			[{ employee: "EMP-003", assigned_qty: 2 }],
			1
		),
		/最多只能分配 1.*当前合计 2/
	);
	assert.deepEqual(workerAssignment.workerAssignmentPlanRows(null), []);
	const rows = [
		{ employee: "EMP-001", assigned_qty: 40, notes: "A区" },
		{ employee: "EMP-002", assigned_qty: 60, notes: "B区" },
	];
	assert.equal(workerAssignment.workerAssignmentPlanTotal(rows), 100);
	assert.equal(workerAssignment.workerAssignmentPlanError(rows, 100), "");
	assert.match(workerAssignment.workerAssignmentPlanError(rows, 99), /100.*99/);
	assert.match(
		workerAssignment.workerAssignmentPlanError(
			[
				{ employee: "EMP-001", assigned_qty: 40 },
				{ employee: "EMP-001", assigned_qty: 60 },
			],
			100
		),
		/不能重复/
	);
	assert.deepEqual(
		workerAssignment.workerAssignmentPlanRows({
			assignments: [
				{ employee: "EMP-001", assigned_qty: 40, notes: "A区", assignment_status: "Active" },
				{ employee: "EMP-OLD", assigned_qty: 10, assignment_status: "Completed" },
			],
		}),
		[{ employee: "EMP-001", assigned_qty: 40, notes: "A区" }]
	);
});

test("contextual assignment renders stable Chinese material state labels", () => {
	assert.deepEqual(workerAssignment.workerAssignmentMaterialStatusMeta("READY_TO_REPORT"), {
		label: "可报工",
		indicator: "green",
	});
	assert.deepEqual(
		workerAssignment.workerAssignmentMaterialStatusMeta("MATERIAL_NOT_TRANSFERRED"),
		{ label: "等待发料", indicator: "orange" }
	);
});

test("completed assignment context is read-only, retains history, and explains the terminal state in Chinese", () => {
	assert.equal(
		workerAssignment.workerAssignmentWorkOrderStatusLabel("In Process"),
		"生产中"
	);
	assert.deepEqual(workerAssignment.workerAssignmentStatusMeta("Completed"), {
		label: "已完成",
		indicator: "green",
	});
	assert.equal(
		workerAssignment.workerAssignmentBlockMessage({
			block_code: "JOB_CARD_NOT_DRAFT",
			block_message: "Worker reporting is only allowed while the Job Card is Draft.",
		}),
		"生产任务单已提交或完成，不能再派工。"
	);
	assert.equal(workerAssignment.assignmentDialogCanSubmit({ can_assign: false }), false);
	assert.equal(workerAssignment.assignmentDialogCanSubmit({ can_assign: true }), true);
	assert.equal(
		workerAssignment.assignmentDialogCanSubmit({ can_assign: true }, "history"),
		false
	);
	assert.equal(
		workerAssignment.workerAssignmentBlockMessage({
			block_code: "MATERIAL_NOT_FULLY_ISSUED",
		}),
		"物料尚未全部发到在制品仓，当前只能查看派工记录。"
	);
	assert.equal(
		workerAssignment.workerAssignmentBlockMessage({ block_code: "ASSIGNMENT_PLAN_LOCKED" }),
		"已有报工记录，不能重做整单派工；退料释放的未完成数量需由主管重新派工。"
	);
	assert.equal(
		workerAssignment.workerAssignmentBlockMessage({
			block_code: "ASSIGNMENT_PLAN_LOCKED",
			block_message: "原派工仍有效，还剩 1 件。",
		}),
		"原派工仍有效，还剩 1 件。"
	);

	const html = workerAssignment.workOrderAssignmentContextHtml(
		{
			can_assign: false,
			work_order: {
				name: "WO-COMPLETED",
				production_item: "FG-001",
				status: "Completed",
				qty: 1,
				produced_qty: 1,
			},
			job_cards: [{
				name: "JC-COMPLETED",
				operation: "Assembly",
				workstation: "WS-1",
				for_quantity: 1,
				completed_qty: 1,
				remaining_qty: 0,
				available_reportable_qty: 0,
				assigned_total_qty: 1,
				material_status: "COMPLETED",
				can_assign: false,
				block_code: "JOB_CARD_NOT_DRAFT",
				block_message: "Worker reporting is only allowed while the Job Card is Draft.",
				display_supervisor: "supervisor@example.com",
				assignments: [{
					employee_name: "李四",
					assigned_qty: 1,
					completed_qty: 1,
					pending_qty: 0,
					remaining_qty: 0,
					supervisor: "supervisor@example.com",
					assignment_status: "Completed",
				}],
			}],
		},
		{
			translate: (message) => message,
			escapeHtml: (value) => String(value ?? "")
				.replaceAll("&", "&amp;")
				.replaceAll("<", "&lt;")
				.replaceAll(">", "&gt;")
				.replaceAll('"', "&quot;"),
			formatNumber: (value) => Number(value || 0).toFixed(2),
		}
	);
	assert.match(html, /李四/);
	assert.match(html, /原派合计.*1\.00/);
	assert.match(html, /当前分配.*1\.00/);
	assert.match(html, /已报.*1\.00.*剩余.*0\.00/);
	assert.match(html, /已完成/);
	assert.match(html, /审核主管.*supervisor@example\.com/);
	assert.match(html, /生产任务单已提交或完成，不能再派工/);
	assert.match(html, /当前不能新增或重做整单派工/);
	assert.match(html, /已有有效分配仍可继续报工/);
	assert.doesNotMatch(html, /未派工/);
});

test("released unfinished quantity is explicit and offers a bounded redispatch action", () => {
	const html = workerAssignment.workOrderAssignmentContextHtml(
		{
			can_assign: false,
			can_redispatch: true,
			work_order: { name: "WO-098", status: "In Process", qty: 99, produced_qty: 0 },
			job_cards: [{
				name: "JC-098",
				operation: "注塑外形",
				for_quantity: 99,
				completed_qty: 98,
				remaining_qty: 1,
				available_reportable_qty: 1,
				assigned_total_qty: 99,
				effective_assigned_total_qty: 98,
				released_pool_qty: 1,
				redispatchable_qty: 1,
				material_status: "READY_TO_REPORT",
				can_assign: false,
				can_redispatch: true,
				block_code: "ASSIGNMENT_PLAN_LOCKED",
				block_message: "原派工历史已锁定；已有 1 件待重新派工。",
				assignments: [{
					employee_name: "李四",
					original_assigned_qty: 89,
					released_qty: 1,
					redispatched_qty: 0,
					effective_assigned_qty: 88,
					assigned_qty: 88,
					completed_qty: 88,
					remaining_qty: 0,
					assignment_status: "Active",
				}],
			}],
		},
		{
			translate: (message) => message,
			escapeHtml: (value) => String(value ?? "")
				.replaceAll("&", "&amp;")
				.replaceAll("<", "&lt;")
				.replaceAll(">", "&gt;")
				.replaceAll('"', "&quot;"),
			formatNumber: (value) => Number(value || 0).toFixed(2),
		}
	);
	assert.match(html, /原派.*89\.00/);
	assert.match(html, /释放.*1\.00/);
	assert.match(html, /当前分配.*88\.00/);
	assert.match(html, /待重新派工.*1\.00/);
	assert.match(html, /重新派工剩余 1\.00 件/);
	assert.match(html, /只对已释放且补料覆盖的未完成数量重新派工/);
});

test("contextual assignment HTML exposes material and assignment state without trusting labels", () => {
	const html = workerAssignment.workOrderAssignmentContextHtml(
		{
			work_order: {
				name: "WO-001",
				production_item: '<img src=x onerror="alert(1)">',
				status: "Not Started",
				qty: 5,
				produced_qty: 1,
			},
			assignment_supervisor: "supervisor@example.com",
			job_cards: [
				{
					name: "JC-001",
					operation: "Assembly",
					workstation: "WS-1",
					for_quantity: 5,
					completed_qty: 1,
					remaining_qty: 4,
					available_reportable_qty: 0,
					material_status: "MATERIAL_NOT_TRANSFERRED",
					material_status_label: "等待发料",
					can_assign: true,
					assignments: [{ employee_name: "李四", report_status: "Pending Approval" }],
				},
			],
		},
		{
			translate: (message) => message,
			escapeHtml: (value) => String(value ?? "")
				.replaceAll("&", "&amp;")
				.replaceAll("<", "&lt;")
				.replaceAll(">", "&gt;")
				.replaceAll('"', "&quot;"),
			formatNumber: (value) => Number(value || 0).toFixed(2),
		}
	);
	assert.doesNotMatch(html, /<img/);
	assert.match(html, /&lt;img/);
	assert.match(html, /等待发料/);
	assert.match(html, /李四/);
	assert.match(html, /待审核/);
});

test("reporting and wage navigation use isolated roles", () => {
	const workerMetadata = JSON.parse(
		fs.readFileSync(
			path.join(
				appDirectory,
				"process_simplification/page/my_production_reporting/my_production_reporting.json"
			),
			"utf8"
		)
	);
	const reviewMetadata = JSON.parse(
		fs.readFileSync(
			path.join(
				appDirectory,
				"process_simplification/page/production_report_review/production_report_review.json"
			),
			"utf8"
		)
	);
	const activeMetadata = JSON.parse(
		fs.readFileSync(
			path.join(
				appDirectory,
				"process_simplification/page/active_production_work/active_production_work.json"
			),
			"utf8"
		)
	);
	const historyMetadata = JSON.parse(
		fs.readFileSync(
			path.join(
				appDirectory,
				"process_simplification/page/production_report_history/production_report_history.json"
			),
			"utf8"
		)
	);
	const sidebar = JSON.parse(
		fs.readFileSync(path.join(appDirectory, "workspace_sidebar/process_simplification.json"), "utf8")
	);
	const wageRate = JSON.parse(
		fs.readFileSync(
			path.join(
				appDirectory,
				"process_simplification/doctype/operation_wage_rate/operation_wage_rate.json"
			),
			"utf8"
		)
	);
	const monthlySummaryScript = fs.readFileSync(
		path.join(
			appDirectory,
			"process_simplification/doctype/monthly_worker_wage_summary/monthly_worker_wage_summary.js"
		),
		"utf8"
	);
	assert.deepEqual(workerMetadata.roles.map((row) => row.role), ["Production Worker"]);
	assert.deepEqual(activeMetadata.roles.map((row) => row.role), ["Production Worker"]);
	assert.deepEqual(historyMetadata.roles.map((row) => row.role), ["Production Worker"]);
	assert.deepEqual(new Set(reviewMetadata.roles.map((row) => row.role)), new Set([
		"Process Simplification Owner",
		"Process Simplification Production Manager",
		"System Manager",
	]));
	assert.equal(sidebar.items.filter((row) => row.link_to === "my-production-reporting").length, 1);
	assert.equal(sidebar.items.filter((row) => row.link_to === "active-production-work").length, 1);
	assert.equal(sidebar.items.filter((row) => row.link_to === "production-report-history").length, 1);
	assert.equal(sidebar.items.filter((row) => row.link_to === "production-report-review").length, 1);
	assert.equal(sidebar.items.filter((row) => row.link_to === "Operation Wage Rate").length, 1);
	assert.equal(sidebar.items.filter((row) => row.link_to === "Monthly Worker Wage Summary").length, 1);
	assert.equal(sidebar.items.filter((row) => row.link_to === "Process Simplification Settings").length, 1);
	assert.equal(sidebar.items.some((row) => row.link_to === "shop-floor"), false);
	assert.equal(sidebar.items.some((row) => row.link_type === "DocType" && /Work Report|Worker Assignment/.test(row.link_to)), false);
	assert.equal(wageRate.permissions.some((row) => row.delete), false);
	assert.match(monthlySummaryScript, /clear_primary_action/);
	assert.match(monthlySummaryScript, /clear_secondary_action/);
	assert.match(monthlySummaryScript, /dashboard\.clear_comment/);
	assert.match(monthlySummaryScript, /当前自然月尚未结束，暂不显示确认按钮/);
	assert.match(monthlySummaryScript, /工资汇总 → 确认月度汇总/);
	assert.doesNotMatch(monthlySummaryScript, /remove_menu_item/);
});


test("workers can withdraw only pending exceptions and request IDs are escaped", () => {
    const html = workerHistoryPage.workerExceptionHistoryCardHtml({name:'bad"<id>',status:'Pending Approval'}, {escapeHtml: value => String(value ?? "").replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;').replaceAll('>','&gt;')});
    assert.ok(html.includes('worker-withdraw-exception'));
    assert.ok(html.includes('data-request="bad&quot;&lt;id&gt;"'));
    for (const status of ['Awaiting Stock Entry','Completed','Withdrawn']) {
        assert.ok(!workerHistoryPage.workerExceptionHistoryCardHtml({name:'safe',status}).includes('worker-withdraw-exception'));
    }
});
