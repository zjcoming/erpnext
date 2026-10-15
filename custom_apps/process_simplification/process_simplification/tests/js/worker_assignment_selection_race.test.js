const nodeTest = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = (name, callback) => nodeTest(name, { timeout: 3000 }, callback);

const appDirectory = path.resolve(__dirname, "../..");
const appSource = fs.readFileSync(path.join(appDirectory, "public/js/worker_assignment.js"), "utf8");
const gridPath = [
	process.env.FRAPPE_GRID_SOURCE,
	path.resolve(appDirectory, "../../frappe/frappe/public/js/frappe/form/grid.js"),
	path.resolve(appDirectory, "../../../development/frappe-bench/apps/frappe/frappe/public/js/frappe/form/grid.js"),
].find((candidate) => candidate && fs.existsSync(candidate));
assert.ok(gridPath, "The real Frappe Grid source is required; set FRAPPE_GRID_SOURCE outside a bench.");
const gridSource = fs.readFileSync(gridPath, "utf8");
const deferred = () => {
	let resolve, reject;
	const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
	return { promise, resolve, reject };
};

// Execute the actual dialog handler and native add/focus methods. Only DOM row
// rendering, RPC delivery and timers are controlled; no copy of the race fix.
function createEnvironment(initialContext, { holdDispatch = false } = {}) {
	const timers = [], lookups = new Map(), contexts = new Map(), messages = [], dispatches = [], pendingDispatches = [];
	class Element {
		constructor(nodes = [{}]) { this.nodes = nodes; this.handlers = new Map(); }
		prop(key, value) {
			if (arguments.length === 1) return this.nodes[0]?.[key];
			this.nodes.forEach((node) => { node[key] = value; }); return this;
		}
		each(callback) { this.nodes.forEach((node, index) => callback(index, node)); return this; }
		on(event, callback) { if (typeof callback === "function") this.handlers.set(event, callback); return this; }
		click(callback) { this.nodes[0].click = callback; return this; }
		find() { return new Element(); }
		addClass() { return this; } attr() { return this; } html() { return this; }
		remove() { return this; } text() { return this; } filter() { return this; } focus() { return this; }
	}
	const frappe = {
		ui: { form: {} }, utils: { escape_html: String }, session: { user: "supervisor" },
		user_roles: ["Process Simplification Production Manager"], msgprint: (message) => messages.push(message),
		show_alert() {},
		db: { get_value: (_doctype, name) => { const pending = deferred(); lookups.set(name, pending); return pending.promise; } },
		call: ({ method, args }) => {
			if (method.endsWith("assign_workers")) {
				dispatches.push(args);
				if (holdDispatch) { const pending = deferred(); pendingDispatches.push(pending); return pending.promise; }
				return Promise.resolve({ message: {} });
			}
			assert.ok(method.endsWith("get_work_order_assignment_context"));
			if (initialContext && args.work_order === initialContext.work_order.name) return Promise.resolve({ message: initialContext });
			const pending = deferred(); contexts.set(args.work_order, pending); return pending.promise;
		},
	};
	const common = {
		frappe, console, $: () => new Element(), __: (message) => message, flt: Number, format_number: String,
		workerAssignmentEmployeeFormatter: () => "",
		setTimeout: (callback, delay) => { const timer = { callback, delay, active: true }; timers.push(timer); return timer; },
		clearTimeout: (timer) => { if (timer) timer.active = false; },
	};
	const nativeContext = { ...common, module: { exports: {} } };
	vm.runInNewContext(gridSource.replace(/^import .*;\r?\n/gm, "").replace("export default class Grid", "class Grid") + "\nmodule.exports = Grid;", nativeContext, { filename: gridPath });
	const Grid = nativeContext.module.exports;
	class Dialog {
		constructor(options) {
			this.options = options; this.values = {}; this.fields_dict = {}; this.$wrapper = new Element();
			this.primary = new Element([{ disabled: false }]); this.refreshes = 0;
			for (const df of options.fields) {
				if (!df.fieldname) continue;
				this.values[df.fieldname] = df.default;
				const field = { df, $wrapper: new Element([{ inert: false }]) }; this.fields_dict[df.fieldname] = field;
				if (df.fieldtype !== "Table") continue;
				const add = { disabled: false }, duplicate = { disabled: true };
				field.add = add; field.duplicate = duplicate;
				field.$wrapper.find = (selector) => selector === ".grid-add-row"
					? new Element([add]) : selector.includes("grid-add-row") ? new Element([add, duplicate]) : new Element();
				const grid = Object.create(Grid.prototype);
				Object.assign(grid, { df, docfields: df.fields, display_status: "Write", wrapper: field.$wrapper,
					grid_rows: [], filter: {}, grid_pagination: { go_to_last_page_to_add_row() {}, go_to_page() {} } });
				grid.refresh = () => {
					this.refreshes++; grid.data = grid.get_data();
					grid.grid_rows = grid.data.map((doc) => ({ doc, toggle_editable_row() {}, row: new Element() }));
				};
				grid.refresh(); grid.setup_add_row(); field.grid = grid;
				field.clickAdd = () => {
					if (field.$wrapper.prop("inert") || add.disabled) return false;
					add.click(); return true;
				};
			}
		}
		get_value(name) { return this.values[name]; }
		set_value(name, value) { this.values[name] = value; }
		set_df_property() {} show() {} get_primary_btn() { return this.primary; }
		hide() { for (const [event, callback] of this.$wrapper.handlers) if (event.startsWith("hide.bs.modal")) callback(); }
	}
	frappe.ui.Dialog = Dialog;
	const appContext = { ...common, window: {}, module: { exports: {} } };
	vm.runInNewContext(appSource, appContext, { filename: "worker_assignment.js" });
	return {
		open: () => appContext.window.process_simplification.open_worker_assignment_dialog(initialContext ? { work_order: initialContext.work_order.name } : {}),
		lookups, contexts, messages, dispatches, pendingDispatches,
		focusErrors() {
			const errors = [];
			for (const timer of timers.filter((item) => item.active && item.delay === 100)) {
				timer.active = false; try { timer.callback(); } catch (error) { errors.push(error.message); }
			}
			return errors;
		},
	};
}

function assignmentContext(workOrder, jobCard, qty) {
	return { work_order: { name: workOrder }, can_assign: true,
		job_cards: [{ name: jobCard, for_quantity: qty, can_assign: true, assignments: [] }] };
}
async function deliverLookup(environment, jobCard, workOrder, qty) {
	environment.lookups.get(jobCard).resolve({ message: { work_order: workOrder, for_quantity: qty } });
	await new Promise(setImmediate);
}
async function deliverContext(environment, jobCard, workOrder, qty) {
	await deliverLookup(environment, jobCard, workOrder, qty);
	environment.contexts.get(workOrder).resolve({ message: assignmentContext(workOrder, jobCard, qty) });
}
function select(dialog, jobCard) {
	dialog.values.job_card = jobCard;
	return dialog.fields_dict.job_card.df.onchange();
}

test("pending context cannot accept a native Add Row that a late refresh removes", async () => {
	const environment = createEnvironment(), dialog = await environment.open(), field = dialog.fields_dict.allocations;
	const pending = select(dialog, "JC122");
	const addedWhileLoading = field.clickAdd();
	await dialog.options.primary_action({ job_card: "JC122", task_qty: 1, allocations: [{ employee: "EMP", assigned_qty: 1 }] });
	await deliverContext(environment, "JC122", "WO110", 1); await pending;
	assert.deepEqual(environment.focusErrors(), []);
	assert.equal(environment.dispatches.length, 0);
	assert.equal(addedWhileLoading, false);
	assert.equal(field.clickAdd(), true);
	assert.equal(field.grid.get_data().length, 1);
	assert.deepEqual(environment.focusErrors(), []);
	assert.equal(field.duplicate.disabled, true, "an originally disabled control stays disabled");
	assert.equal(dialog.primary.prop("disabled"), false);
});

test("a prior Job Card response cannot overwrite or unlock the latest selection", async () => {
	const environment = createEnvironment(), dialog = await environment.open();
	const old = select(dialog, "OLD"), current = select(dialog, "NEW");
	await deliverLookup(environment, "OLD", "WO-OLD", 9);
	const staleReadRequested = environment.contexts.has("WO-OLD");
	if (staleReadRequested) environment.contexts.get("WO-OLD").resolve({ message: assignmentContext("WO-OLD", "OLD", 9) });
	await old;
	const latestStillLocked = dialog.primary.prop("disabled");
	await deliverContext(environment, "NEW", "WO-NEW", 1); await current;
	assert.equal(staleReadRequested, false, "stale first await must stop before another RPC");
	assert.equal(latestStillLocked, true);
	assert.equal(dialog.values.job_card, "NEW"); assert.equal(dialog.values.task_qty, 1);
	assert.equal(dialog.fields_dict.job_card.df.get_query().filters.work_order, "WO-NEW");
	assert.equal(dialog.primary.prop("disabled"), false);
});

test("a stale context already in flight cannot replace a completed newer selection", async () => {
	const environment = createEnvironment(), dialog = await environment.open();
	const old = select(dialog, "OLD"); await deliverLookup(environment, "OLD", "WO-OLD", 9);
	const current = select(dialog, "NEW"); await deliverContext(environment, "NEW", "WO-NEW", 1); await current;
	environment.contexts.get("WO-OLD").resolve({ message: assignmentContext("WO-OLD", "OLD", 9) }); await old;
	assert.equal(dialog.values.task_qty, 1);
	assert.equal(dialog.fields_dict.job_card.df.get_query().filters.work_order, "WO-NEW");
	assert.equal(dialog.primary.prop("disabled"), false);
});

test("clearing selection invalidates pending context and prevents dispatch", async () => {
	const environment = createEnvironment(), dialog = await environment.open();
	const pending = select(dialog, "JC122"); await deliverLookup(environment, "JC122", "WO110", 1);
	await select(dialog, "");
	environment.contexts.get("WO110").resolve({ message: assignmentContext("WO110", "JC122", 1) }); await pending;
	assert.equal(dialog.values.task_qty, 0); assert.equal(dialog.fields_dict.allocations.grid.get_data().length, 0);
	assert.equal(dialog.primary.prop("disabled"), true); assert.equal(dialog.fields_dict.allocations.$wrapper.prop("inert"), true);
});

test("latest lookup failure clears old values and a new selection can recover", async () => {
	const environment = createEnvironment(), dialog = await environment.open();
	const failed = select(dialog, "BROKEN"); environment.lookups.get("BROKEN").reject(new Error("offline")); await failed;
	assert.match(environment.messages.at(-1), /读取失败/);
	assert.equal(dialog.values.task_qty, 0); assert.equal(dialog.primary.prop("disabled"), true);
	const retry = select(dialog, "JC122"); await deliverContext(environment, "JC122", "WO110", 1); await retry;
	assert.equal(dialog.values.task_qty, 1); assert.equal(dialog.primary.prop("disabled"), false);
	assert.equal(dialog.fields_dict.allocations.$wrapper.prop("inert"), false);
});

test("closing the dialog invalidates a pending response without touching the closed view", async () => {
	const environment = createEnvironment(), dialog = await environment.open();
	const pending = select(dialog, "JC122"); await deliverLookup(environment, "JC122", "WO110", 1);
	dialog.hide(); const refreshes = dialog.refreshes;
	environment.contexts.get("WO110").resolve({ message: assignmentContext("WO110", "JC122", 1) }); await pending;
	assert.equal(dialog.refreshes, refreshes); assert.equal(dialog.values.task_qty, 0);
	assert.deepEqual(environment.messages, []);
});

test("an existing work-order context remains immediately editable", async () => {
	const environment = createEnvironment(assignmentContext("WO110", "JC122", 1)), dialog = await environment.open();
	assert.equal(dialog.values.job_card, "JC122"); assert.equal(dialog.primary.prop("disabled"), false);
	assert.equal(dialog.fields_dict.allocations.clickAdd(), true);
	assert.deepEqual(environment.focusErrors(), []);
});

test("failed dispatch overlapping clear and reselect cannot cache a temporary primary lock", async () => {
	const environment = createEnvironment(undefined, { holdDispatch: true }), dialog = await environment.open();
	const firstSelection = select(dialog, "JC122");
	await deliverContext(environment, "JC122", "WO110", 1); await firstSelection;
	const submitting = dialog.options.primary_action({ job_card: "JC122", task_qty: 1,
		allocations: [{ employee: "EMP", assigned_qty: 1 }] });
	const failedSubmit = assert.rejects(submitting, /POST failed/);
	assert.equal(environment.pendingDispatches.length, 1);
	await select(dialog, "");
	const reselected = select(dialog, "JC122");
	environment.pendingDispatches[0].reject(new Error("POST failed")); await failedSubmit;
	const disabledWhileContextPending = dialog.primary.prop("disabled");
	await deliverContext(environment, "JC122", "WO110", 1); await reselected;
	assert.equal(dialog.values.task_qty, 1);
	assert.equal(dialog.fields_dict.allocations.$wrapper.prop("inert"), false);
	assert.equal(dialog.fields_dict.allocations.add.disabled, false);
	assert.equal(dialog.primary.prop("disabled"), false, "the temporary POST lock must not survive a completed selection");
	assert.equal(disabledWhileContextPending, true, "POST finally must keep the pending selection locked");
});
