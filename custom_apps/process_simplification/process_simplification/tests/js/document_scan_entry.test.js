const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// Run the actual app bootstrap: toolbar icons are unavailable on Frappe's mobile layout.
function scanEntry({ allowed = true, route = "my-production-reporting", delayedPage = false } = {}) {
	const rows = [], icons = [], visibleActions = [], observers = [], events = {}, routes = [];
	const selection = (items, selector) => {
		const matches = items.filter((row) => row.classes.has(selector.slice(1)));
		const remove = () => matches.forEach((row) => items.splice(items.indexOf(row), 1));
		return { length: matches.length, remove, parent: () => ({ remove }) };
	};
	const element = (items, classes, click) => {
		const row = { classes: new Set(classes), click };
		items.push(row);
		return {
			addClass(value) { row.classes.add(value); return this; }, attr() { return this; }, html() { return this; },
			prependTo(target) { assert.equal(target[0].kind, "standard"); items.splice(items.indexOf(row), 1); visibleActions.unshift(row); return this; },
		};
	};
	const menu = { 0: { kind: "menu" }, length: 1, find: (selector) => selection(rows, selector) };
	const page = {
		menu, icon_group: { 0: { kind: "icons" } }, standard_actions: { 0: { kind: "standard" } },
		wrapper: { attr: () => `page-${route}`, find: (selector) => selection(visibleActions, selector) },
		add_action_icon: (_icon, click, classes) => element(icons, [classes], click),
		add_custom_menu_item: (_parent, _label, click) => element(rows, [], click),
	};
	const document = {};
	const frappe = {
		boot: { page_info: allowed ? { "document-scan": {} } : {} },
		container: { page: delayedPage ? null : { page } },
		get_route: () => [route], route_options: { stale: "filter" },
		set_route: (target) => { routes.push(target); },
		after_ajax: (callback) => callback(), router: { on: (_event, callback) => { events.route = callback; } },
	};
	const $ = (value) => {
		if (typeof value === "function") { value(); return; }
		if (value === document) return { on: (event, callback) => { events[event] = callback; } };
		return { text() { return this; } };
	};
	vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, "../../public/js/document_scan.js"), "utf8"), {
		window: {}, frappe, document, $, __: (value) => value, setTimeout: (callback) => callback(),
		MutationObserver: class {
			constructor(callback) { this.callback = callback; this.targets = []; observers.push(this); }
			observe(target) { this.targets.push(target); }
		},
	});
	return { rows, icons, visibleActions, observers, page, frappe, events, routes };
}

test("worker landing exposes a direct scan button outside mobile-hidden icons, plus the menu entry", () => {
	const state = scanEntry();
	assert.equal(state.rows.length, 1);
	state.rows[0].click();
	assert.deepEqual(state.routes, ["document-scan"]);
	assert.equal(state.frappe.route_options, null);
	assert.equal(state.icons.length, 0, "the button must not stay inside the hidden mobile group");
	assert.equal(state.visibleActions.length, 1);
	state.visibleActions[0].click();
	assert.deepEqual(state.routes, ["document-scan", "document-scan"]);
});

test("async first page load and repeated route events install exactly one menu item", () => {
	const state = scanEntry({ delayedPage: true });
	assert.equal(state.rows.length, 0);
	state.frappe.container.page = { page: state.page };
	state.events.route();
	state.events.route();
	assert.equal(state.rows.length, 1);
	assert.equal(state.visibleActions.length, 1);
	assert.equal(state.observers.length, 1);
});

test("native menu rebuilding restores scanning without duplicating existing worker actions", () => {
	const state = scanEntry();
	assert.deepEqual(state.observers[0].targets.map((target) => target.kind), ["standard", "icons", "menu"]);
	state.rows.splice(0);
	const refreshAction = { classes: new Set(["worker-refresh-menu-item"]) };
	state.rows.push(refreshAction);
	state.observers[0].callback();
	state.observers[0].callback();
	assert.equal(state.rows.length, 2);
	assert.equal(state.rows[0], refreshAction);
	state.rows[1].click();
	assert.deepEqual(state.routes, ["document-scan"]);
});

test("scanner page and accounts without scan-page permission get no redundant entry", () => {
	for (const options of [{ allowed: false }, { route: "document-scan" }]) {
		const state = scanEntry(options);
		assert.equal(state.rows.length, 0);
		assert.equal(state.icons.length, 0);
		assert.equal(state.visibleActions.length, 0);
		assert.equal(state.observers.length, 0);
	}
});
