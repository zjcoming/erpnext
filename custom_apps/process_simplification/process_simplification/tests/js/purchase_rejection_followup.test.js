const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createPageReadLoader } = require("../../public/js/page_refresh.js");
const { rejectionScope, rejectionRoute, rejectionReturnAction, rejectionReceiptHtml, rejectionContextHtml, rejectionShortageHtml } =
	require("../../process_simplification/page/purchase_rejection_followup/purchase_rejection_followup.js");
const { purchaseReceiptQuantityLabels, labelPurchaseReceiptQuantities } = require("../../public/js/purchase_receipt.js");
const { purchaseNoticeRejectionActionHtml } = require("../../process_simplification/page/purchase_receipt_notice/purchase_receipt_notice.js");
const esc = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
const helpers = { esc, number: String };
const receipt = {
	name: "PR-1", supplier: "Supplier", company: "Company A", date: "2026-09-23", can_return: true, return_drafts: [],
	can_check_shortage: true, can_purchase: true, shortage_rows: [],
	items: [{ name: "PRI-1", item_code: "RM-1", item_name: "Copper", stock_uom: "Kg", accepted_qty: 600, rejected_qty: 100,
		returned_rejected_qty: 0, pending_return_qty: 100, warehouse: "Raw A", rejected_warehouse: "Rejected A", purchase_order: "PO-1",
		material_request: "MR-1", order_status: "To Receive", order_pending_qty: 100 }],
};

test("receipt notices link to current rejection followup only with explicit source access", () => {
	const model = { can_rejection_followup: true, rejection_followup_url: "/desk/purchase-rejection-followup?receipt=PR-1&company=A" };
	const html = purchaseNoticeRejectionActionHtml(model, esc);
	assert.match(html, /拒收与补货跟进/);
	assert.match(html, /receipt=PR-1&amp;company=A/);
	assert.match(html, /阅读通知不会完成退换货/);
	assert.equal(purchaseNoticeRejectionActionHtml({ ...model, can_rejection_followup: false }, esc), "");
	assert.equal(purchaseNoticeRejectionActionHtml({ ...model, rejection_followup_url: "javascript:alert(1)" }, esc), "");
	assert.equal(purchaseNoticeRejectionActionHtml({ ...model, rejection_followup_url: "https://untrusted.test" }, esc), "");
});

test("rejection detail separates accepted, rejected, returned and pending quantities and procurement steps", () => {
	const html = rejectionContextHtml(receipt, helpers);
	assert.match(html, /合格入库<\/dt><dd>600 Kg/);
	assert.match(html, /原拒收<\/dt><dd>100 Kg/);
	assert.match(html, /已退拒收<\/dt><dd>0 Kg/);
	assert.match(html, /拒收待退<\/dt><dd>100 Kg/);
	assert.match(html, /登记拒收退货/);
	assert.match(html, /原供应商补送则从原采购单登记收货/);
	assert.match(html, /关闭原采购单剩余量/);
	assert.match(html, /其他申请到货不回填本申请/);
	assert.match(html, /不会自动创建补货采购单/);
	assert.match(html, /purchase-order\/PO-1/);
	assert.match(html, /material-request\/MR-1/);
});

test("existing drafts take precedence over a new return and remain visible without create permission", () => {
	const model = { ...receipt, can_return: false, return_drafts: ["RT-1", "RT-2"] };
	assert.deepEqual(rejectionReturnAction(model), { type: "draft", name: "RT-1" });
	const html = rejectionContextHtml(model, helpers);
	assert.match(html, /继续退货草稿/);
	assert.match(html, /purchase-receipt\/RT-1/);
	assert.match(html, /purchase-receipt\/RT-2/);
	assert.match(html, /草稿尚未扣减拒收仓库存/);
	assert.doesNotMatch(html, /data-rejection-return/);
});

test("completed returns stop offering return actions while keeping supply followup available", () => {
	const model = { ...receipt, items: [{ ...receipt.items[0], pending_return_qty: 0, returned_rejected_qty: 100 }] };
	assert.deepEqual(rejectionReturnAction(model), { type: "none" });
	const html = rejectionContextHtml(model, helpers);
	assert.doesNotMatch(html, /data-rejection-return/);
	assert.match(html, /拒收品已退回/);
	assert.match(html, /shortage-purchase-planning\?company=Company\+A/);
	assert.match(html, /合格入库<\/dt><dd>600 Kg/);
});

test("permission-limited rows expose only returned document links and no new return button", () => {
	const model = { ...receipt, can_return: false, can_check_shortage: false, can_purchase: false,
		items: [{ ...receipt.items[0], purchase_order: null, material_request: null }] };
	const html = rejectionContextHtml(model, helpers);
	assert.doesNotMatch(html, /data-rejection-return|purchase-order\/|material-request\/|shortage-purchase-planning\?/);
	assert.match(html, /当前账号不能登记退货/);
	assert.match(html, /当前账号无法核对生产缺口/);
});

test("failed demand reads never claim zero shortage, even if stale shortage data exists", () => {
	const html = rejectionShortageHtml({ ...receipt, shortage_error: true }, helpers);
	assert.match(html, /当前缺料未能核对/);
	assert.doesNotMatch(html, /没有待补采缺口|当前缺口 0|href=/);
	const shortage = rejectionShortageHtml({ ...receipt, shortage_rows: [{ item_code: "RM-1", stock_uom: "Kg", warehouse: "Raw A", shortage_qty: 350 }] }, helpers);
	assert.match(shortage, /当前缺口 350 Kg/);
});

test("record routes retain company and history scope without permitting markup injection", () => {
	const scope = { company: "Factory A & B", receipt: "PR/1", view: "all" };
	assert.deepEqual(rejectionScope({}, new URL(rejectionRoute(scope), "https://erp.test").search), scope);
	assert.equal(rejectionScope({ view: "unknown" }, "?view=all").view, "pending");
	const html = rejectionReceiptHtml({ ...receipt, supplier: '<img src=x onerror="alert(1)">', company: 'A"><img src=x>',
		items: [{ ...receipt.items[0], item_name: "<script>bad()</script>" }] }, helpers, false, { view: "all" });
	assert.doesNotMatch(html, /<img|<script>/);
	assert.match(html, /&amp;view=all/);
});

test("receiving captions distinguish accepted quantities without changing amounts or return semantics", () => {
	assert.equal(purchaseReceiptQuantityLabels(false).qty, "合格数量");
	assert.equal(purchaseReceiptQuantityLabels(true).qty, "退货数量");
	const changes = [];
	const frm = { doc: { is_return: 0, items: [{ qty: 600, received_qty: 700, rejected_qty: 100 }] },
		fields_dict: { items: { grid: { update_docfield_property: (...args) => changes.push(args) } } } };
	const original = JSON.stringify(frm.doc);
	global.__ = String;
	try { labelPurchaseReceiptQuantities(frm); } finally { delete global.__; }
	assert.equal(JSON.stringify(frm.doc), original);
	assert.ok(changes.some(([field, property, value]) => field === "received_qty" && property === "label" && value === "到货总数"));
	assert.ok(changes.some(([field, property, value]) => field === "qty" && property === "description" && value.includes("合格数量 + 拒收数量")));
});

function pageHarness(respond = () => ({ message: { rows: [], next_start: null } }), map = async () => undefined) {
	const nodes = new Map(), reads = [], mapped = [];
	function node(key) {
		if (typeof key !== "string") return key;
		if (nodes.has(key)) return nodes.get(key);
		const value = { content: "", writes: 0, attributes: {}, handlers: new Map(), find: node, appendTo() { return this; },
			html(entry) { this.content = entry; this.writes += 1; return this; }, empty() { this.content = ""; return this; },
			text(entry) { this.content = entry; return this; }, toggle(value) { this.visible = value; return this; },
			val(entry) { if (entry === undefined) return this.value; this.value = entry; return this; },
			attr(key, entry) { if (entry === undefined) return this.attributes[key]; this.attributes[key] = entry; return this; },
			prop(key, entry) { if (entry === undefined) return this.attributes[key]; this.attributes[key] = entry; return this; },
			on(event, selector, callback) { this.handlers.set(event + "|" + selector, callback); return this; },
		};
		nodes.set(key, value);
		return value;
	}
	const page = { main: node("main"), add_inner_button(label, callback) { this.refresh = callback; } };
	const window = { location: { search: "" }, history: { state: {}, replaceState(_state, _title, url) { window.location.search = new URL(url, "https://erp.test").search; } } };
	const frappe = { pages: { "purchase-rejection-followup": {} }, route_options: null,
		utils: { escape_html: esc },
		ui: { make_app_page: () => page, form: { make_control: () => ({ set_input() {} }) } },
		call: (request) => { reads.push(request); return respond(request); },
		model: { open_mapped_doc: (request) => { mapped.push(request); return map(request); } },
	};
	frappe.ps_read_page = createPageReadLoader(frappe.call, () => true);
	vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, "../../process_simplification/page/purchase_rejection_followup/purchase_rejection_followup.js"), "utf8"), {
		frappe, window, URLSearchParams, __: String, $: node, setTimeout, clearTimeout,
	});
	const wrapper = {};
	const handlers = frappe.pages["purchase-rejection-followup"];
	handlers.on_page_load(wrapper);
	const root = [...nodes.values()].find((value) => value.handlers.size);
	return { nodes, reads, mapped, state: wrapper.purchase_rejection_followup.state, page,
		open(scope) { window.location.search = new URL(rejectionRoute(scope), "https://erp.test").search; return handlers.on_page_show(wrapper); },
		clickReturn() { return root.handlers.get("click|[data-rejection-return]")({ currentTarget: node("return-button") }); },
	};
}

test("page distinguishes an empty queue from a read failure and clears old receipt actions", async () => {
	let failure = false;
	const h = pageHarness(() => { if (failure) throw new Error("PermissionError"); return { message: { rows: [], next_start: null } }; });
	await h.open({ company: "Company A" });
	assert.match(h.nodes.get(".rejection-results").content, /当前没有拒收品待退回/);
	failure = true;
	await h.page.refresh();
	assert.match(h.nodes.get(".rejection-results").content, /读取失败/);
	assert.doesNotMatch(h.nodes.get(".rejection-results").content, /当前没有/);
	assert.equal(h.state.model, null);
});

test("changing company while a read is in flight cannot display old company records", async () => {
	const pending = [];
	const h = pageHarness((request) => new Promise((resolve) => pending.push({ request, resolve })));
	const first = h.open({ company: "Company A" });
	await new Promise(setImmediate);
	const second = h.open({ company: "Company B" });
	pending[0].resolve({ message: { rows: [receipt], next_start: null } });
	await first;
	await new Promise(setImmediate);
	assert.equal(h.state.model, null);
	assert.equal(h.state.loading, true);
	pending[1].resolve({ message: { rows: [{ ...receipt, name: "PR-B", company: "Company B" }], next_start: null } });
	await second;
	assert.equal(h.reads[1].args.company, "Company B");
	assert.equal(h.state.model.rows[0].name, "PR-B");
	assert.doesNotMatch(h.nodes.get(".rejection-results").content, /Company A/);
});

test("return action opens a native unsaved mapping for the original receipt and refreshes on page return", async () => {
	let model = receipt;
	const h = pageHarness(() => ({ message: model }));
	await h.open({ receipt: receipt.name, company: receipt.company });
	await h.clickReturn();
	assert.equal(h.mapped.length, 1);
	assert.equal(h.mapped[0].method, "process_simplification.purchasing.rejections.make_rejected_return");
	assert.equal(h.mapped[0].source_name, receipt.name);
	model = { ...receipt, return_drafts: ["RT-1"] };
	await h.open({ receipt: receipt.name, company: receipt.company });
	assert.equal(h.reads.length, 2);
	assert.match(h.nodes.get(".rejection-results").content, /继续退货草稿/);
	await h.clickReturn();
	assert.equal(h.mapped.length, 1, "existing draft must prevent another mapping");
});

test("return mapping errors remain actionable and never claim a saved or submitted return", async () => {
	const h = pageHarness(() => ({ message: receipt }), async () => { throw new Error("Already exists"); });
	await h.open({ receipt: receipt.name });
	await h.clickReturn();
	assert.match(h.nodes.get(".rejection-return-error").content, /退货单未能打开/);
	assert.equal(h.nodes.get("return-button").prop("disabled"), false);
});

test("background refresh does not replace unchanged cards and removes stale actions on failure", async () => {
	let failure = false;
	const h = pageHarness(() => { if (failure) throw new Error("Offline"); return { message: receipt }; });
	await h.open({ receipt: receipt.name });
	const result = h.nodes.get(".rejection-results");
	const writes = result.writes;
	await h.page.rejection_refresh({ background: true });
	assert.equal(result.writes, writes, "unchanged cards retain focus and their DOM");
	failure = true;
	await assert.rejects(h.page.rejection_refresh({ background: true }), /Offline/);
	assert.match(result.content, /读取失败/);
	assert.doesNotMatch(result.content, /data-rejection-return/);
	assert.equal(h.state.model, null);
});

test("an in-flight return mapping blocks repeat clicks and background reads", async () => {
	let finish;
	const h = pageHarness(() => ({ message: receipt }), () => new Promise((resolve) => { finish = resolve; }));
	await h.open({ receipt: receipt.name });
	const first = h.clickReturn();
	await h.clickReturn();
	assert.equal(await h.page.rejection_refresh({ background: true }), false);
	assert.equal(h.mapped.length, 1);
	assert.equal(h.reads.length, 1);
	finish();
	await first;
	assert.equal(h.state.mapping, false);
});
