const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {
	workerPlannedStatus,
	workerPlannedCardHtml,
	workerPlannedBounded,
	createWorkerPrearrangementController,
} = require("../../public/js/worker_prearrangement.js");

function task(overrides = {}) {
	return {
		name: "PLAN-1", revision: 3, job_card: "JC-1", work_order: "WO-1", operation: "切割",
		item_name: "支架", for_quantity: 100, planned_qty: 40, can_start: true,
		...overrides,
	};
}

function fixture(overrides = {}) {
	const calls = { reads: 0, starts: [], dashboards: 0, started: [] };
	let tasks = [task()];
	const controller = createWorkerPrearrangementController({
		read: async () => { calls.reads++; return { tasks: [...tasks] }; },
		start: async (args) => {
			calls.starts.push(args);
			return { status: "started", plan: args.plan, assignment: "ASSIGN-1", report: { name: "REPORT-1" } };
		},
		dashboard: async () => { calls.dashboards++; return { assignments: [] }; },
		onStarted: (result) => calls.started.push(result),
		requestId: () => "START-REQUEST-1",
		...overrides,
	});
	return { controller, calls, setTasks: (value) => { tasks = value; } };
}

test("worker planned cards distinguish readiness without claiming stock or reportable quantity", () => {
	assert.equal(workerPlannedStatus(task()).label, "可以开始");
	assert.equal(workerPlannedStatus(task({ can_start: false, block_code: "PREVIOUS_OPERATION_PENDING" })).label, "等待前序");
	assert.equal(workerPlannedStatus(task({ can_start: false, block_code: "MATERIAL_NOT_FULLY_ISSUED" })).label, "等待发料");
	assert.equal(workerPlannedStatus(task({ can_start: false, block_code: "PLAN_STALE" })).label, "需处理");
	const html = workerPlannedCardHtml(task());
	assert.match(html, /本人安排数量<\/small><strong>40<\/strong>/);
	assert.doesNotMatch(html, /当前可报|100|申请退料|结束并报工/);
	assert.match(html, /开始后才计时/);
});

test("blocked plans explain their state and offer no start action", () => {
	const html = workerPlannedCardHtml(task({
		can_start: false, block_code: "PREVIOUS_OPERATION_PENDING", block_message: "等待前序审核", notes: "<script>alert(1)</script>",
	}));
	assert.match(html, /等待前序审核/);
	assert.match(html, /&lt;script&gt;/);
	assert.doesNotMatch(html, /worker-planned-start|<script>/);
});

test("reads do not activate plans and blocked tasks cannot start", async () => {
	const f = fixture();
	f.setTasks([task({ can_start: false })]);
	await f.controller.load();
	await f.controller.load();
	assert.equal(await f.controller.begin("PLAN-1"), false);
	assert.equal(f.calls.reads, 2);
	assert.deepEqual(f.calls.starts, []);
	assert.deepEqual(f.calls.started, []);
});

test("one click carries the exact plan revision and removes a confirmed started plan", async () => {
	const f = fixture();
	await f.controller.load();
	assert.equal(await f.controller.begin("PLAN-1"), true);
	assert.deepEqual(f.calls.starts, [{ plan: "PLAN-1", expected_revision: 3, request_id: "START-REQUEST-1" }]);
	assert.equal(f.calls.started.length, 1);
	assert.deepEqual(f.controller.state.tasks, []);
	assert.equal(f.controller.state.busy, false);
});

test("a second click cannot create another start while the original is in flight", async () => {
	let resolve, requests = 0;
	const f = fixture({ start: () => { requests++; return new Promise((done) => { resolve = done; }); } });
	await f.controller.load();
	const first = f.controller.begin("PLAN-1");
	assert.equal(await f.controller.begin("PLAN-1"), false);
	assert.equal(requests, 1);
	resolve({ status: "started", plan: "PLAN-1", assignment: "ASSIGN-1", report: "REPORT-1" });
	assert.equal(await first, true);
});

test("a lost response is recovered from the worker's real active report", async () => {
	let starts = 0;
	const f = fixture({
		start: async () => { starts++; throw { status: 0, statusText: "timeout" }; },
		dashboard: async () => ({ assignments: [{ name: "ASSIGN-1", job_card: "JC-1", active_report: "REPORT-1" }] }),
	});
	await f.controller.load();
	assert.equal(await f.controller.begin("PLAN-1"), true);
	assert.equal(starts, 1);
	assert.equal(f.calls.started[0].recovered, true);
	assert.equal(f.controller.state.actions.get("PLAN-1").status, "started");
});

test("an unknown outcome is retained when the plan disappears and never claimed as a failure", async () => {
	const f = fixture({ start: async () => { throw { status: 503 }; } });
	await f.controller.load();
	assert.equal(await f.controller.begin("PLAN-1"), false);
	f.setTasks([]);
	await f.controller.load();
	assert.equal(f.controller.state.tasks.length, 1);
	const action = f.controller.state.actions.get("PLAN-1");
	assert.equal(action.status, "uncertain");
	assert.doesNotMatch(action.message, /失败/);
	assert.equal(await f.controller.begin("PLAN-1"), false);
	assert.deepEqual(f.calls.started, []);
	const html = workerPlannedCardHtml(task(), { action, unresolved: true });
	assert.match(html, /worker-planned-start[^>]*disabled/);
	assert.match(html, /worker-planned-check[^>]*>核实结果/);
});

test("a formal assignment without an active report is not evidence that timing started", async () => {
	const f = fixture({
		start: async () => { throw { status: 0 }; },
		dashboard: async () => ({ assignments: [{ name: "ASSIGN-1", job_card: "JC-1", active_report: null }] }),
	});
	await f.controller.load();
	assert.equal(await f.controller.begin("PLAN-1"), false);
	assert.equal(f.controller.state.actions.get("PLAN-1").status, "uncertain");
	assert.deepEqual(f.calls.started, []);
});

test("a later ordinary refresh resolves uncertainty from actual active work without starting again", async () => {
	let starts = 0;
	const f = fixture({ start: async () => { starts++; throw { status: 0 }; } });
	await f.controller.load();
	await f.controller.begin("PLAN-1");
	f.controller.accept({ tasks: [] });
	f.controller.reconcile({ assignments: [{ name: "ASSIGN-1", job_card: "JC-1", active_report: "REPORT-1" }] });
	assert.equal(starts, 1);
	assert.equal(f.controller.state.tasks.length, 0);
	assert.equal(f.controller.state.actions.get("PLAN-1").status, "started");
	assert.match(f.controller.state.notice, /已经开始计时/);
	assert.deepEqual(f.calls.started, [], "background refresh must not navigate away from what the worker is reading");
});

test("a rejected server recheck refreshes the stale plan and preserves an explanation", async () => {
	const f = fixture({ start: async () => { throw { status: 417, responseJSON: { _server_messages: JSON.stringify([JSON.stringify({ message: "工单已停止" })]) } }; } });
	await f.controller.load();
	f.setTasks([task({ can_start: false, block_code: "WORK_ORDER_UNAVAILABLE", block_message: "工单已停止" })]);
	assert.equal(await f.controller.begin("PLAN-1"), false);
	assert.equal(f.controller.state.tasks[0].can_start, false);
	assert.equal(f.controller.state.actions.get("PLAN-1").message, "工单已停止");
	assert.deepEqual(f.calls.started, []);
});

test("a failed read cannot enable a stale ready button and a subsequent refresh can recover", async () => {
	let fail = false;
	const f = fixture({ read: () => { if (fail) throw new Error("离线"); return { tasks: [task()] }; } });
	await f.controller.load();
	fail = true;
	assert.equal(await f.controller.load(), false);
	assert.equal(await f.controller.begin("PLAN-1"), false);
	fail = false;
	assert.equal(await f.controller.load(), true);
	assert.equal(await f.controller.begin("PLAN-1"), true);
});

test("waiting on a start response is bounded without pretending the server aborted", async () => {
	let expire;
	const pending = workerPlannedBounded(new Promise(() => {}), 60000, { schedule: (callback) => { expire = callback; }, cancel() {} });
	expire();
	await assert.rejects(pending, (error) => error.name === "TimeoutError" && error.status === 0);
});

function mountedQueue({ planned = true, fail = false, assignments = [], tasks = [task()], mode = "queue" } = {}) {
	const html = new Map(), visible = new Map(), properties = new Map(), handlers = new Map(), queries = [];
	const root = {
		on(_event, selector, handler) { handlers.set(selector, handler); },
		find(selector) { return {
			html(value) { html.set(selector, value); }, toggle(value) { visible.set(selector, value); },
			prop(name, value) { properties.set(`${selector}:${name}`, value); },
		}; },
	};
	const page = {
		add_custom_menu_item() { return { addClass() {} }; },
		menu_btn_group: { addClass() {}, find() { return { attr() {} }; } },
	};
	const frappe = {
		utils: { escape_html: String }, datetime: { str_to_user: String }, session: { user: "worker@example.com" },
		container: { page: { page } },
		async ps_read_page(_page, options) {
			queries.push(options);
			if (fail) throw new Error("读取失败");
			const dashboard = { assignments, reports: [] };
			options.apply({ message: options.requests ? { dashboard, planned: { tasks } } : dashboard });
			return true;
		},
	};
	const context = vm.createContext({
		frappe, __: (text) => text, flt: (value) => Number(value || 0), format_number: String,
		window: { process_simplification: {} }, setTimeout, clearTimeout,
	});
	vm.runInContext(fs.readFileSync(path.resolve(__dirname, "../../public/js/worker_reporting.js"), "utf8"), context);
	context.window.process_simplification.worker_reporting.mountWorkerReportingPage({ page, root, mode });
	if (planned && mode === "queue") {
		vm.runInContext(fs.readFileSync(path.resolve(__dirname, "../../public/js/worker_prearrangement.js"), "utf8"), context);
		context.window.process_simplification.worker_prearrangement.mountWorkerPrearrangement({ page, root });
	}
	return { page, html, visible, properties, handlers, queries };
}

test("queue refresh reads formal and planned tasks as one tracked page batch", async () => {
	const f = mountedQueue();
	await f.page.worker_reporting.load();
	assert.equal(f.queries.length, 1);
	assert.equal(f.queries[0].requests.dashboard.method, "process_simplification.api.production_reporting.get_my_dashboard");
	assert.equal(f.queries[0].requests.planned.method, "process_simplification.api.production_prearrangement.get_my_planned_tasks");
	assert.match(f.html.get(".worker-reporting-summary"), /待开始任务 1 项/);
	assert.match(f.html.get(".worker-assignment-list"), /本人安排数量/);
	assert.equal(f.html.has(".worker-planned-assignments"), false);
	assert.notEqual(f.visible.get(".worker-current-assignments"), false);
});

test("a planned section read failure keeps start actions disabled and surfaces the reason", async () => {
	const f = mountedQueue({ fail: true });
	f.page.worker_prearrangement.accept({ tasks: [task()] });
	await assert.rejects(f.page.worker_reporting.load(), /读取失败/);
	assert.match(f.html.get(".worker-task-notices"), /读取失败/);
	assert.match(f.html.get(".worker-assignment-list"), /worker-planned-start[^>]*disabled/);
});

test("pages without the optional helper keep the original dashboard read contract", async () => {
	const f = mountedQueue({ planned: false, assignments: [{ name: "FORMAL", can_start: true }] });
	await f.page.worker_reporting.load();
	assert.equal(f.queries[0].method, "process_simplification.api.production_reporting.get_my_dashboard");
	assert.equal(f.queries[0].requests, undefined);
	assert.match(f.html.get(".worker-assignment-list"), /data-assignment="FORMAL"/);
	assert.equal(f.page.worker_reporting.canStartTask("formal", "FORMAL"), true);
});

test("mixed tasks stay in one queue when either source rerenders", async () => {
	const f = mountedQueue({
		assignments: [{ name: "FORMAL", job_card: "JC-FORMAL", can_start: true }],
		tasks: [task({ can_start: false, block_code: "MATERIAL_NOT_FULLY_ISSUED" })],
	});
	await f.page.worker_reporting.load();
	const first = f.html.get(".worker-assignment-list");
	assert.ok(first.indexOf('data-assignment="FORMAL"') < first.indexOf('data-plan="PLAN-1"'));
	assert.match(f.html.get(".worker-reporting-summary"), /待开始任务 2 项/);
	f.page.worker_prearrangement.accept({ tasks: [task()] });
	f.page.worker_reporting.render();
	const next = f.html.get(".worker-assignment-list");
	assert.equal((next.match(/data-task-group="ready"/g) || []).length, 1);
	assert.equal((next.match(/<article/g) || []).length, 2);
	assert.match(next, /worker-planned-start/);
	assert.match(next, /worker-report-action/);
	assert.ok(f.handlers.has(".worker-planned-start") && f.handlers.has(".worker-report-action"));
	assert.doesNotMatch(next, /已为我安排|待开始的派工/);
});

test("active or paused work keeps its shortcut and prevents either source from starting", async () => {
	const f = mountedQueue({ assignments: [
		{ name: "ACTIVE", job_card: "JC-ACTIVE", active_report: "REPORT", timer_paused_at: "2026-10-05" },
		{ name: "FORMAL", job_card: "JC-FORMAL", can_start: true },
	] });
	await f.page.worker_reporting.load();
	assert.match(f.html.get(".worker-active-shortcut"), /正在做 1 项/);
	assert.doesNotMatch(f.html.get(".worker-assignment-list"), /data-task-group="ready"|worker-planned-start/);
	assert.match(f.html.get(".worker-assignment-list"), /worker-report-action[^>]*disabled/);
	assert.equal(f.page.worker_reporting.canStartTask("formal", "FORMAL"), false);
	assert.equal(f.page.worker_reporting.canStartTask("planned", "PLAN-1"), false);
});

test("uncertain planned activation suppresses duplicate formal start and disables other starts", async () => {
	const f = mountedQueue({ assignments: [
		{ name: "FORMAL-SAME", job_card: "JC-1", can_start: true },
		{ name: "FORMAL-OTHER", job_card: "JC-2", can_start: true },
	] });
	await f.page.worker_reporting.load();
	f.page.worker_prearrangement.state.actions.set("PLAN-1", { task: task(), status: "uncertain", message: "正在核实" });
	f.page.worker_reporting.render();
	const html = f.html.get(".worker-assignment-list");
	assert.doesNotMatch(html, /data-assignment="FORMAL-SAME"/);
	assert.match(html, /worker-planned-check/);
	assert.match(html, /worker-report-action[^>]*FORMAL-OTHER[^>]*disabled/);
	assert.equal(f.page.worker_reporting.canStartTask("formal", "FORMAL-OTHER"), false);
	f.page.worker_prearrangement.loading();
	assert.match(f.html.get(".worker-assignment-list"), /worker-planned-check/);
});

test("active work page keeps its original read and finish timer actions", async () => {
	const f = mountedQueue({ mode: "active", assignments: [{ name: "ACTIVE", active_report: "REPORT", timer_paused_at: "2026-10-05" }] });
	await f.page.worker_reporting.load();
	assert.equal(f.queries[0].requests, undefined);
	assert.match(f.html.get(".worker-active-list"), /结束并报工/);
	assert.match(f.html.get(".worker-active-list"), /继续计时/);
	assert.equal(f.html.has(".worker-assignment-list"), false);
});

test("refresh disables starts in place before the shared reader captures expanded task details", async () => {
	const f = mountedQueue();
	await f.page.worker_reporting.load();
	f.html.set(".worker-assignment-list", "expanded-task-details-before-refresh");
	f.page.worker_prearrangement.loading();
	assert.equal(f.html.get(".worker-assignment-list"), "expanded-task-details-before-refresh");
	assert.equal(f.properties.get(".worker-report-action, .worker-planned-start:disabled"), true);
	assert.equal(f.page.worker_reporting.canStartTask("planned", "PLAN-1"), false);
	f.page.worker_prearrangement.accept({ tasks: [task()] });
	assert.match(f.html.get(".worker-assignment-list"), /worker-planned-start/);
	assert.equal(f.page.worker_reporting.canStartTask("planned", "PLAN-1"), true);
});
