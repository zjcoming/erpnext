const test = require("node:test");
const assert = require("node:assert/strict");
const pool = require("../../public/js/operation_dispatch_pool.js");
const workbench = require("../../process_simplification/page/production_workbench/production_workbench.js");

const card = (name, overrides = {}) => ({
	job_card: name, work_order: `WO-${name}`, company: "Factory A", operation: "切割", operation_id: `OP-${name}`,
	for_quantity: 10, completed_qty: 0, remaining_qty: 10, stock_uom: "件", can_assign_first: true,
	has_assignment_history: false, assignments: [], assignment_supervisor: "manager@example.test", ...overrides,
});
const page = (rows) => ({ rows, companies: ["Factory A"], pagination: { page: 1, page_length: 20, total_count: rows.length, total_pages: 1, has_more: false } });
const plan = (...names) => Object.fromEntries(names.map((name) => [name, [{ employee: "EMP-1", assigned_qty: 10 }]]));
const deferred = () => {
	let resolve, reject;
	const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
	return { promise, resolve, reject };
};
const setup = (options = {}) => {
	const calls = [];
	const controller = pool.createOperationDispatchController({
		read: async () => page([card("A"), card("B")]),
		assign: async (args) => { calls.push(args); return { status: "assigned", job_card: args.job_card }; },
		recheck: async () => ({ job_cards: [] }), ...options,
	});
	return { controller, calls };
};

test("only cards eligible for first dispatch can be selected", () => {
	assert.equal(pool.operationDispatchSelectable(card("A")), true);
	for (const facts of [
		{ can_assign_first: false }, { has_assignment_history: true }, { has_work_history: true },
		{ has_movement_history: true }, { assignments: [{ employee: "EMP-1" }] },
		{ for_quantity: 0 }, { for_quantity: Infinity }, { job_card: "" },
	]) assert.equal(pool.operationDispatchSelectable(card("A", facts)), false);
});

test("select-page includes only this page and one company with a hard cap", async () => {
	const rows = Array.from({ length: 25 }, (_, i) => card(String(i)));
	rows.push(card("OTHER", { company: "Other" }), card("BLOCKED", { can_assign_first: false }));
	const { controller } = setup({ read: async () => page(rows) });
	await controller.load();
	controller.selectPage(true);
	assert.equal(controller.state.selected.size, 20);
	assert.equal(controller.state.selected.has("OTHER"), false);
	assert.equal(controller.state.selected.has("BLOCKED"), false);
	assert.match(controller.select("21", true), /20/);
	controller.selectPage(false);
	controller.select("0", true);
	assert.match(controller.select("OTHER", true), /同一公司/);
	assert.equal(controller.state.selected.size, 1);
});

test("refresh, filters and paging clear selection before read returns", async () => {
	const pending = deferred();
	let count = 0;
	const { controller } = setup({ read: async (args) => ++count === 1 ? page([card("A")]) : pending.promise });
	await controller.load();
	controller.select("A", true);
	const loading = controller.load({ filters: { operation: "焊接" }, page: 2 });
	assert.equal(controller.state.selected.size, 0);
	assert.equal(controller.state.loading, true);
	controller.select("A", true);
	assert.equal(controller.state.selected.size, 0);
	pending.resolve(page([card("B")]));
	await loading;
	assert.deepEqual(controller.state.rows.map((row) => row.job_card), ["B"]);
});

test("out-of-order responses and errors cannot replace the newest filter result", async () => {
	const first = deferred();
	const second = deferred();
	let reads = 0;
	const { controller } = setup({ read: () => (++reads === 1 ? first.promise : second.promise) });
	const oldLoad = controller.load({ filters: { search: "old" } });
	const newLoad = controller.load({ filters: { search: "new" } });
	second.resolve(page([card("NEW")]));
	await newLoad;
	first.reject(new Error("stale failure"));
	await oldLoad;
	assert.equal(controller.state.rows[0].job_card, "NEW");
	assert.equal(controller.state.error, "");
	assert.equal(controller.state.loading, false);
});

test("typing invalidates both selected cards and pending list response", async () => {
	const pending = deferred();
	const { controller } = setup({ read: () => pending.promise });
	const request = controller.load();
	controller.invalidate();
	pending.resolve(page([card("STALE")]));
	assert.equal(await request, false);
	assert.equal(controller.state.rows.length, 0);
	assert.equal(controller.state.loading, true);
	controller.selectPage(true);
	assert.equal(controller.state.selected.size, 0);
});

test("every plan is validated before the first write; fractional allocations retain exact quantity", async () => {
	assert.equal(pool.operationDispatchPlanError([{ employee: "A", assigned_qty: 0.1 }, { employee: "B", assigned_qty: 0.2 }], 0.3), "");
	for (const rows of [[], [{ employee: "", assigned_qty: 10 }], [{ employee: "A", assigned_qty: NaN }],
		[{ employee: "A", assigned_qty: 5 }, { employee: "A", assigned_qty: 5 }], [{ employee: "A", assigned_qty: 9 }]]) {
		assert.ok(pool.operationDispatchPlanError(rows, 10));
	}
	const { controller, calls } = setup();
	await controller.load(); controller.selectPage(true);
	await assert.rejects(controller.execute({ ...plan("A"), B: [{ employee: "EMP-2", assigned_qty: 9 }] }));
	assert.equal(calls.length, 0);
	assert.equal(controller.state.running, false);
});

test("batch writes are sequential and send original identity and quantity guards", async () => {
	const pending = deferred();
	const calls = [];
	const { controller } = setup({ assign: async (args) => {
		calls.push(args);
		if (args.job_card === "A") await pending.promise;
		return { status: "assigned", job_card: args.job_card };
	} });
	await controller.load(); controller.selectPage(true);
	const execution = controller.execute(plan("A", "B"));
	assert.equal(calls.length, 1);
	assert.equal(await controller.execute(plan("A", "B")), false);
	assert.equal(await controller.load(), false);
	assert.deepEqual(calls[0], {
		job_card: "A", assignments: [{ employee: "EMP-1", assigned_qty: 10, notes: "" }], expected_qty: 10,
		expected_work_order: "WO-A", expected_operation_id: "OP-A", supervisor: "manager@example.test",
	});
	pending.resolve(); await execution;
	assert.deepEqual(calls.map((args) => args.job_card), ["A", "B"]);
	assert.deepEqual(controller.state.results.map((row) => row.status), ["assigned", "assigned"]);
	assert.equal(controller.state.selected.size, 0);
});

test("one business failure does not roll back or obscure other successful cards", async () => {
	const { controller } = setup({ assign: async (args) => {
		if (args.job_card === "A") throw { status: 417, responseJSON: { message: "已经由另一名主管安排" } };
		return { status: "assigned", job_card: args.job_card };
	} });
	await controller.load(); controller.selectPage(true);
	await controller.execute(plan("A", "B"));
	assert.deepEqual(controller.state.results.map((row) => row.status), ["failed", "assigned"]);
	assert.match(controller.state.results[0].message, /另一名主管/);
});

test("a confirmed success updates the visible source card and people without another read", async () => {
	let reads = 0;
	const assignments = [{ name: "ASSIGN-A", employee: "EMP-1", employee_name: "张三", assigned_qty: 10 }];
	const { controller } = setup({
		read: async () => { reads++; return page([card("A"), card("B")]); },
		assign: async (args) => ({ status: "assigned", job_card: args.job_card, assignments }),
	});
	await controller.load(); controller.select("A", true);
	await controller.execute(plan("A"));
	const source = controller.state.rows.find((row) => row.job_card === "A");
	assert.equal(source.has_assignment_history, true);
	assert.equal(source.can_assign_first, false);
	assert.deepEqual(source.assignments, assignments);
	assert.equal(source.block_code, "EXISTING_HISTORY");
	assert.match(source.block_message, /原派工入口/);
	const html = pool.operationDispatchRowHtml(source);
	assert.match(html, /已有派工历史/);
	assert.match(html, /张三/);
	assert.equal(reads, 1);
	assert.equal(pool.operationDispatchSelectable(controller.state.rows.find((row) => row.job_card === "B")), true);
});

test("stop waits for the current request and leaves subsequent cards untouched", async () => {
	const pending = deferred();
	const calls = [];
	const { controller } = setup({ assign: async (args) => { calls.push(args.job_card); await pending.promise; return { status: "assigned", job_card: args.job_card }; } });
	await controller.load(); controller.selectPage(true);
	const execution = controller.execute(plan("A", "B"));
	controller.stop();
	pending.resolve(); await execution;
	assert.deepEqual(calls, ["A"]);
	assert.deepEqual(controller.state.results.map((row) => row.status), ["assigned", "skipped"]);
});

test("leaving the view invalidates reads and stops not-yet-submitted writes", async () => {
	const pending = deferred();
	const { controller } = setup({ assign: async (args) => { await pending.promise; return { status: "assigned", job_card: args.job_card }; } });
	await controller.load(); controller.selectPage(true);
	const execution = controller.execute(plan("A", "B"));
	controller.deactivate(); pending.resolve(); await execution;
	assert.deepEqual(controller.state.results.map((row) => row.status), ["assigned", "skipped"]);
});

test("timeout performs readback and never attributes an existing plan to this request", async () => {
	let writes = 0, reads = 0;
	const { controller } = setup({
		assign: async () => { writes++; throw { status: 0, statusText: "timeout" }; },
		recheck: async () => { reads++; return { job_cards: [{ name: "A", assignments: [{ employee: "EMP-1", assigned_qty: 10 }] }] }; },
	});
	await controller.load(); controller.select("A", true);
	await controller.execute(plan("A"));
	assert.equal(writes, 1); assert.equal(reads, 1);
	assert.equal(controller.state.results[0].status, "exists");
	assert.match(controller.state.results[0].message, /无法确认/);
	await controller.recheckResult("A");
	assert.equal(writes, 1); assert.equal(reads, 2);
});

test("unresolved timeout stops the batch and remains unselectable after list refresh", async () => {
	let writes = 0;
	const { controller } = setup({ assign: async () => { writes++; throw { status: 504 }; } });
	await controller.load(); controller.selectPage(true);
	await controller.execute(plan("A", "B"));
	assert.equal(writes, 1);
	assert.deepEqual(controller.state.results.map((row) => row.status), ["uncertain", "skipped"]);
	await controller.load();
	assert.equal(controller.state.results[0].status, "uncertain");
	assert.match(controller.select("A", true), /不能首次派工/);
	assert.equal(controller.select("B", true), "");
});

test("an unresolved card remains guarded and visible after a different batch completes", async () => {
	const { controller } = setup({ assign: async (args) => {
		if (args.job_card === "A") throw { status: 504 };
		return { status: "assigned", job_card: args.job_card };
	} });
	await controller.load(); controller.select("A", true);
	await controller.execute(plan("A"));
	await controller.load(); controller.select("B", true);
	await controller.execute(plan("B"));
	await controller.load();
	assert.deepEqual(controller.state.results.map((row) => [row.job_card, row.status]), [["A", "uncertain"], ["B", "assigned"]]);
	assert.match(controller.select("A", true), /不能首次派工/);
});

test("a hung call is bounded even if Frappe ignores timeout; late commits are not success", async () => {
	let fire, cleared = 0;
	const pending = deferred();
	const request = pool.operationDispatchBounded(pending.promise, 50, {
		schedule: (callback, delay) => { assert.equal(delay, 50); fire = callback; return "timer"; },
		cancel: (timer) => { assert.equal(timer, "timer"); cleared++; },
	});
	const rejected = assert.rejects(request, (error) => error.name === "TimeoutError" && pool.operationDispatchUncertainError(error));
	fire(); await rejected;
	pending.resolve({ status: "assigned" });
	await Promise.resolve();
	await assert.rejects(request, { name: "TimeoutError" });
	assert.equal(cleared, 1);
});

test("a completed call clears its bounded-wait timer", async () => {
	let cleared = false;
	assert.equal(await pool.operationDispatchBounded(Promise.resolve("done"), 10, {
		schedule: () => 1, cancel: () => { cleared = true; },
	}), "done");
	assert.equal(cleared, true);
});

test("readback failure and unexpected success response remain uncertain", async () => {
	const { controller } = setup({ assign: async () => ({ status: "assigned", job_card: "OTHER" }), recheck: async () => { throw new Error("offline"); } });
	await controller.load(); controller.select("A", true);
	await controller.execute(plan("A"));
	assert.equal(controller.state.results[0].status, "uncertain");
	await controller.recheckResult("A");
	assert.equal(controller.state.results[0].status, "uncertain");
});

test("selection identity detects same-card quantity, operation and source changes", () => {
	const original = pool.operationDispatchSelectionKey([card("A")]);
	for (const facts of [{ for_quantity: 11 }, { operation_id: "NEW" }, { work_order: "OTHER" }, { company: "Other" }]) {
		assert.notEqual(pool.operationDispatchSelectionKey([card("A", facts)]), original);
	}
});

test("row, preview and result renderers escape untrusted fields without combining quantities", () => {
	const attack = '"><img src=x onerror=alert(1)>';
	const row = card("A", { item_name: attack, description: '<p>直径 20 mm</p><script>alert(1)</script>', sales_order: attack, stock_uom: attack, block_message: attack, assignments: [{ employee_name: attack }] });
	const html = pool.operationDispatchRowHtml(row);
	assert.ok(!html.includes('<img src=x'));
	assert.ok(!html.includes('<script>'));
	assert.ok(html.includes('&lt;img'));
	assert.ok(html.includes('直径 20 mm'));
	const preview = pool.operationDispatchPreviewHtml([row], { A: [{ employee: attack, assigned_qty: 10, notes: attack }] });
	assert.ok(!preview.includes('<img src=x'));
	const result = pool.operationDispatchResultsHtml([{ ...row, status: "failed", message: attack }]);
	assert.ok(!result.includes('<img src=x'));
	assert.match(result, /原派工/);
});

test("opening an order deep link restores the original view before loading it", () => {
	const calls = [];
	const state = { filters: {}, pagination: { page: 2 }, expandedDemands: new Set() };
	workbench.refreshProductionOverview({ production_workbench: { state, showOrderView: () => calls.push("orders"), loadOverview: () => calls.push("load") } }, "SO-ROW");
	assert.deepEqual(calls, ["orders", "load"]);
	assert.equal(state.filters.demandKey, "SO-ROW");
});

function browserHarness() {
	class Element {
		constructor(elements = []) { this.elements = elements; this.children = new Map(); this.handlers = []; this.properties = {}; this.value = ""; this.dataset = {}; }
		addClass() { return this; }
		html(value) { if (value === undefined) return this.content; this.content = value; return this; }
		text(value) { this.content = value; return this; }
		prop(key, value) { this.properties[key] = value; return this; }
		val(value) { if (value === undefined) return this.value; this.value = value; return this; }
		find(selector) {
			if (selector === "[data-pool-filter]") return new Element(["company", "operation", "search", "assignment_state"].map((key) => this.find(`[data-pool-filter="${key}"]`)));
			if (!this.children.has(selector)) {
				const element = new Element();
				const key = selector.match(/^\[data-pool-filter="(.*)"\]$/)?.[1];
				if (key) { element.dataset.poolFilter = key; element.tagName = ["company", "assignment_state"].includes(key) ? "SELECT" : "INPUT"; element.value = key === "assignment_state" ? "all" : ""; }
				this.children.set(selector, element);
			}
			return this.children.get(selector);
		}
		each(callback) { this.elements.forEach((element, index) => callback(index, element)); return this; }
		on(events, selector, callback) { this.handlers.push({ events, selector: typeof selector === "string" ? selector : null, callback: callback || selector }); return this; }
		trigger(type, selector, target = this) { for (const handler of this.handlers) if (handler.events.split(" ").includes(type) && handler.selector === selector) handler.callback({ type, currentTarget: target }); }
	}
	const host = new Element(), dialogs = [], calls = [];
	class Dialog {
		constructor(options) {
			this.options = options; this.fields_dict = {}; this.$wrapper = new Element(); this.button = new Element(); this.visible = false; this.values = {};
			for (const field of options.fields) this.fields_dict[field.fieldname] = { $wrapper: new Element(), df: field };
			this.set_primary_action(options.primary_action_label, options.primary_action);
			dialogs.push(this);
			for (const field of options.fields) field.onchange?.();
		}
		set_primary_action(label, action) {
			this.label = label;
			this.click = (values) => {
				this.values = values;
				const result = action(values);
				// Match Frappe v16: asynchronous primary actions restore the captured label.
				if (result?.then) Promise.resolve(result).finally(() => { this.label = label; });
				return result;
			};
		}
		get_primary_btn() { return this.button; }
		get_value(fieldname) { return this.values[fieldname]; }
		show() { this.visible = true; }
		hide() { this.visible = false; this.$wrapper.trigger("hide.bs.modal.operation-pool", null); }
	}
	const previous = { $: global.$, frappe: global.frappe, __: global.__ };
	global.$ = (element) => element;
	global.__ = (message, args = []) => String(message).replace(/\{(\d+)\}/g, (_, index) => args[index]);
	global.frappe = {
		ui: { Dialog }, router: { on() {} }, get_route: () => ["production-workbench"], msgprint() {},
		call: async (args) => {
			calls.push(args);
			if (args.method.endsWith("get_operation_dispatch_pool")) return { message: { ...page([card("A")]), companies: ["Factory A", "Factory B"] } };
			if (args.method.endsWith("assign_workers_first")) return { message: { status: "assigned", job_card: args.args.job_card } };
			return { message: { job_cards: [] } };
		},
	};
	return { host, dialogs, calls, restore() { Object.assign(global, previous); } };
}

test("real Dialog primary-action contract keeps preview synchronous and final confirmation correctly labeled", async () => {
	const harness = browserHarness();
	try {
		const mounted = pool.mountOperationDispatchPool({ host: harness.host, openAssignment() {} });
		await mounted.activate(); mounted.controller.select("A", true);
		harness.host.trigger("click", "[data-pool-plan]");
		const dialog = harness.dialogs[0];
		assert.equal(dialog.click({ employee: "EMP-1" }), undefined);
		await Promise.resolve();
		assert.match(dialog.label, /确认派工 1/);
		assert.equal(harness.calls.filter((call) => call.method.endsWith("assign_workers_first")).length, 0);
		assert.equal(await mounted.refresh({ background: true }), false);
		assert.equal(dialog.visible, true);
		dialog.click({ employee: "EMP-1" });
		await new Promise((resolve) => setImmediate(resolve));
		assert.equal(harness.calls.filter((call) => call.method.endsWith("assign_workers_first")).length, 1);
		assert.equal(mounted.controller.state.results[0].status, "assigned");
	} finally { harness.restore(); }
});

test("native company select input then change preserves the newly chosen company", async () => {
	const harness = browserHarness();
	try {
		const mounted = pool.mountOperationDispatchPool({ host: harness.host, openAssignment() {} });
		await mounted.activate();
		const company = harness.host.find('[data-pool-filter="company"]');
		company.val("Factory B");
		harness.host.trigger("input", "[data-pool-filter]", company);
		assert.equal(company.val(), "Factory B");
		harness.host.trigger("change", "[data-pool-filter]", company);
		await new Promise((resolve) => setImmediate(resolve));
		assert.equal(mounted.controller.state.filters.company, "Factory B");
		assert.equal(JSON.parse(harness.calls.at(-1).args.filters).company, "Factory B");
	} finally { harness.restore(); }
});

test("late Link validation keeps the reviewed employee preview; changing employee requires another preview", async () => {
	const harness = browserHarness();
	try {
		const mounted = pool.mountOperationDispatchPool({ host: harness.host, openAssignment() {} });
		await mounted.activate(); mounted.controller.select("A", true);
		harness.host.trigger("click", "[data-pool-plan]");
		const dialog = harness.dialogs[0];
		dialog.click({ employee: "EMP-1" });
		dialog.fields_dict.employee.df.onchange();
		assert.match(dialog.label, /确认派工 1/);
		assert.match(dialog.fields_dict.preview.$wrapper.content, /EMP-1：10/);
		// The value can change before the async Link onchange has finished.
		dialog.click({ employee: "EMP-2" });
		assert.equal(harness.calls.filter((call) => call.method.endsWith("assign_workers_first")).length, 0);
		assert.match(dialog.fields_dict.preview.$wrapper.content, /EMP-2：10/);
		dialog.values = { employee: "EMP-3" };
		dialog.fields_dict.employee.df.onchange();
		assert.equal(dialog.label, "预览逐任务分配");
		dialog.click({ employee: "EMP-3" });
		dialog.click({ employee: "EMP-3" });
		await new Promise((resolve) => setImmediate(resolve));
		const writes = harness.calls.filter((call) => call.method.endsWith("assign_workers_first"));
		assert.equal(writes.length, 1);
		assert.equal(JSON.parse(writes[0].args.assignments)[0].employee, "EMP-3");
	} finally { harness.restore(); }
});
