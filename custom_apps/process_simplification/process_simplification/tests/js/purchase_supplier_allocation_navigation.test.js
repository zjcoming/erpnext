const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function pageHarness() {
	const nodes = [], buttons = new Map(), reads = [], routes = [];
	function node(markup = "") {
		const value = { markup, attrs: {}, children: [], events: {}, fields: new Map(), length: 1,
			append(child) { this.children.push(child); return this; },
			appendTo(parent) { parent.append(this); return this; },
			attr(key, entry) { if (typeof key === "object") Object.assign(this.attrs, key); else if (entry !== undefined) this.attrs[key] = entry; else return this.attrs[key]; return this; },
			val(entry) { if (entry === undefined) return this.value; this.value = entry; return this; },
			text(entry) { if (entry === undefined) return this.content; this.content = entry; return this; },
			html(entry) { if (entry === undefined) return this.content; this.content = entry; this.children = []; this.fields.clear(); return this; },
			empty() { this.children = []; this.fields.clear(); return this; },
			find(selector) { if (!this.fields.has(selector)) this.fields.set(selector, node(selector)); return this.fields.get(selector); },
			addClass() { return this; }, prop() { return this; },
			on(events, ...args) { const callback = args.at(-1); for (const event of events.split(" ")) this.events[event] = callback; return this; },
			get: () => ({ isConnected: true }), map: () => ({ get: () => [] }),
		};
		nodes.push(value);
		return value;
	}
	const page = { main: node(), clear_primary_action() {},
		add_inner_button: (label, callback) => buttons.set(label, callback),
		ps_refresh: { configure() {}, resetDirty() {} },
	};
	const window = { location: { search: "" }, crypto: { randomUUID: () => String(nodes.length) },
		history: { state: {}, replaceState(_state, _title, url) { window.location.search = new URL(url, "https://erp.test").search; } },
	};
	const frappe = { pages: { "purchase-supplier-allocation": {} }, route_options: {},
		ui: { make_app_page: () => page }, utils: { escape_html: String }, model: { can_read: () => true },
		get_route: () => ["purchase-supplier-allocation"],
		set_route: (route, options) => routes.push({ route, options: JSON.parse(JSON.stringify(options || {})) }),
		call: async ({ args }) => ({ message: { material_request: args.material_request, company: "Company A", items: [], orders: [], can_create: false } }),
		ps_read_page: async (_page, options) => { reads.push(JSON.parse(JSON.stringify(options.args))); await options.apply({ message: { rows: [], next_start: null, page_length: 20 } }); return true; },
	};
	vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, "../../process_simplification/page/purchase_supplier_allocation/purchase_supplier_allocation.js"), "utf8"), {
		frappe, window, URLSearchParams, __: String, $: node, setTimeout, clearTimeout,
	});
	const wrapper = {};
	const handlers = frappe.pages["purchase-supplier-allocation"];
	handlers.on_page_load(wrapper);
	return { page, window, frappe, buttons, reads, routes, nodes,
		async open(search, options = {}) { window.location.search = search; frappe.route_options = options; await handlers.on_page_show(wrapper); },
	};
}

test("returning from another company's request detail keeps the selected company's shortage toolbar route", async () => {
	const h = pageHarness();
	await h.open("?material_request=MR-A");
	h.buttons.get("缺料采购")();
	assert.deepEqual(h.routes.at(-1), { route: "shortage-purchase-planning", options: { company: "Company A" } });
	await h.open("?company=Company+B&view=to_order");
	assert.equal(h.reads.at(-1).company, "Company B");
	h.buttons.get("缺料采购")();
	assert.deepEqual(h.routes.at(-1), { route: "shortage-purchase-planning", options: { company: "Company B" } });
});

test("actionable request scope survives toolbar refresh and a changed scope survives refresh too", async () => {
	const h = pageHarness();
	await h.open("?company=Company+B&view=to_order");
	assert.equal(h.reads.at(-1).view, "to_order");
	await h.buttons.get("刷新进度")();
	assert.equal(h.reads.at(-1).view, "to_order");
	assert.equal(h.reads.at(-1).company, "Company B");
	const selector = h.nodes.findLast((entry) => entry.markup.startsWith("<select") && entry.value === "待下单 / 确认");
	assert.ok(selector, "the actionable scope must be visible in the selector");
	selector.val("已处理历史");
	selector.events.change();
	assert.equal(new URLSearchParams(h.window.location.search).get("view"), "history");
	await h.buttons.get("刷新进度")();
	assert.equal(h.reads.at(-1).view, "history");
	assert.equal(h.reads.at(-1).company, "Company B");
});
