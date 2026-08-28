const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const appDirectory = path.resolve(__dirname, "../..");
const workerPage = require(path.join(
	appDirectory,
	"process_simplification/page/my_production_reporting/my_production_reporting.js"
));
const reviewPage = require(path.join(
	appDirectory,
	"process_simplification/page/production_report_review/production_report_review.js"
));

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
		workerPage.workReportButtonMeta({ can_start: false, block_code: "PENDING_REPORT" }),
		{ action: null, label: "待主管审核", disabled: true }
	);
	assert.deepEqual(
		workerPage.workReportButtonMeta({ can_start: false, block_code: "RATE_MISSING" }),
		{ action: null, label: "缺少计价规则", disabled: true }
	);
});

test("piecework and time wage previews use different quantities", () => {
	assert.equal(workerPage.workReportAmount("Piecework", 3, 120, 5), 15);
	assert.equal(workerPage.workReportAmount("Time", 3, 90, 20), 30);
});

test("worker and review pages strip database microseconds before Frappe datetime formatting", () => {
	const value = "2026-08-27 23:31:23.424982";
	assert.equal(workerPage.normalizeFrappeDateTime(value), "2026-08-27 23:31:23");
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
	assert.deepEqual(new Set(reviewMetadata.roles.map((row) => row.role)), new Set([
		"Production Supervisor",
		"System Manager",
	]));
	assert.equal(sidebar.items.filter((row) => row.link_to === "my-production-reporting").length, 1);
	assert.equal(sidebar.items.filter((row) => row.link_to === "production-report-review").length, 1);
	assert.equal(sidebar.items.filter((row) => row.link_to === "Operation Wage Rate").length, 1);
	assert.equal(sidebar.items.filter((row) => row.link_to === "Monthly Worker Wage Summary").length, 1);
	assert.equal(sidebar.items.some((row) => row.link_to === "shop-floor"), false);
	assert.equal(sidebar.items.some((row) => row.link_type === "DocType" && /Work Report|Worker Assignment/.test(row.link_to)), false);
	assert.equal(wageRate.permissions.some((row) => row.delete), false);
	assert.match(monthlySummaryScript, /clear_primary_action/);
	assert.match(monthlySummaryScript, /clear_secondary_action/);
	assert.doesNotMatch(monthlySummaryScript, /remove_menu_item/);
});
