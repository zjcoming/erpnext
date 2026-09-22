const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createPageReadLoader } = require("../../public/js/page_refresh.js");

const shortage = (company, item = "RM-1") => ({ company, item_code: item, item_name: item,
	shortage_qty: 10, required_qty: 10, stock_uom: "Nos", warehouse: company + " Stores", sources: [] });

function pageHarness(respond = ({ args }) => ({ message: { shortages: [shortage(args.company), shortage(args.company, "RM-2")] } })) {
	const nodes = new Map(), reads = [];
	function node(key) {
		if (nodes.has(key)) return nodes.get(key);
		const value = { fields: {}, handlers: new Map(),
			find: node, appendTo() { return this; },
			html(entry) { this.content = entry; return this; }, empty() { this.content = ""; return this; },
			text(entry) { this.content = entry; return this; },
			val(entry) { if (entry === undefined) return this.value; this.value = entry; return this; },
			prop(key, entry) { if (entry === undefined) return this.fields[key]; this.fields[key] = entry; return this; },
			hide() { this.visible = false; return this; }, toggle(entry) { this.visible = entry; return this; },
			on(events, selector, callback) { this.handlers.set(events + "|" + selector, callback); return this; },
		};
		nodes.set(key, value);
		return value;
	}
	const page = { main: node("main"), btn_primary: node("primary"),
		add_inner_button() {}, set_primary_action() {}, clear_primary_action() {},
		ps_refresh: { dirty: false, editable: () => false, resetDirty() { this.dirty = false; } },
	};
	const window = { location: { search: "" }, process_simplification: { item_identity: {
		itemIdentityHtml: (code) => code,
	} }, history: { state: {}, replaceState(_state, _title, url) { window.location.search = new URL(url, "https://erp.test").search; } } };
	const frappe = { pages: { "shortage-purchase-planning": {} }, route_options: {},
		ui: { make_app_page: () => page, form: { make_control: () => ({ value: "2026-09-23", set_value(value) { this.value = value; }, get_value() { return this.value; } }) } },
		utils: { escape_html: String }, model: { can_create: () => true },
		datetime: { nowdate: () => "2026-09-22", add_days: () => "2026-09-23" },
		ps_read_page: createPageReadLoader((request) => { reads.push(request); return respond(request); }, () => true),
	};
	vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, "../../process_simplification/page/shortage_purchase_planning/shortage_purchase_planning.js"), "utf8"), {
		frappe, window, URLSearchParams, __: String, $: node, flt: Number, cint: Number, format_number: String,
	});
	const wrapper = {};
	const handlers = frappe.pages["shortage-purchase-planning"];
	handlers.on_page_load(wrapper);
	return { page, nodes, reads, frappe, state: wrapper.shortage_purchase_planning.state,
		open(company) { window.location.search = "?" + new URLSearchParams({ company }); frappe.route_options = {}; return handlers.on_page_show(wrapper); },
	};
}

test("same-company page return preserves manual purchase quantities and selection without another read", async () => {
	const h = pageHarness();
	assert.equal(h.reads.length, 0, "initial read starts on page show");
	await h.open("Company A");
	const rows = h.state.rows;
	h.state.rows[0].purchase_qty = 3;
	h.state.selectedIndexes = new Set([1]);
	assert.equal(await h.open("Company A"), false);
	assert.equal(h.reads.length, 1);
	assert.equal(h.state.rows, rows);
	assert.equal(h.state.rows[0].purchase_qty, 3);
	assert.deepEqual([...h.state.selectedIndexes], [1]);
});

test("switching company clears the old search and reads the new company's shortages", async () => {
	const h = pageHarness();
	await h.open("Company A");
	h.state.search = "only Company A";
	h.nodes.get(".shortage-search").val("only Company A");
	await h.open("Company B");
	assert.equal(h.reads.length, 2);
	assert.equal(h.reads.at(-1).args.company, "Company B");
	assert.equal(h.state.search, "");
	assert.equal(h.nodes.get(".shortage-search").val(), "");
	assert.ok(h.state.rows.every((row) => row.company === "Company B"));
});

test("a pending initial read stays dirty and can load on a same-company return", async () => {
	let calls = 0;
	const h = pageHarness(({ args }) => ++calls === 1 ? { message: { _refresh_pending: true } } :
		{ message: { shortages: [shortage(args.company)] } });
	assert.equal(await h.open("Company A"), false);
	assert.equal(h.state.loaded, false);
	assert.equal(h.state.loading, false);
	assert.equal(h.page.ps_refresh.dirty, true);
	assert.equal(await h.open("Company A"), true);
	assert.equal(h.reads.length, 2);
	assert.equal(h.state.loaded, true);
	assert.equal(h.state.rows[0].company, "Company A");
});

test("switching company during the initial read discards the old company's result", async () => {
	const pending = [];
	const h = pageHarness((request) => new Promise((resolve) => pending.push({ request, resolve })));
	const first = h.open("Company A");
	await new Promise(setImmediate);
	const second = h.open("Company B");
	pending[0].resolve({ message: { shortages: [shortage("Company A")] } });
	await first;
	await new Promise(setImmediate);
	assert.equal(h.state.loaded, false);
	assert.equal(h.state.rows.length, 0);
	assert.equal(h.state.loading, true);
	assert.equal(pending[1].request.args.company, "Company B");
	pending[1].resolve({ message: { shortages: [shortage("Company B")] } });
	await second;
	assert.equal(h.state.loaded, true);
	assert.equal(h.state.rows[0].company, "Company B");
	assert.equal(h.state.loading, false);
});
