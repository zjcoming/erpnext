const test = require("node:test");
const assert = require("node:assert/strict");
const { createPageRefreshController, createPageReadLoader, bindFreshNotificationView } = require("../../public/js/page_refresh.js");

async function drain() { for (let i = 0; i < 20; i++) await Promise.resolve(); }
function fixture() {
	let now = 1000000, id = 0, visible = true, connected = true, checks = 0, notifications = 0, loads = 0;
	const timers = new Map(), versions = { tasks: "A", notifications: "A" };
	const options = { now: () => now, visible: () => visible, connected: () => connected,
		setTimeout: (fn, ms) => { timers.set(++id, { fn, at: now + ms, ms }); return id; }, clearTimeout: (id) => timers.delete(id),
		check: async () => { checks++; return { versions: { ...versions } }; },
		notifications: async () => { notifications++; },
	};
	const page = { topics: ["tasks"], interval: 3000, load: async () => { loads++; return true; }, status() {} };
	const controller = createPageRefreshController(options);
	async function tick(ms) {
		const until = now + ms;
		for (;;) {
			const next = [...timers].filter(([, t]) => t.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
			if (!next) break;
			timers.delete(next[0]); now = next[1].at; next[1].fn(); await drain();
		}
		now = until; await drain();
	}
	return { controller, options, page, versions, timers, tick, counters: () => ({ checks, notifications, loads }),
		hide() { visible = false; controller.wake(); }, show() { visible = true; controller.wake(); },
		disconnect() { connected = false; controller.wake(); },
	};
}
test("idle pages check only versions, at 120 seconds healthy and 30 seconds disconnected", async () => {
	const f = fixture(); f.controller.activate(f.page); await drain(); await f.tick(3000);
	const baseline = f.counters();
	await f.tick(117000);
	assert.equal(f.counters().loads, baseline.loads);
	assert.equal(f.counters().notifications, baseline.notifications);
	assert.equal(f.counters().checks, baseline.checks + 1);
	f.disconnect(); await drain(); const checks = f.counters().checks;
	await f.tick(30000); assert.equal(f.counters().checks, checks + 1);
});
test("continuous event bursts have a bounded delay and unrelated topics do not reload", async () => {
	const f = fixture(); f.controller.activate(f.page); await drain(); await f.tick(3000);
	const before = f.counters().loads;
	f.controller.event({ topics: ["purchase"] }); await f.tick(1000);
	assert.equal(f.counters().loads, before);
	for (let i = 0; i < 12; i++) { f.controller.event({ topics: ["tasks"] }); await f.tick(250); }
	assert.equal(f.counters().loads, before + 1);
});
test("changes during an in-flight read result in one follow-up, never concurrent reads", async () => {
	const f = fixture(); f.controller.activate(f.page); await drain(); await f.tick(3000);
	let resolve, calls = 0;
	f.page.load = () => { calls++; return new Promise((yes) => { resolve = yes; }); };
	f.controller.event({ topics: ["tasks"] }); await f.tick(3000);
	for (let i = 0; i < 20; i++) f.controller.event({ topics: ["tasks"] });
	await f.tick(30000); assert.equal(calls, 1);
	resolve(true); await drain();
	f.page.load = async () => { calls++; return true; };
	await f.tick(1000); assert.equal(calls, 2);
});
test("editing and hidden pages preserve pending changes until safe foreground refresh", async () => {
	const f = fixture(); f.controller.activate(f.page); await drain(); await f.tick(3000);
	const before = f.counters().loads;
	f.page.editable = () => true;
	f.controller.event({ topics: ["tasks"] }); await f.tick(30000);
	assert.equal(f.counters().loads, before);
	f.hide(); await f.tick(180000); assert.equal(f.timers.size, 0);
	f.page.editable = () => false; f.versions.tasks = "B"; f.show(); await drain(); await f.tick(1000);
	assert.equal(f.counters().loads, before + 1);
});
test("failed notification fetch retains the version so the next check retries", async () => {
	const f = fixture(); let attempts = 0;
	f.options.notifications = async () => { attempts++; if (attempts === 1) throw new Error("offline"); };
	f.controller.activate(null); await drain();
	await f.tick(30000); assert.equal(attempts, 2);
});
test("50 idle foreground pages do not recalculate business data between version changes", async () => {
	const clients = Array.from({ length: 50 }, fixture);
	for (const f of clients) { f.controller.activate(f.page); await drain(); await f.tick(3000); }
	const loads = clients.reduce((sum, f) => sum + f.counters().loads, 0);
	for (const f of clients) await f.tick(600000);
	assert.equal(clients.reduce((sum, f) => sum + f.counters().loads, 0), loads);
	assert.equal(clients.reduce((sum, f) => sum + f.counters().checks, 0), 300);
});
test("reader shares duplicate calls and refuses late responses from old filters/routes", async () => {
	const pending = [], applied = []; let current = true;
	const read = createPageReadLoader((args) => new Promise((resolve) => pending.push({ args, resolve })), () => current);
	const page = {};
	const a = read(page, { method: "orders", args: { search: "A" }, background: true, apply: (r) => applied.push(r.message) });
	const duplicate = read(page, { method: "orders", args: { search: "A" }, apply() { throw Error("duplicate"); } });
	await drain(); assert.equal(pending.length, 1); assert.equal(pending[0].args.freeze, false);
	const b = read(page, { method: "orders", args: { search: "B" }, apply: (r) => applied.push(r.message) });
	await drain(); assert.equal(pending.length, 1);
	pending[0].resolve({ message: "A" }); await Promise.all([a, duplicate]); await drain();
	pending[1].resolve({ message: "B" }); await b; await drain(); assert.deepEqual(applied, ["B"]);
	const c = read(page, { method: "orders", args: { search: "C" }, apply: (r) => applied.push(r.message) });
	await drain(); current = false; pending[2].resolve({ message: "C" }); assert.equal(await c, false);
	assert.deepEqual(applied, ["B"]);
});
test("reader rechecks editing at response time and keeps query arguments immutable", async () => {
	let resolve, requested, editing = false, applied = false;
	const read = createPageReadLoader((args) => { requested = args; return new Promise((yes) => { resolve = yes; }); }, () => true);
	const args = { filters: { customer: "A" } };
	const page = { ps_refresh: { editable: () => editing } };
	const p = read(page, { method: "orders", args, background: true, apply() { applied = true; } });
	args.filters.customer = "B"; await drain(); assert.equal(requested.args.filters.customer, "A");
	editing = true; resolve({ message: {} }); assert.equal(await p, false); assert.equal(applied, false);
});
test("switching A to B to A while both requests are pending applies only the latest selection", async () => {
	const pending = [], applied = [];
	const read = createPageReadLoader(() => new Promise((resolve) => pending.push(resolve)), () => true);
	const page = {}, request = (search) => read(page, { method: "orders", args: { search }, apply: (r) => applied.push(r.message) });
	const a = request("A"), b = request("B"), again = request("A");
	await drain(); assert.equal(pending.length, 1); assert.equal(await b, false);
	pending[0]({ message: "A" }); await Promise.all([a, again]);
	assert.deepEqual(applied, ["A"]);
});
test("failed business reads back off instead of retrying every three seconds", async () => {
	const f = fixture(); let attempts = 0;
	f.page.load = async () => { attempts++; throw Error("server unavailable"); };
	f.controller.activate(f.page); await drain(); await f.tick(1000);
	await f.tick(29999); assert.equal(attempts, 1);
	await f.tick(1); assert.equal(attempts, 2);
	await f.tick(60000); assert.equal(attempts, 3);
});
test("leaving input resumes a dirty page without another version request", async () => {
	const f = fixture(); f.controller.activate(f.page); await drain(); await f.tick(3000);
	const before = f.counters().checks;
	for (let i = 0; i < 100; i++) f.controller.resumePage();
	await drain(); assert.equal(f.counters().checks, before);
});
test("events arriving during a version read trigger a catch-up check", async () => {
	const f = fixture(); let resolve, checks = 0;
	f.options.check = () => { checks++; return new Promise((yes) => { resolve = yes; }); };
	f.controller.activate(null); await drain();
	f.controller.event({ topics: ["notifications"] });
	resolve({ versions: { notifications: "A" } }); await drain();
	assert.equal(checks, 2);
	resolve({ versions: { notifications: "B" } }); await drain();
	assert.equal(f.counters().notifications, 2);
});
test("a late native cached notification response cannot replace the fresh snapshot", () => {
	let rendered, events = 0, badge;
	const data = { notification_logs: [{ name: "fresh" }], enabled: true, unread_count: 1, seen: 0 };
	const view = { settings: {}, container: { empty() {} },
		render_notifications_dropdown() { rendered = this.dropdown_items; },
		update_count_badge(n) { badge = n; }, toggle_notification_icon() {} };
	bindFreshNotificationView(view, () => data, () => { events++; });
	view.dropdown_items = [{ name: "old cached GET" }];
	view.render_notifications_dropdown();
	assert.deepEqual(rendered, data.notification_logs); assert.equal(badge, 1);
	view.update_dropdown(); assert.equal(events, 1);
});
test("a changing calculation can display a snapshot while retaining the pending refresh", async () => {
	let displayed;
	const read = createPageReadLoader(() => ({ then: (resolve) => resolve({message:{rows:[1],_refresh_stale:true}}) }), () => true);
	assert.equal(await read({}, {method:'orders',apply: r => { displayed = r.message.rows; }}), false);
	assert.deepEqual(displayed, [1]);
});
