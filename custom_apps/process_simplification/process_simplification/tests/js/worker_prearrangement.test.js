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
	const scheduled = new Map(), pageEvents = new Map(), visibilityListeners = new Set(), nodes = new Map();
	const dialogs = [], writes = [], messages = [], buttons = [];
	let now = 0, nextTimer = 0, readOverride = null;
	const document = {
		hidden: false,
		addEventListener(_name, handler) { visibilityListeners.add(handler); },
		removeEventListener(_name, handler) { visibilityListeners.delete(handler); },
	};
	const button = () => ({ disabled: false, label: "结束并报工", prop(name, value) { this[name] = value; return this; },
		text(value) { if (value === undefined) return this.label; this.label = value; return this; } });
	const root = {
		0: { querySelectorAll() { return [...nodes.values()]; } },
		on(_event, selector, handler) { handlers.set(selector, handler); },
		off() { handlers.clear(); },
		find(selector) { return {
			html(value) {
				html.set(selector, value);
				if (selector === ".worker-active-list") {
					nodes.clear();
					for (const match of value.matchAll(/data-worker-timer="([^"]+)"[^>]*>([^<]*)/g)) {
						nodes.set(match[1], { dataset: { workerTimer: match[1] }, textContent: match[2] });
					}
				}
			}, toggle(value) { visible.set(selector, value); },
			prop(name, value) { properties.set(`${selector}:${name}`, value); },
		}; },
	};
	const page = {
		wrapper: { 0: {}, on(name, handler) { pageEvents.set(name, handler); }, off() { pageEvents.clear(); } },
		add_custom_menu_item() { return { addClass() {} }; },
		menu_btn_group: { addClass() {}, find() { return { attr() {} }; } },
	};
	const frappe = {
		utils: { escape_html: String }, datetime: { str_to_user: String }, session: { user: "worker@example.com" },
		container: { page: { page } },
		msgprint(message) { messages.push(message); }, show_alert() {}, confirm(_message, action) { return action(); },
		async call(request) {
			writes.push(request);
			if (/pause_work_session$/.test(request.method)) assignments = assignments.map((row) => ({ ...row, timer_paused_at: "paused", active_minutes: 2 }));
			if (/resume_work_session$/.test(request.method)) assignments = assignments.map((row) => ({ ...row, timer_paused_at: null }));
			if (/finish_work_session$|cancel_work_session$/.test(request.method)) assignments = [];
			return { message: {} };
		},
		ui: { Dialog: class {
			constructor(options) {
				this.options = options; this.values = {}; this.fields_dict = {}; this.changed = []; this.primary = button();
				for (const field of options.fields) {
					this.values[field.fieldname] = field.default;
					this.fields_dict[field.fieldname] = { df: { ...field }, $wrapper: { html() {} } };
				}
				dialogs.push(this);
			}
			get_value(name) { return this.values[name]; }
			set_value(name, value) {
				this.values[name] = value; this.changed.push(name);
				this.fields_dict[name].df.onchange?.();
			}
			set_df_property(name, property, value) { this.fields_dict[name].df[property] = value; }
			get_primary_btn() { return this.primary; }
			set_secondary_action(callback) { this.secondary = callback; }
			set_secondary_action_label() {}
			show() { this.display = true; }
			hide() { this.display = false; this.options.on_hide?.(); }
		} },
		async ps_read_page(_page, options) {
			queries.push(options);
			if (fail) throw new Error("读取失败");
			if (readOverride) return readOverride(options);
			const dashboard = { assignments, reports: [] };
			options.apply({ message: options.requests ? { dashboard, planned: { tasks } } : dashboard });
			return true;
		},
	};
	const context = vm.createContext({
		frappe, __: (text, values = []) => text.replace(/\{(\d+)\}/g, (_, index) => values[index]),
		flt: (value) => Number(value || 0), format_number: (value) => Number(value).toFixed(2),
		window: { process_simplification: {}, document }, performance: { now: () => now },
		$: (element) => element,
		setTimeout(callback) { const id = ++nextTimer; scheduled.set(id, callback); return id; },
		clearTimeout(id) { scheduled.delete(id); },
	});
	vm.runInContext(fs.readFileSync(path.resolve(__dirname, "../../public/js/worker_reporting.js"), "utf8"), context);
	context.window.process_simplification.worker_reporting.mountWorkerReportingPage({ page, root, mode });
	if (planned && mode === "queue") {
		vm.runInContext(fs.readFileSync(path.resolve(__dirname, "../../public/js/worker_prearrangement.js"), "utf8"), context);
		context.window.process_simplification.worker_prearrangement.mountWorkerPrearrangement({ page, root });
	}
	return {
		page, html, visible, properties, handlers, queries, scheduled, nodes, dialogs, writes, messages, buttons, document, visibilityListeners,
		setAssignments(value) { assignments = value; }, setFailure(value) { fail = value; }, setRead(value) { readOverride = value; },
		advance(ms) { now += ms; const pending = [...scheduled.values()]; scheduled.clear(); pending.forEach((tick) => tick()); },
		async click(selector, data = {}) {
			const target = Object.assign(button(), { data: (name) => data[name] });
			buttons.push(target);
			await handlers.get(selector)({ currentTarget: target });
			return target;
		},
		event(name, target = page.wrapper[0]) { pageEvents.get(`${name}.worker-reporting-timer`)?.({ target }); },
		async visibility(hidden) { document.hidden = hidden; await Promise.all([...visibilityListeners].map((listener) => listener())); },
		leave() { this.event("hide"); frappe.container.page = null; },
		enter() { frappe.container.page = { page }; this.event("show"); },
		remount() { context.window.process_simplification.worker_reporting.mountWorkerReportingPage({ page, root, mode }); },
	};
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

function timedAssignment(overrides = {}) {
	return {
		name: "ACTIVE", active_report: "REPORT", active_minutes: 0.006,
		active_started_at: "2026-10-10 13:26:11", job_card: "JC-1", operation: "加工",
		can_finish: true, reportable_qty: 2, wage_type: "Piecework", rate: 3,
		wage_options: [{ wage_type: "Piecework", rate: 3 }, { wage_type: "Time", rate: 30 }],
		time_manual_entry_available: true, ...overrides,
	};
}

test("active timer advances by elapsed time without more reads or whole-page renders", async () => {
	const f = mountedQueue({ mode: "active", assignments: [timedAssignment()] });
	await f.page.worker_reporting.load();
	const originalHtml = f.html.get(".worker-active-list");
	f.advance(90000);
	assert.equal(f.nodes.get("ACTIVE").textContent, "1.51 分钟");
	assert.equal(f.html.get(".worker-active-list"), originalHtml);
	assert.equal(f.queries.length, 1);
	assert.equal(f.scheduled.size, 1);
	assert.equal(f.page.worker_reporting.state.data.assignments[0].active_minutes, 0.006);
});

test("pause freezes the display and resume reanchors without counting the break", async () => {
	const f = mountedQueue({ mode: "active", assignments: [timedAssignment()] });
	await f.page.worker_reporting.load();
	f.advance(120000);
	await f.click(".worker-timer-action", { assignment: "ACTIVE", action: "pause" });
	f.advance(600000);
	assert.equal(f.nodes.get("ACTIVE").textContent, "2.00 分钟");
	assert.equal(f.scheduled.size, 0);
	await f.click(".worker-timer-action", { assignment: "ACTIVE", action: "resume" });
	f.advance(60000);
	assert.equal(f.nodes.get("ACTIVE").textContent, "3.00 分钟");
	assert.equal(f.scheduled.size, 1);
	assert.equal(f.writes.length, 2);
});

test("refresh and a new active report replace the clock anchor without adding timers", async () => {
	const f = mountedQueue({ mode: "active", assignments: [timedAssignment()] });
	await f.page.worker_reporting.load();
	f.advance(120000);
	f.setAssignments([timedAssignment({ active_report: "REPORT-2", active_minutes: 0.5 })]);
	await f.page.worker_reporting.load();
	f.page.worker_reporting.render(); f.page.worker_reporting.render();
	f.advance(60000);
	assert.equal(f.nodes.get("ACTIVE").textContent, "1.50 分钟");
	assert.equal(f.scheduled.size, 1);
	f.setAssignments([]);
	await f.page.worker_reporting.load();
	assert.equal(f.scheduled.size, 0);
});

test("finish reads a fresh snapshot and updates only the read-only minute field", async () => {
	const f = mountedQueue({ mode: "active", assignments: [timedAssignment()] });
	await f.page.worker_reporting.load();
	f.setAssignments([timedAssignment({ active_minutes: 2 })]);
	await f.click(".worker-report-action", { assignment: "ACTIVE" });
	const dialog = f.dialogs[0];
	assert.equal(dialog.values.captured_minutes, 2);
	assert.equal(dialog.fields_dict.captured_minutes.df.precision, 2);
	dialog.set_value("completed_qty", 1.5);
	dialog.set_value("wage_type", "计时 · 30.00/小时");
	dialog.set_value("reported_minutes", 45);
	dialog.changed.length = 0;
	f.advance(90000);
	assert.equal(dialog.values.captured_minutes, 3.5);
	assert.equal(f.nodes.get("ACTIVE").textContent, "3.50 分钟");
	assert.equal(dialog.values.completed_qty, 1.5);
	assert.equal(dialog.values.wage_type, "计时 · 30.00/小时");
	assert.equal(dialog.values.reported_minutes, 45);
	assert.deepEqual(dialog.changed, ["captured_minutes"]);
	assert.equal(f.queries.length, 2, "ticks must never read the dashboard");
	assert.equal(f.dialogs.length, 1);
});

test("finish waits for a merged read and suppresses a second open while loading", async () => {
	const f = mountedQueue({ mode: "active", assignments: [timedAssignment()] });
	await f.page.worker_reporting.load();
	let resolve, opts;
	f.setRead((options) => { opts = options; return new Promise((done) => { resolve = done; }); });
	const pending = f.click(".worker-report-action", { assignment: "ACTIVE" });
	await f.click(".worker-report-action", { assignment: "ACTIVE" });
	assert.equal(f.buttons[0].disabled, true);
	assert.match(f.buttons[0].label, /正在读取计时/);
	assert.equal(f.dialogs.length, 0);
	assert.equal(f.queries.length, 2);
	assert.match(opts.freeze_message, /正在读取/);
	opts.apply({ message: { assignments: [timedAssignment({ active_minutes: 4 })] } });
	resolve(true);
	const button = await pending;
	assert.equal(f.dialogs.length, 1);
	assert.equal(f.dialogs[0].values.captured_minutes, 4);
	assert.equal(button.disabled, false);
	assert.equal(button.label, "结束并报工");
});

test("failed or unapplied finish reads never open an old row and release the loading guard", async () => {
	for (const outcome of ["throw", "false", "true-without-apply", "different-report"]) {
		const f = mountedQueue({ mode: "active", assignments: [timedAssignment()] });
		await f.page.worker_reporting.load();
		f.setRead((options) => {
			if (outcome === "throw") throw new Error("读取失败");
			if (outcome === "different-report") options.apply({ message: { assignments: [timedAssignment({ active_report: "OTHER" })] } });
			return outcome !== "false";
		});
		const button = await f.click(".worker-report-action", { assignment: "ACTIVE" });
		assert.equal(f.dialogs.length, 0, outcome);
		assert.equal(button.disabled, false, outcome);
		assert.equal(f.messages.length, 1, outcome);
		assert.equal(f.writes.length, 0);
		f.setRead(null); f.setAssignments([timedAssignment()]);
		await f.page.worker_reporting.load();
		await f.click(".worker-report-action", { assignment: "ACTIVE" });
		assert.equal(f.dialogs.length, 1, "worker can retry explicitly after a read failure");
	}
});

test("leaving and returning during a finish read cannot open a delayed dialog", async () => {
	const f = mountedQueue({ mode: "active", assignments: [timedAssignment()] });
	await f.page.worker_reporting.load();
	let resolve, opts;
	f.setRead((options) => { opts = options; return new Promise((done) => { resolve = done; }); });
	const pending = f.click(".worker-report-action", { assignment: "ACTIVE" });
	f.leave(); f.enter();
	opts.apply({ message: { assignments: [timedAssignment()] } });
	resolve(true); await pending;
	assert.equal(f.dialogs.length, 0);
	assert.equal(f.writes.length, 0);
});

test("hidden pages stop ticks and visibility resumes with one fresh read without erasing inputs", async () => {
	const f = mountedQueue({ mode: "active", assignments: [timedAssignment()] });
	await f.page.worker_reporting.load();
	await f.click(".worker-report-action", { assignment: "ACTIVE" });
	const dialog = f.dialogs[0];
	dialog.set_value("completed_qty", 1.5);
	await f.visibility(true);
	assert.equal(f.scheduled.size, 0);
	f.setAssignments([timedAssignment({ active_minutes: 12 })]);
	await f.visibility(false);
	assert.equal(f.queries.length, 3);
	assert.equal(dialog.values.captured_minutes, 12);
	assert.equal(dialog.values.completed_qty, 1.5);
	assert.equal(f.scheduled.size, 1);
	assert.equal(f.dialogs.length, 1);
	await f.visibility(true); f.setFailure(true);
	await f.visibility(false);
	assert.equal(f.scheduled.size, 0);
	assert.equal(dialog.values.completed_qty, 1.5);
	assert.match(f.messages.at(-1), /读取当前计时失败/);
});

test("an externally replaced active report disables the existing dialog without erasing it", async () => {
	const f = mountedQueue({ mode: "active", assignments: [timedAssignment()] });
	await f.page.worker_reporting.load(); await f.click(".worker-report-action", { assignment: "ACTIVE" });
	const dialog = f.dialogs[0];
	dialog.set_value("completed_qty", 1);
	f.setAssignments([timedAssignment({ active_report: "OTHER" })]);
	await f.page.worker_reporting.load();
	assert.equal(dialog.primary.disabled, true);
	assert.match(dialog.fields_dict.captured_minutes.df.description, /任务状态已改变/);
	assert.equal(dialog.values.completed_qty, 1);
	await dialog.options.primary_action(dialog.values);
	assert.equal(f.writes.length, 0);
});

test("page hide and remount clean timers and listeners while child hide is ignored", async () => {
	const f = mountedQueue({ mode: "active", assignments: [timedAssignment()] });
	await f.page.worker_reporting.load();
	f.event("hide", {});
	assert.equal(f.scheduled.size, 1);
	f.leave();
	assert.equal(f.scheduled.size, 0);
	assert.equal(f.visibilityListeners.size, 0);
	const oldText = f.nodes.get("ACTIVE").textContent;
	f.advance(60000);
	assert.equal(f.nodes.get("ACTIVE").textContent, oldText);
	f.enter();
	assert.equal(f.queries.length, 1, "show leaves the read to the existing page refresh");
	await f.page.worker_reporting.load();
	assert.equal(f.visibilityListeners.size, 1);
	f.remount();
	assert.equal(f.scheduled.size, 0);
	await f.page.worker_reporting.load();
	assert.equal(f.scheduled.size, 1);
	assert.equal(f.visibilityListeners.size, 1);
	f.page.worker_reporting.dispose();
	assert.equal(f.scheduled.size, 0);
	assert.equal(f.visibilityListeners.size, 0);
});

test("a disposed page instance cannot apply a delayed response over the replacement instance", async () => {
	const f = mountedQueue({ mode: "active", assignments: [timedAssignment()] });
	await f.page.worker_reporting.load();
	let resolve, oldOptions;
	f.setRead((options) => { oldOptions = options; return new Promise((done) => { resolve = done; }); });
	const pending = f.page.worker_reporting.load();
	f.remount(); f.setRead(null);
	f.setAssignments([timedAssignment({ active_report: "NEW", active_minutes: 3 })]);
	await f.page.worker_reporting.load();
	const html = f.html.get(".worker-active-list");
	assert.equal(oldOptions.apply({ message: { assignments: [] } }), false);
	resolve(false); await pending;
	assert.equal(f.html.get(".worker-active-list"), html);
	assert.equal(f.page.worker_reporting.state.data.assignments[0].active_report, "NEW");
	assert.equal(f.scheduled.size, 1);
});

test("visibility recovery stops the display if an applied stale response is reported unsuccessful", async () => {
	const f = mountedQueue({ mode: "active", assignments: [timedAssignment()] });
	await f.page.worker_reporting.load(); await f.visibility(true);
	f.setRead((options) => {
		options.apply({ message: { assignments: [timedAssignment({ active_minutes: 8 })], _refresh_stale: true } });
		return false;
	});
	await f.visibility(false);
	assert.equal(f.scheduled.size, 0);
	assert.match(f.messages.at(-1), /计时状态未能更新/);
	const text = f.nodes.get("ACTIVE").textContent;
	f.advance(60000);
	assert.equal(f.nodes.get("ACTIVE").textContent, text);
});

test("dialog close detaches its clock while finish preserves the server-authoritative payload", async () => {
	const f = mountedQueue({ mode: "active", assignments: [timedAssignment()] });
	await f.page.worker_reporting.load(); await f.click(".worker-report-action", { assignment: "ACTIVE" });
	const old = f.dialogs[0], previous = old.values.captured_minutes;
	old.hide(); f.advance(60000);
	assert.equal(old.values.captured_minutes, previous);
	assert.equal(f.scheduled.size, 1);
	await f.click(".worker-report-action", { assignment: "ACTIVE" });
	const dialog = f.dialogs[1];
	dialog.set_value("completed_qty", 1.5);
	dialog.set_value("wage_type", "计时 · 30.00/小时");
	dialog.set_value("reported_minutes", 45);
	f.advance(90000);
	await dialog.options.primary_action(dialog.values);
	assert.equal(f.writes.length, 1);
	assert.deepEqual(Object.keys(f.writes[0].args).sort(), ["completed_qty", "report", "reported_minutes", "request_id", "wage_type"]);
	assert.equal(f.writes[0].args.reported_minutes, 45);
	assert.equal(f.writes[0].args.wage_type, "Time");
	assert.equal(f.writes[0].args.completed_qty, 1.5);
	assert.equal(f.scheduled.size, 0);
	assert.equal(dialog.display, false);
});

test("canceling a session stops its display without adding timing fields to the request", async () => {
	const f = mountedQueue({ mode: "active", assignments: [timedAssignment()] });
	await f.page.worker_reporting.load(); await f.click(".worker-report-action", { assignment: "ACTIVE" });
	f.dialogs[0].secondary();
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(f.writes.length, 1);
	assert.match(f.writes[0].method, /cancel_work_session$/);
	assert.deepEqual(Object.keys(f.writes[0].args), ["report"]);
	assert.equal(f.scheduled.size, 0);
	assert.equal(f.dialogs[0].display, false);
});
