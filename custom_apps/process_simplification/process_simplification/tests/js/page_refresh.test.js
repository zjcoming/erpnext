const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createPageRefreshController, createPageReadLoader, bindFreshNotificationView, bindNotificationScrollUnlock } = require("../../public/js/page_refresh.js");

function notificationScrollFixture() {
	let changed, closes = 0, observers = 0;
	const state = { mobile: true, hidden: false, expanded: false };
	const dropdown = { classList: { contains: () => state.hidden } };
	const sidebar = {
		sidebar_expanded: true, notifications: { dropdown: [dropdown] },
		wrapper: { hasClass: () => state.expanded },
		close() { this.sidebar_expanded = false; closes++; },
	};
	const frappe = { app: { sidebar }, is_mobile: () => state.mobile };
	const win = {
		MutationObserver: class {
			constructor(callback) { changed = callback; observers++; }
			observe(node, options) {
				assert.equal(node, dropdown);
				assert.deepEqual(options, { attributes: true, attributeFilter: ["class"] });
			}
		},
	};
	bindNotificationScrollUnlock(frappe, win);
	return { sidebar, closes: () => closes, observers: () => observers,
		bind: () => bindNotificationScrollUnlock(frappe, win),
		change(options = {}) {
			Object.assign(state, options);
			changed();
		},
	};
}

test("closing mobile notifications releases the visually collapsed sidebar's scroll lock once", () => {
	const f = notificationScrollFixture();
	f.bind();
	assert.equal(f.observers(), 1, "reopening notifications must not duplicate the observer");
	f.change();
	assert.equal(f.closes(), 0, "keep the background locked while notifications are open");
	f.change({ hidden: true });
	assert.equal(f.closes(), 1);
	assert.equal(f.sidebar.sidebar_expanded, false);
	f.change();
	assert.equal(f.closes(), 1);
});

test("notification visibility changes leave desktop and an actually open sidebar alone", () => {
	const f = notificationScrollFixture();
	f.change({ mobile: false, hidden: true });
	assert.equal(f.closes(), 0);
	f.change({ mobile: true, expanded: true });
	assert.equal(f.closes(), 0);
});

test("an already closed sidebar does not release another component's scroll lock", () => {
	const f = notificationScrollFixture();
	f.sidebar.sidebar_expanded = false;
	f.change({ hidden: true });
	assert.equal(f.closes(), 0);
});

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

function trackedReader(f, call, isCurrent = () => true) {
	return createPageReadLoader(call, isCurrent, {
		begin: (page) => f.controller.beginRead(page),
		end: (ticket, result) => f.controller.endRead(ticket, result),
	});
}

function workerHistoryFixture() {
	const f = fixture(), pending = [], statuses = [], rendered = new Map(), menus = new Map();
	let notifications = 0;
	const filters = { status: "", page_length: "20" };
	let current = true;
	const root = { on() {}, find(selector) {
		return {
			serializeArray: () => Object.entries(filters).map(([name, value]) => ({ name, value })),
			html: (value) => rendered.set(selector, value),
		};
	} };
	f.page.main = { html() {}, find: () => root };
	f.page.add_custom_menu_item = (_menu, label, callback) => { menus.set(label, callback); return { addClass() {} }; };
	f.page.menu_btn_group = { addClass() {}, find: () => ({ attr() {} }) };
	f.page.manual = true;
	f.page.interval = 120000;
	f.page.status = (state) => statuses.push(state);
	const read = trackedReader(f, (args) => new Promise((resolve, reject) => pending.push({ args, resolve, reject })), () => current);
	const frappe = {
		pages: { "production-report-history": {} },
		ui: { make_app_page: () => f.page }, utils: { escape_html: String },
		ps_read_page: read,
	};
	const context = vm.createContext({
		frappe, __: (text) => text, flt: Number, format_number: String,
		window: { process_simplification: { page_refresh: { notifications() { notifications++; } } } },
	});
	for (const file of ["../../public/js/worker_reporting.js",
		"../../process_simplification/page/production_report_history/production_report_history.js"]) {
		vm.runInContext(fs.readFileSync(path.resolve(__dirname, file), "utf8"), context);
	}
	const native = frappe.pages["production-report-history"];
	native.on_page_load({ page: f.page });
	f.page.load = f.page.worker_history.load;
	f.controller.activate(f.page);
	function resolvePair(offset, label, page = 1) {
		pending[offset].resolve({ message: { rows: [{ operation: label }], pagination: { page, page_length: 20 } } });
		pending[offset + 1].resolve({ message: [{ request_type: label }] });
	}
	return { ...f, filters, pending, statuses, rendered, resolvePair, menus, notificationCalls: () => notifications,
		nativeRefresh: () => native.refresh({ page: f.page }), leave: () => { current = false; f.controller.activate(null); },
	};
}

function receiptNoticeFixture(name = "") {
	const f = fixture(), pending = [], statuses = [], rendered = new Map(), nodes = new Map();
	function node(key) {
		if (nodes.has(key)) return nodes.get(key);
		const value = { length: 1, get: () => ({ isConnected: true }),
			html(html) { rendered.set(key, html); return this; }, empty() { return this.html(""); },
			find: (selector) => node(selector), append() { return this; }, appendTo() { return this; },
			attr() { return this; }, prop() { return this; }, val() { return this; }, text() { return this; }, on() { return this; },
			children: () => ({ length: 0 }),
		};
		nodes.set(key, value); return value;
	}
	f.page.main = node("main");
	f.page.clear_primary_action = () => {};
	f.page.add_inner_button = () => {};
	f.page.manual = true; f.page.interval = 120000;
	f.page.status = (state) => statuses.push(state);
	const frappe = { pages: { "purchase-receipt-notice": {} }, ui: { make_app_page: () => f.page },
		utils: { escape_html: String }, get_route: () => ["purchase-receipt-notice"],
		ps_read_page: trackedReader(f, (args) => new Promise((resolve, reject) => pending.push({ args, resolve, reject }))),
	};
	const context = vm.createContext({ frappe, $: node, __: String, URLSearchParams,
		setTimeout, clearTimeout, window: { location: { search: name ? `?name=${name}` : "" }, history: { replaceState() {} } },
	});
	const native = frappe.pages["purchase-receipt-notice"], wrapper = {};
	vm.runInContext(fs.readFileSync(path.resolve(__dirname, "../../process_simplification/page/purchase_receipt_notice/purchase_receipt_notice.js"), "utf8"), context);
	native.on_page_load(wrapper);
	f.page.load = f.page.purchase_notice_refresh;
	f.controller.activate(f.page);
	return { ...f, pending, statuses, rendered, load: wrapper.load_receipt_notice };
}

test("arrival list acknowledges completion only after its actual rows have loaded", async () => {
	const f = receiptNoticeFixture();
	const load = f.load(); await drain();
	assert.equal(f.pending.length, 1);
	assert.match(f.pending[0].args.method, /get_notice_page$/);
	assert.equal(f.statuses.at(-1), "loading");
	f.pending[0].resolve({ message: { rows: [], next_start: null } }); await load;
	assert.equal(f.statuses.at(-1), "fresh");
	assert.equal(f.page.dirty, false);
	assert.match(f.rendered.get(".purchase-notice-rows"), /当前范围没有到货记录/);
});

test("arrival refresh retains a change received during the query until the next successful refresh", async () => {
	const f = receiptNoticeFixture();
	const first = f.controller.refresh(true); await drain();
	f.controller.event({ topics: ["tasks"] });
	f.pending[0].resolve({ message: { rows: [], next_start: null } }); await first;
	assert.equal(f.page.dirty, true);
	await f.tick(120000); assert.equal(f.pending.length, 1, "manual arrival page does not poll records");
	const retry = f.controller.refresh(true); await drain();
	f.pending[1].resolve({ message: { rows: [], next_start: null } }); await retry;
	assert.equal(f.page.dirty, false);
	assert.equal(f.statuses.at(-1), "fresh");
});

test("arrival detail failure keeps an error until retry successfully renders the notice", async () => {
	const f = receiptNoticeFixture("NOTICE");
	const first = f.controller.refresh(true); await drain();
	assert.match(f.pending[0].args.method, /get_notice$/);
	f.pending[0].reject(Error("offline")); await first;
	assert.equal(f.statuses.at(-1), "error");
	assert.match(f.rendered.get("main"), /读取失败/);
	const retry = f.controller.refresh(true); await drain();
	f.pending[1].resolve({ message: { subject: "Arrival", description: "Received", status: "Delivered" } }); await retry;
	assert.equal(f.statuses.at(-1), "fresh");
	assert.equal(f.page.dirty, false);
});

test("worker history first entry and native refresh clear the banner only after both sections finish", async () => {
	const f = workerHistoryFixture();
	const initial = f.nativeRefresh(); await drain();
	assert.equal(f.pending.length, 2);
	f.pending[0].resolve({ message: { rows: [], pagination: { page: 1 } } }); await drain();
	assert.equal(f.statuses.at(-1), "loading");
	assert.equal(f.rendered.size, 0);
	f.pending[1].resolve({ message: [] }); await initial;
	assert.equal(f.statuses.at(-1), "fresh");
	assert.equal(f.page.dirty, false);
	f.controller.event({ topics: ["tasks"] }); await f.tick(600000);
	assert.equal(f.statuses.at(-1), "changed");
	assert.equal(f.pending.length, 2, "manual history must not poll full records");
	const refresh = f.nativeRefresh(); await drain(); f.resolvePair(2, "latest"); await refresh;
	assert.equal(f.page.dirty, false);
	assert.equal(f.statuses.at(-1), "fresh");
});

test("history menu refresh loads records as well as notifications", async () => {
	const f = workerHistoryFixture();
	const refresh = f.menus.get("刷新记录与通知")(); await drain();
	assert.equal(f.pending.length, 2);
	assert.equal(f.notificationCalls(), 1);
	f.resolvePair(0, "menu refreshed"); await refresh;
	assert.equal(f.statuses.at(-1), "fresh");
	assert.match(f.rendered.get(".worker-history-results"), /menu refreshed/);
});

test("history inline refresh preserves filters and pagination and retains events arriving mid-read", async () => {
	const f = workerHistoryFixture();
	f.filters.status = "Approved";
	f.page.worker_history.state.pagination.page = 3;
	const refresh = f.controller.refresh(true); await drain();
	assert.equal(f.pending[0].args.args.status, "Approved");
	assert.equal(f.pending[0].args.args.page, 3);
	assert.equal(f.pending[1].args.args.limit, 100);
	f.controller.event({ topics: ["tasks"] });
	f.resolvePair(0, "first", 3); await refresh;
	assert.equal(f.page.dirty, true);
	assert.equal(f.statuses.at(-1), "changed");
	const retry = f.controller.refresh(true); await drain();
	assert.equal(f.pending[2].args.args.page, 3);
	f.resolvePair(2, "latest", 3); await retry;
	assert.equal(f.page.dirty, false);
	assert.equal(f.statuses.at(-1), "fresh");
});

test("history partial failure keeps old records and an error until explicit retry succeeds", async () => {
	const f = workerHistoryFixture();
	const initial = f.nativeRefresh(); await drain(); f.resolvePair(0, "old"); await initial;
	const oldHtml = f.rendered.get(".worker-history-results");
	const refresh = f.controller.refresh(true); await drain();
	f.pending[2].resolve({ message: { rows: [{ operation: "new" }] } });
	f.pending[3].reject(Error("exceptions offline")); await refresh;
	assert.equal(f.rendered.get(".worker-history-results"), oldHtml);
	assert.equal(f.statuses.at(-1), "error");
	await f.tick(120000); assert.equal(f.pending.length, 4);
	const retry = f.controller.refresh(true); await drain(); f.resolvePair(4, "new"); await retry;
	assert.equal(f.statuses.at(-1), "fresh");
	assert.match(f.rendered.get(".worker-history-results"), /new/);
});

test("history queued filters wait for the whole earlier batch and late routes never render", async () => {
	const f = workerHistoryFixture();
	const first = f.nativeRefresh(); await drain();
	f.filters.status = "Approved";
	const approved = f.page.load({ page: 1 });
	f.pending[0].reject(Error("obsolete filter failed")); await drain();
	assert.equal(f.pending.length, 2, "the other old request is still running");
	f.pending[1].resolve({ message: [] }); await first; await drain();
	assert.equal(f.pending.length, 4);
	assert.equal(f.pending[2].args.args.status, "Approved");
	f.filters.status = "Rejected";
	const rejected = f.page.load({ page: 1 });
	f.resolvePair(2, "approved"); await approved; await drain();
	assert.equal(f.rendered.size, 0);
	f.resolvePair(4, "rejected"); await rejected;
	assert.match(f.rendered.get(".worker-history-results"), /rejected/);
	const late = f.nativeRefresh(); await drain(); f.leave(); f.resolvePair(6, "late"); await late;
	assert.doesNotMatch(f.rendered.get(".worker-history-results"), /late/);
});

test("batched reads retain pending and stale markers from any section", async () => {
	for (const flag of ["_refresh_pending", "_refresh_stale"]) {
		const f = fixture(); let applied = 0;
		f.page.manual = true;
		const read = trackedReader(f, async ({ method }) => ({ message: method === "exceptions" ? { [flag]: true } : {} }));
		f.controller.activate(f.page);
		assert.equal(await read(f.page, { requests: { reports: { method: "reports" }, exceptions: { method: "exceptions" } },
			apply() { applied++; } }), false);
		assert.equal(f.page.dirty, true);
		assert.equal(applied, flag === "_refresh_pending" ? 0 : 1);
	}
});

test("entering a page reuses its initial version check and finishes with one business read", async () => {
	const f = fixture(), statuses = [];
	f.page.interval = 30000;
	f.page.lastRead = 1000000;
	f.page.status = (state) => statuses.push(state);
	let resolveVersion, resolveData, requests = 0;
	f.options.check = () => new Promise((resolve) => { resolveVersion = resolve; });
	const read = trackedReader(f, () => { requests++; return new Promise((resolve) => { resolveData = resolve; }); });
	f.controller.activate(f.page);
	const initial = read(f.page, { method: "production", apply() {} });
	await drain();
	assert.equal(requests, 0);
	resolveVersion({ versions: { tasks: "A" } }); await drain();
	assert.equal(requests, 1);
	resolveData({ message: { rows: [1] } }); assert.equal(await initial, true); await drain();
	assert.equal(f.page.dirty, false);
	assert.equal(statuses.at(-1), "fresh");
	assert.equal(statuses.includes("waiting"), false);
	await f.tick(30000);
	assert.equal(f.counters().loads, 0);
	assert.equal(requests, 1);
});

test("manual reads clear a waiting banner and cancel the redundant automatic read", async () => {
	const f = fixture(), statuses = [];
	f.page.status = (state) => statuses.push(state);
	f.controller.activate(f.page); await drain(); await f.tick(3000);
	const baseline = f.counters();
	f.controller.event({ topics: ["tasks"] });
	assert.equal(statuses.at(-1), "waiting");
	const read = trackedReader(f, async () => ({ message: {} }));
	await read(f.page, { method: "tasks", apply() {} }); await drain();
	assert.equal(statuses.at(-1), "fresh");
	assert.equal(f.page.dirty, false);
	await f.tick(30000);
	assert.deepEqual(f.counters(), baseline);
});

test("initial business reads do not wait for a slow notification dropdown", async () => {
	const f = fixture(); let resolveNotifications, requests = 0;
	f.options.notifications = () => new Promise((resolve) => { resolveNotifications = resolve; });
	const read = trackedReader(f, async () => { requests++; return { message: {} }; });
	f.controller.activate(f.page);
	await read(f.page, { method: "tasks", apply() {} });
	assert.equal(requests, 1);
	assert.equal(f.page.dirty, false);
	resolveNotifications(); await drain();
	await f.tick(3000); assert.equal(f.counters().loads, 0);
});

test("a real change during the initial read still schedules exactly one follow-up", async () => {
	const f = fixture(); let resolveData, applied = 0;
	const read = trackedReader(f, () => new Promise((resolve) => { resolveData = resolve; }));
	f.controller.activate(f.page);
	const initial = read(f.page, { method: "tasks", apply() { applied++; } }); await drain();
	for (let i = 0; i < 20; i++) f.controller.event({ topics: ["tasks"] });
	await f.tick(10000); assert.equal(f.counters().loads, 0);
	resolveData({ message: {} }); await initial; await drain();
	assert.equal(applied, 1);
	assert.equal(f.page.dirty, true);
	await f.tick(2999); assert.equal(f.counters().loads, 0);
	await f.tick(1); assert.equal(f.counters().loads, 1);
});

test("switching routes during a version check uses the new route's baseline", async () => {
	const f = fixture(), pending = [], requests = [], statuses = [];
	let current = f.page;
	f.options.check = ({ topics }) => new Promise((resolve) => pending.push({ topics, resolve }));
	const second = { topics: ["production"], interval: 30000, status: (s) => statuses.push(s) };
	const read = trackedReader(f, async ({ method }) => { requests.push(method); return { message: {} }; }, (page) => page === current);
	f.controller.activate(f.page);
	const firstRead = read(f.page, { method: "tasks", apply() { throw Error("old route"); } }); await drain();
	current = second;
	f.controller.activate(second);
	const secondRead = read(second, { method: "production", apply() {} }); await drain();
	pending[0].resolve({ versions: { tasks: "A" } }); await drain();
	assert.equal(await firstRead, false);
	assert.deepEqual(pending[1].topics, ["notifications", "production"]);
	assert.equal(requests.length, 0);
	pending[1].resolve({ versions: { production: "B" } });
	assert.equal(await secondRead, true); await drain();
	assert.deepEqual(requests, ["production"]);
	assert.equal(second.dirty, false);
	assert.equal(statuses.at(-1), "fresh");
});

test("backend stale snapshots keep the pending state after a successful display", async () => {
	const f = fixture();
	const read = trackedReader(f, async () => ({ message: { rows: [1], _refresh_stale: true } }));
	f.controller.activate(f.page);
	assert.equal(await read(f.page, { method: "tasks", apply() {} }), false); await drain();
	assert.equal(f.page.dirty, true);
	await f.tick(3000); assert.equal(f.counters().loads, 1);
});

test("failed reads retain the error message throughout retry backoff", async () => {
	const f = fixture(), statuses = [];
	f.page.status = (s) => statuses.push(s);
	f.page.load = async () => { throw Error("offline"); };
	f.controller.activate(f.page); await drain(); await f.tick(1000);
	assert.equal(statuses.at(-1), "error");
	await f.tick(29999); assert.equal(statuses.at(-1), "error");
});

test("manual read failure can recover without leaving an obsolete error or retry timer", async () => {
	const f = fixture(), statuses = [];
	f.page.status = (s) => statuses.push(s);
	f.controller.activate(f.page); await drain(); await f.tick(3000);
	let fail = true;
	const read = trackedReader(f, async () => { if (fail) throw Error("offline"); return { message: {} }; });
	await assert.rejects(read(f.page, { method: "tasks", apply() {} })); await drain();
	assert.equal(statuses.at(-1), "error");
	fail = false;
	await read(f.page, { method: "tasks", apply() {} }); await drain();
	assert.equal(statuses.at(-1), "fresh");
	assert.equal(f.page.failures, 0);
	const baseline = f.counters().loads;
	await f.tick(30000); assert.equal(f.counters().loads, baseline);
});
