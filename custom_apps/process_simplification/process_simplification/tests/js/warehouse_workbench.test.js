const test = require("node:test");
const assert = require("node:assert/strict");
const { warehouseDocumentHtml, warehouseBatchNavigationHtml } = require("../../process_simplification/page/warehouse_workbench/warehouse_workbench.js");
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
test("batch cards show each actual stock quantity and separate rejected batches", () => {
	const html = warehouseDocumentHtml({ ...doc, doctype: "Purchase Receipt", items: [{ ...doc.items[0], has_batch_no: true,
		batches: [{ batch_no: "A<01>", qty: 12.5, stock_uom: "Kg" }, { batch_no: "A02", qty: 17.5, stock_uom: "Kg" }],
		rejected_batches: [{ batch_no: "R01", qty: 2.5, stock_uom: "Kg" }],
	}] }, "receipt", esc, String);
	assert.match(html, /批次：A&lt;01&gt; · 12.5 Kg/);
	assert.match(html, /批次：A02 · 17.5 Kg/);
	assert.match(html, /拒收批次：R01 · 2.5 Kg/);
	assert.match(html, /在原单核对实际批次/);
	assert.match(html, /href="\/desk\/purchase-receipt\/PO-1"/);
});
test("ordinary cards and scoped operators do not gain empty batch UI or global report links", () => {
	assert.doesNotMatch(warehouseDocumentHtml(doc, "purchase", esc, String), /批次/);
	assert.equal(warehouseBatchNavigationHtml({ reports: [], can_configure: false }, esc), "");
	const admin = warehouseBatchNavigationHtml({ reports: [{ name: "Serial No and Batch Traceability", label: "批次追溯" }], can_configure: true }, esc);
	assert.match(admin, /Serial%20No%20and%20Batch%20Traceability/);
	assert.match(admin, /批次管理（可选）/);
	assert.match(admin, /可手动建立批次/);
});
