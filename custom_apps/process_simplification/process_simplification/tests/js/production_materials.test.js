const test = require("node:test");
const assert = require("node:assert/strict");
const material = require("../../public/js/production_materials.js");
const row = (name, changes = {}) => ({ name, work_order: name, company: "C", can_select: true, request_qty: 10,
	expected_state: { work_order: name }, items: [{ item_code: "RM", source_warehouse: "Stores", target_warehouse: "WIP", uom: "Nos", qty: 10 }], ...changes });
const page = (rows, view = "request") => ({ rows, view, capabilities: { request: true, confirm: true }, companies: ["C"] });
const deferred = () => { let resolve; const promise = new Promise((yes) => { resolve = yes; }); return { promise, resolve }; };
function setup(options = {}) {
	const calls = [];
	const rows = options.rows || [row("A"), row("B")];
	const controller = material.createProductionMaterialsController({ read: async () => page(rows),
		preview: async (view, names) => ({ rows: rows.filter((item) => names.includes(item.name)) }),
		request: async (args) => { calls.push(args); return { stock_entry: `STE-${args.work_order}` }; },
		submit: async (args) => { calls.push(args); return { stock_entry: args.stock_entry }; },
		outcome: async () => ({ status: "unconfirmed" }), ...options });
	return { controller, calls };
}

test("selection respects current page eligibility company and cap", async () => {
	const { controller } = setup({ rows: [...Array.from({ length: 22 }, (_, i) => row(String(i))), row("FOREIGN", { company: "Other" }), row("BLOCKED", { can_select: false })] });
	await controller.load(); controller.selectPage(true);
	assert.equal(controller.state.selected.size, 20);
	assert.equal(controller.state.selected.has("BLOCKED"), false);
	controller.selectPage(false); controller.select("0", true);
	assert.match(controller.select("FOREIGN", true), /同一公司/);
});

test("preview and reads never request or submit", async () => {
	const { controller, calls } = setup();
	await controller.load(); controller.select("A", true); const preview = await controller.prepare();
	assert.equal(preview.rows[0].name, "A"); assert.equal(calls.length, 0);
});

test("partial requests need explicit confirmation and never override priority", async () => {
	const { controller, calls } = setup({ rows: [row("A", { partial: true, request_qty: 4 })] });
	await controller.load(); controller.select("A", true); const preview = await controller.prepare();
	await assert.rejects(controller.execute(preview), /确认/); assert.equal(calls.length, 0);
	await controller.execute(preview, true);
	assert.equal(calls[0].allow_partial, 1); assert.equal(calls[0].qty, 4); assert.equal("override_priority" in calls[0], false);
});

test("warehouse confirmation requires explicit physical check", async () => {
	const rows = [row("STE", { stock_entry: "STE", work_order: "WO", expected_state: "hash" })];
	const { controller, calls } = setup({ rows, read: async () => page(rows, "confirm") });
	await controller.load(); controller.select("STE", true); const plan = await controller.prepare();
	await assert.rejects(controller.execute(plan), /确认/); assert.equal(calls.length, 0);
	await controller.execute(plan, true); assert.deepEqual(calls, [{ stock_entry: "STE", expected_state: "hash" }]);
});

test("changed selection invalidates an in-flight preview", async () => {
	const pending = deferred(); const { controller, calls } = setup({ preview: () => pending.promise });
	await controller.load(); controller.select("A", true); const preview = controller.prepare();
	controller.select("B", true); pending.resolve({ rows: [row("A")] });
	await assert.rejects(preview, /范围已变化/); assert.equal(calls.length, 0);
});

test("backend preview cannot add or remove selected documents", async () => {
	const { controller } = setup({ preview: async () => ({ rows: [row("A"), row("B")] }) });
	await controller.load(); controller.select("A", true);
	await assert.rejects(controller.prepare(), /不一致/);
});

test("one validation failure does not erase another successful document", async () => {
	const calls = []; const { controller } = setup({ request: async (args) => { calls.push(args.work_order); if (args.work_order === "A") throw { status: 417, message: "库存变化" }; return { stock_entry: "STE-B" }; } });
	await controller.load(); controller.selectPage(true); await controller.execute(await controller.prepare());
	assert.deepEqual(calls, ["A", "B"]); assert.deepEqual(controller.state.results.map((result) => result.status), ["failed", "success"]);
});

test("preview makes newly unavailable rows explicit without blocking eligible work", async () => {
	const { controller, calls } = setup({ preview: async () => ({ rows: [row("A"), row("B", { can_select: false, request_qty: 0, items: [], block_message: "共享库存不足" })] }) });
	await controller.load(); controller.selectPage(true); const plan = await controller.prepare();
	assert.match(material.materialPreviewHtml(plan.rows, "request"), /本轮不处理/);
	await controller.execute(plan);
	assert.equal(calls.length, 1); assert.equal(controller.state.results[1].status, "skipped");
});

test("unknown network outcome stops later writes and survives refresh", async () => {
	let writes = 0; const { controller } = setup({ request: async () => { writes += 1; throw { status: 0 }; } });
	await controller.load(); controller.selectPage(true); await controller.execute(await controller.prepare());
	assert.equal(writes, 1); assert.equal(controller.state.results[0].status, "uncertain");
	await controller.load(); assert.equal(controller.selectable(controller.state.rows[0]), false);
	controller.selectPage(true); assert.deepEqual([...controller.state.selected], ["B"]);
});

test("readback locates existing request without claiming it was created by this batch", async () => {
	const { controller } = setup({ request: async () => { throw { status: 0 }; }, outcome: async () => ({ status: "exists", stock_entry: "STE" }) });
	await controller.load(); controller.select("A", true); await controller.execute(await controller.prepare());
	assert.equal(controller.state.results[0].status, "exists"); assert.match(controller.state.results[0].message, /无法判断/);
});

test("leaving view stops unsent documents but keeps result of in-flight write", async () => {
	const pending = deferred(), calls = [];
	const { controller } = setup({ request: (args) => { calls.push(args.work_order); return pending.promise; } });
	await controller.load(); controller.selectPage(true); const execution = controller.execute(await controller.prepare());
	controller.deactivate(); pending.resolve({ stock_entry: "STE-A" }); await execution;
	assert.deepEqual(calls, ["A"]); assert.equal(controller.state.results[0].status, "success");
});

test("stale list response cannot overwrite newer filters", async () => {
	const pending = deferred(); let count = 0;
	const { controller } = setup({ read: () => (++count === 1 ? pending.promise : Promise.resolve(page([row("NEW")])) ) });
	const old = controller.load(); await controller.load({ search: "new" }); pending.resolve(page([row("OLD")])); await old;
	assert.equal(controller.state.rows[0].name, "NEW");
});

test("material summary never merges distinct source warehouses or units", () => {
	const rows = [row("A"), row("B"), row("C", { items: [{ item_code: "RM", source_warehouse: "Other", target_warehouse: "WIP", uom: "Nos", qty: 5 }] })];
	const summary = material.materialSummary(rows);
	assert.equal(summary.length, 2); assert.equal(summary[0].qty, 20); assert.equal(summary[1].qty, 5);
});

test("preview escapes item warehouse and order labels", () => {
	const html = material.materialPreviewHtml([row("<script>", { items: [{ item_code: "<img>", qty: 1, uom: "件", source_warehouse: "<svg>", target_warehouse: "WIP" }] })], "request");
	assert.doesNotMatch(html, /<script>|<img>|<svg>/); assert.match(html, /&lt;script&gt;/);
});

test("batch cards explain the native fallback and cannot be selected", () => {
	const html = material.materialRowHtml(row("STE-BATCH", { stock_entry: "STE-BATCH", can_select: false,
		requires_native: true, block_message: "请在原单核对批次、序列号和实际数量。" }), { view: "confirm" });
	assert.match(html, /data-material-select="STE-BATCH"[^>]*disabled/);
	assert.match(html, /打开原单核对批次/);
	assert.match(html, /href="\/app\/stock-entry\/STE-BATCH"/);
	assert.match(html, /请在原单核对批次、序列号和实际数量/);
});

test("uncertain cards retain an explicit blocked state and next step", () => {
	const html = material.materialRowHtml(row("WO-UNCERTAIN"), { selectable: false });
	assert.match(html, /上次结果待核实/);
	assert.match(html, /先到下方处理结果中核实/);
	assert.match(html, /data-material-select="WO-UNCERTAIN"[^>]*disabled/);
	assert.equal(material.materialQuantity(0), "0");
	assert.equal(material.materialQuantity(undefined), "—");
});

test("empty states distinguish no tasks, filters and request failure", () => {
	const state = { view: "request", companies: ["C"], search: "", company: "" };
	assert.match(material.materialEmptyHtml(state), /订单与进度/);
	assert.match(material.materialEmptyHtml({ ...state, view: "confirm" }), /主管还未申请/);
	assert.match(material.materialEmptyHtml({ ...state, view: "confirm" }), /完工入库请到“库房工作台”办理/);
	assert.doesNotMatch(material.materialEmptyHtml({ ...state, view: "confirm" }), /全部库房待办/);
	assert.match(material.materialEmptyHtml({ ...state, search: "<order>" }), /&lt;order&gt;/);
	assert.match(material.materialEmptyHtml({ ...state, search: "X" }), /data-material-clear/);
	assert.doesNotMatch(material.materialEmptyHtml({ ...state, error: "Server unavailable" }), /当前没有待/);
});

function fakeHost() {
	const elements = new Map(), handlers = new Map();
	const element = () => ({ values: {}, addClass() { return this; }, on(event, selector, callback) { handlers.set(`${event}:${selector}`, callback); return this; },
		html(value) { this.values.html = value; return this; }, text(value) { this.values.text = value; return this; },
		prop(key, value) { this.values[key] = value; return this; }, attr(key, value) { this.values[key] = value; return this; },
		val(value) { if (!arguments.length) return this.values.value; this.values.value = value; return this; } });
	return Object.assign(element(), { trigger(event, selector, payload = {}) { return handlers.get(`${event}:${selector}`)?.(payload); },
		find(selector) { if (!elements.has(selector)) elements.set(selector, element()); return elements.get(selector); } });
}

function mountedFixture(t, { route = "production-workbench", capabilities = { request: true, confirm: true }, call, options = {} } = {}) {
	const original = { frappe: globalThis.frappe, translate: globalThis.__ };
	const calls = [], dialogs = [], messages = [], host = fakeHost();
	let currentRoute = route, changed;
	globalThis.__ = String;
	globalThis.frappe = {
		get_route: () => [currentRoute], router: { on(_event, callback) { changed = callback; } },
		msgprint: (message) => messages.push(message),
		call: async (args) => {
			calls.push(args);
			if (call) return call(args);
			return { message: { ...page([row("A"), row("B")], args.args.view || (capabilities.request ? "request" : "confirm")), capabilities } };
		},
		ui: { Dialog: class {
			constructor(config) { this.config = config; this.hidden = true; this.$wrapper = { on() {} }; dialogs.push(this); }
			show() { this.hidden = false; }
			hide() { this.hidden = true; }
			get_primary_btn() { return { prop() {} }; }
		} },
	};
	t.after(() => { globalThis.frappe = original.frappe; globalThis.__ = original.translate; });
	const mounted = material.mountProductionMaterials(host, options);
	return { ...mounted, host, calls, dialogs, messages, navigate(next) { currentRoute = next; changed(); } };
}

test("warehouse preference selects confirmation once after learning capabilities and preserves later user choice", async (t) => {
	const f = mountedFixture(t, { route: "warehouse-workbench", options: { ownerRoute: "warehouse-workbench", preferredView: "confirm" } });
	await f.activate();
	assert.deepEqual(f.calls.map((call) => call.args.view), [undefined, "confirm"]);
	assert.equal(f.controller.state.view, "confirm");
	await f.controller.load({ view: "request" });
	await f.refresh(); await f.activate();
	assert.equal(f.controller.state.view, "request");
	assert.equal(f.calls.at(-1).args.view, "request");
	assert.ok(f.calls.every((call) => call.method.endsWith(".get_material_workbench")));
});

test("initial view preference falls back without attempting a forbidden confirmation read", async (t) => {
	const f = mountedFixture(t, { capabilities: { request: true, confirm: false }, options: { preferredView: "confirm" } });
	await f.activate();
	assert.equal(f.calls.length, 1);
	assert.equal(f.calls[0].args.view, undefined);
	assert.equal(f.controller.state.view, "request");
	assert.doesNotMatch(f.host.find(".production-materials-tabs").values.html, /data-material-view="confirm"/);
});

test("confirmation-only operators need no second read for the preferred warehouse stage", async (t) => {
	const f = mountedFixture(t, { capabilities: { request: false, confirm: true }, options: { preferredView: "confirm" } });
	await f.activate();
	assert.equal(f.calls.length, 1);
	assert.equal(f.controller.state.view, "confirm");
});

test("order shortcuts override the initial stage preference and ordinary returns keep that choice", async (t) => {
	const f = mountedFixture(t, { options: { preferredView: "confirm" } });
	await f.activate({ search: "SO-1" });
	assert.equal(f.calls.length, 1);
	assert.equal(f.controller.state.view, "request");
	await f.activate();
	assert.equal(f.controller.state.view, "request");
	assert.equal(f.calls.at(-1).args.search, "SO-1");
});

for (const [ownerRoute, otherRoute] of [["production-workbench", "warehouse-workbench"], ["warehouse-workbench", "production-workbench"]]) {
	test(`leaving ${ownerRoute} for ${otherRoute} cancels a pending preview and allows safe return`, async (t) => {
		const pending = deferred();
		const f = mountedFixture(t, { route: ownerRoute,
			options: ownerRoute === "production-workbench" ? { ownerRoute } : {},
			call: async (args) => args.method.endsWith(".preview_material_requests") ? pending.promise : { message: page([row("A")]) },
		});
		await f.activate(); f.controller.select("A", true);
		f.navigate(ownerRoute);
		assert.equal(f.controller.state.selected.size, 1, "same owner route is not a departure");
		const preview = f.host.trigger("click", "[data-material-preview]");
		f.navigate(otherRoute);
		assert.equal(f.controller.state.selected.size, 0);
		assert.equal(f.controller.state.stopped, true);
		f.navigate(ownerRoute);
		await f.activate();
		f.controller.select("A", true);
		pending.resolve({ message: { rows: [row("A")] } }); await preview;
		assert.equal(f.dialogs.length, 0, "an old preview cannot reopen after returning to the same page");
		assert.deepEqual(f.messages, []);
		await f.host.trigger("click", "[data-material-preview]");
		assert.equal(f.dialogs.length, 1);
		assert.equal(f.dialogs[0].hidden, false);
		f.navigate(otherRoute);
		assert.equal(f.dialogs[0].hidden, true, "an open preview closes when its own page is left");
		assert.ok(f.calls.every((call) => /\.(get_material_workbench|preview_material_requests)$/.test(call.method)));
	});

	test(`leaving ${ownerRoute} stops later writes while preserving an in-flight result`, async (t) => {
		const pending = deferred(), writes = [];
		const view = ownerRoute === "warehouse-workbench" ? "confirm" : "request";
		const rows = [row("A", { stock_entry: "STE-A" }), row("B", { stock_entry: "STE-B" })];
		const f = mountedFixture(t, { route: ownerRoute, options: { ownerRoute }, call: async (args) => {
			if (/\.(request_material|submit_material)$/.test(args.method)) { writes.push(args.args); return pending.promise; }
			if (args.method.includes(".preview_")) return { message: { rows } };
			return { message: page(rows, view) };
		} });
		await f.activate(); f.controller.selectPage(true);
		const plan = await f.controller.prepare();
		const execution = f.controller.execute(plan, true);
		f.navigate(otherRoute);
		pending.resolve({ message: { stock_entry: "STE-A" } }); await execution;
		assert.equal(writes.length, 1);
		assert.equal(f.controller.state.results[0].status, "success");
		assert.equal(f.controller.state.selected.size, 0);
		f.navigate(ownerRoute); await f.activate();
		assert.equal(writes.length, 1, "returning only reads; it never resumes cancelled writes");
	});
}

test("order navigation resets filters and selection with no write, and keeps ordinary activation compatible", async (t) => {
	const calls = [], host = fakeHost();
	const previousFrappe = globalThis.frappe;
	globalThis.frappe = { call: async (args) => { calls.push(args); return { message: page([row("A")], args.args.view || "request") }; }, router: { on() {} } };
	t.after(() => { globalThis.frappe = previousFrappe; });
	const mounted = material.mountProductionMaterials(host);
	assert.equal(calls.length, 0);
	await mounted.activate();
	mounted.controller.select("A", true);
	assert.match(host.find("[data-material-preview]").values.text, /1 张/);
	await mounted.controller.load({ view: "confirm", company: "C", start: 20 });
	await mounted.activate({ search: "  SO-001  " });
	assert.deepEqual(calls.at(-1).args, { view: "request", company: "", search: "SO-001", start: 0 });
	assert.equal(host.find("[data-material-search]").val(), "SO-001");
	assert.equal(mounted.controller.state.selected.size, 0);
	assert.equal(host.find("[data-material-preview]").values.disabled, true);
	assert.equal(host.find(".production-materials-company-filter").values.hidden, true);
	assert.equal(host.find("[data-material-single-company]").values.text, "C");
	assert.ok(calls.every((call) => call.method.endsWith(".get_material_workbench")));
});

test("warehouse-only activation keeps confirmation view and hides unavailable request tab", async (t) => {
	const calls = [], host = fakeHost(), previousFrappe = globalThis.frappe;
	globalThis.frappe = { call: async (args) => { calls.push(args); return { message: { ...page([], "confirm"), capabilities: { request: false, confirm: true } } }; }, router: { on() {} } };
	t.after(() => { globalThis.frappe = previousFrappe; });
	const mounted = material.mountProductionMaterials(host);
	await mounted.activate();
	await mounted.activate({ search: "WO-002" });
	assert.equal(calls.at(-1).args.view, "confirm");
	assert.equal(host.find("[data-material-title]").values.text, "库房确认出库");
	assert.equal(host.find(".production-materials-tabs").values.hidden, true);
	assert.doesNotMatch(host.find(".production-materials-tabs").values.html, /data-material-view="request"/);
	assert.match(host.find("[data-material-selection-hint]").values.text, /实际发出/);
	assert.ok(calls.every((call) => call.method.endsWith(".get_material_workbench")));
});

test("manager-only view offers application without a warehouse confirmation action", async (t) => {
	const host = fakeHost(), previousFrappe = globalThis.frappe;
	globalThis.frappe = { call: async () => ({ message: { ...page([row("A")]), capabilities: { request: true, confirm: false } } }), router: { on() {} } };
	t.after(() => { globalThis.frappe = previousFrappe; });
	const mounted = material.mountProductionMaterials(host);
	await mounted.activate({ search: "SO-003" });
	assert.equal(host.find("[data-material-title]").values.text, "集中申请领料");
	assert.doesNotMatch(host.find(".production-materials-tabs").values.html, /data-material-view="confirm"/);
	assert.match(host.find("[data-material-description]").values.text, /库房确认出库后/);
	assert.equal(host.find("[data-material-preview]").values.text, "预览领料申请");
});

test("empty pages hide the selection action bar while blocked rows retain guidance", async (t) => {
	const host = fakeHost(), previousFrappe = globalThis.frappe;
	let rows = [];
	globalThis.frappe = { call: async () => ({ message: page(rows) }), router: { on() {} } };
	t.after(() => { globalThis.frappe = previousFrappe; });
	const mounted = material.mountProductionMaterials(host);
	await mounted.activate();
	assert.equal(host.find(".production-materials-actions").values.hidden, true);
	rows = [row("BLOCKED", { can_select: false, block_message: "库存不足" })];
	await mounted.refresh();
	assert.equal(host.find(".production-materials-actions").values.hidden, false);
	assert.equal(host.find("[data-material-preview]").values.disabled, true);
	assert.match(host.find(".production-materials-list").values.html, /库存不足/);
	rows = [];
	await mounted.refresh();
	assert.equal(host.find(".production-materials-actions").values.hidden, true);
});
