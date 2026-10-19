const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function pageHarness({ allocation = false } = {}) {
	const nodes = [], buttons = new Map(), reads = [], routes = [], controls = [], previews = [], messages = [];
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
			addClass() { return this; }, toggleClass() { return this; }, prop() { return this; },
			on(events, ...args) { const callback = args.at(-1); for (const event of events.split(" ")) this.events[event] = callback; return this; },
			get: () => ({ isConnected: true }), map: () => ({ get: () => [] }),
		};
		nodes.push(value);
		return value;
	}
	const page = { main: node(), clear_primary_action() {}, set_primary_action(_label, callback) { this.preview = callback; },
		add_inner_button: (label, callback) => buttons.set(label, callback),
		ps_refresh: { configure() {}, resetDirty() {} },
	};
	const window = { location: { search: "" }, crypto: { randomUUID: () => String(nodes.length) },
		history: { state: {}, replaceState(_state, _title, url) { window.location.search = new URL(url, "https://erp.test").search; } },
	};
	const allocationContext = { material_request: "MR-A", company: "Company A", currency: "CNY", orders: [], can_create: allocation,
		items: allocation ? [{ name: "MRI-A", item_code: "RAW", item_name: "原料", stock_uom: "Gram", uom: "Gram", uoms: [],
			stock_qty: 1, available_qty: 1, received_qty: 0, default_supplier: null, warehouse: "RAW-WH", schedule_date: "2026-10-18" }] : [] };
	const frappe = { pages: { "purchase-supplier-allocation": {} }, route_options: {},
		ui: { make_app_page: () => page, form: { make_control: ({ df }) => {
			const control = { df, $input: node("input"), value: "", inside_change_event: false,
				set_input(value) { this.value = value || ""; }, get_value() { return this.value; },
				beginValidation(value) { this.value = value; this.inside_change_event = true; },
				finishValidation(value) { this.value = value || ""; this.inside_change_event = false; this.df.change.call(this); },
			};
			controls.push(control); return control;
		} } }, datetime: { nowdate: () => "2026-10-17" }, msgprint: (message) => messages.push(message), utils: { escape_html: String }, model: { can_read: () => true },
		get_route: () => ["purchase-supplier-allocation"],
		set_route: (route, options) => routes.push({ route, options: JSON.parse(JSON.stringify(options || {})) }),
		call: async ({ method, args }) => {
			if (method.endsWith("get_allocation_context")) return { message: { ...allocationContext, material_request: args.material_request } };
			assert.ok(method.endsWith("preview_allocation"), "the harness must never create purchase orders");
			previews.push(JSON.parse(JSON.stringify(args))); return { message: { groups: [] } };
		},
		ps_read_page: async (_page, options) => { reads.push(JSON.parse(JSON.stringify(options.args))); await options.apply({ message: { rows: [], next_start: null, page_length: 20 } }); return true; },
	};
	frappe.ui.Dialog = class { constructor() { this.fields_dict = { preview: { $wrapper: node() } }; } show() {} };
	vm.runInNewContext(fs.readFileSync(process.env.PURCHASE_ALLOCATION_SOURCE || path.resolve(__dirname, "../../process_simplification/page/purchase_supplier_allocation/purchase_supplier_allocation.js"), "utf8"), {
		frappe, window, URLSearchParams, __: String, $: node, setTimeout, clearTimeout, document: { createTextNode: (value) => value },
	});
	const wrapper = {};
	const handlers = frappe.pages["purchase-supplier-allocation"];
	handlers.on_page_load(wrapper);
	return { page, window, frappe, buttons, reads, routes, nodes, previews, messages,
		supplier: () => controls.find((control) => control.df.label === "供应商"),
		setRate(value) {
			const input = nodes.find((entry) => entry.markup.startsWith("<input") && nodes.some((holder) => holder.children.includes(entry) && holder.children.some((child) => child?.content === "单价（CNY）")));
			assert.ok(input); input.val(value); input.events.input();
		},
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


// Native ControlLink's asynchronous implementation is independently exercised in
// the acceptance harness; these page tests control its pending/settled contract.
test("supplier validation must settle before preview sends the selected supplier and edited price", async () => {
	const h = pageHarness({ allocation: true }); await h.open("?material_request=MR-A");
	const supplier = h.supplier(); supplier.beginValidation("供应商甲"); h.setRate(1);
	await h.page.preview();
	assert.equal(h.previews.length, 0); assert.match(h.messages.at(-1), /正在校验/);
	supplier.finishValidation("SUP-A"); await h.page.preview();
	assert.equal(h.previews.length, 1); assert.equal(h.previews[0].data.rows[0].supplier, "SUP-A");
	assert.equal(h.previews[0].data.rows[0].rate, 1);
});

test("changing supplier while Link validation is pending cannot preview the old supplier", async () => {
	const h = pageHarness({ allocation: true }); await h.open("?material_request=MR-A");
	const supplier = h.supplier(); supplier.finishValidation("SUP-A"); supplier.beginValidation("供应商乙");
	await h.page.preview(); assert.equal(h.previews.length, 0);
	supplier.finishValidation("SUP-B"); await h.page.preview();
	assert.equal(h.previews.length, 1); assert.equal(h.previews[0].data.rows[0].supplier, "SUP-B");
});

test("empty or natively rejected supplier does not send a preview request", async () => {
	const h = pageHarness({ allocation: true }); await h.open("?material_request=MR-A");
	await h.page.preview(); assert.equal(h.previews.length, 0); assert.match(h.messages.at(-1), /选择供应商/);
	h.supplier().beginValidation("INVALID"); h.supplier().finishValidation(undefined);
	await h.page.preview(); assert.equal(h.previews.length, 0); assert.match(h.messages.at(-1), /选择供应商/);
});
