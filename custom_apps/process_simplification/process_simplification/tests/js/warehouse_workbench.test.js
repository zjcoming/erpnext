const test = require("node:test");
const assert = require("node:assert/strict");
const { warehouseDocumentHtml } = require("../../process_simplification/page/warehouse_workbench/warehouse_workbench.js");
const esc = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
const doc = { name: "PO-1", doctype: "Purchase Order", company: "Company A", party: "Supplier", can_write: true,
	items: [{ item_name: "Paper", item_code: "RM-1", qty: 15, uom: "Box" }] };

test("receiving cards show remaining quantity and open the existing purchase order", () => {
	const html = warehouseDocumentHtml(doc, "purchase", esc, String);
	assert.match(html, /待收 15 Box/);
	assert.match(html, /href="\/desk\/purchase-order\/PO-1"/);
	assert.match(html, /核对采购收货/);
});
test("untrusted supplier and item names remain text", () => {
	const html = warehouseDocumentHtml({ ...doc, party: "<img src=x onerror=alert(1)>", items: [{ ...doc.items[0], item_name: "<script>bad()</script>" }] }, "purchase", esc, String);
	assert.doesNotMatch(html, /<img|<script>/);
	assert.match(html, /&lt;img/);
});
test("read-only documents never promise a posting action", () => {
	const html = warehouseDocumentHtml({ ...doc, can_write: false }, "purchase", esc, String);
	assert.match(html, /查看单据/);
	assert.doesNotMatch(html, /核对采购收货|打开处理/);
});
test("returns keep their direction and visible return state", () => {
	const html = warehouseDocumentHtml({ ...doc, doctype: "Purchase Receipt", is_return: true, items: [{ ...doc.items[0], qty: -3 }] }, "receipt", esc, String);
	assert.match(html, /退货草稿/);
	assert.match(html, /-3 Box/);
});
