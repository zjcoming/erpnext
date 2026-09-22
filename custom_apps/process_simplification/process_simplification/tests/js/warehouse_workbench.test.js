const test = require("node:test");
const assert = require("node:assert/strict");
const { warehouseDocumentHtml, warehouseBatchNavigationHtml, warehouseActionSummaryHtml, warehouseWorkbenchRequests } = require("../../process_simplification/page/warehouse_workbench/warehouse_workbench.js");
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

test("new order shortages show an actionable count even with no stock documents", () => {
	const html = warehouseActionSummaryHtml({ company: "工厂 A & B", actions: [
		{ key: "shortage", status: "ready", has_pending: true, material_count: 10, sales_order_count: 1 },
		{ key: "purchase_followup", status: "ready", has_pending: false, request_count: 0 },
	] }, esc, String);
	assert.match(html, /缺料待处理/);
	assert.match(html, /10 项物料/);
	assert.match(html, /涉及 1 张销售订单/);
	assert.match(html, /查看缺料并处理/);
	assert.match(html, /company=%E5%B7%A5%E5%8E%82\+A\+%26\+B/);
	assert.doesNotMatch(html, /暂无待办|0 张申请/);
});

test("purchase coverage replaces shortage prompt with the remaining procurement step", () => {
	const html = warehouseActionSummaryHtml({ actions: [
		{ key: "shortage", status: "ready", has_pending: false, material_count: 0, sales_order_count: 0 },
		{ key: "purchase_followup", status: "ready", has_pending: true, request_count: 2 },
	] }, esc, String);
	assert.doesNotMatch(html, /缺料待处理|0 项物料/);
	assert.match(html, /2 张申请/);
	assert.match(html, /purchase-supplier-allocation\?view=to_order/);
	assert.match(html, /跟进采购单提交/);
	assert.doesNotMatch(html, /标记已读|标记完成/);
});

test("permission-limited action summaries expose neither counts nor target links", () => {
	const hidden = [{ key: "shortage", status: "unavailable", material_count: 999 }, { key: "purchase_followup", status: "unavailable" }];
	assert.equal(warehouseActionSummaryHtml({ actions: hidden }, esc, String), "");
	const partial = warehouseActionSummaryHtml({ actions: [hidden[0], { key: "purchase_followup", status: "ready", has_pending: false }] }, esc, String);
	assert.doesNotMatch(partial, /999|缺料|href=/);
	assert.match(partial, /当前没有采购申请待下单/);
});

test("unresolved reads never claim no work or retain stale counts", () => {
	for (const status of ["error", "pending"]) {
		const html = warehouseActionSummaryHtml({ actions: [{ key: "shortage", status, material_count: 99, has_pending: true }] }, esc, String);
		assert.doesNotMatch(html, /99|当前没有|href=/);
		assert.match(html, status === "error" ? /读取失败/ : /正在核对/);
	}
});

test("company scope survives links without allowing injected markup", () => {
	const html = warehouseActionSummaryHtml({ company: '\"><img src=x>', actions: [
		{ key: "purchase_followup", status: "ready", has_pending: true, request_count: 1 },
	] }, esc, String);
	assert.doesNotMatch(html, /<img/);
	assert.match(html, /&amp;view=to_order/);
});

test("stock document filters do not conceal purchasing tasks and both reads share company scope", () => {
	const requests = warehouseWorkbenchRequests({ company: "Company B", search: "STE-1", queue: "issue", cursors: [0, 50] });
	assert.deepEqual(requests.actions.args, { company: "Company B" });
	assert.deepEqual(requests.workbench.args, { company: "Company B", search: "STE-1", queue: "issue", start: 50 });
});

test("all-company mode keeps each actionable count linked to that exact company", () => {
	const html = warehouseActionSummaryHtml({ groups: [
		{ company: "Company A", actions: [{ key: "shortage", status: "ready", has_pending: true, material_count: 3, sales_order_count: 1 }] },
		{ company: "Company B", actions: [{ key: "shortage", status: "ready", has_pending: true, material_count: 7, sales_order_count: 2 }] },
		{ company: "Empty Company", actions: [{ key: "shortage", status: "ready", has_pending: false }] },
	] }, esc, String);
	assert.match(html, /Company A[\s\S]*3 项物料[\s\S]*company=Company\+A/);
	assert.match(html, /Company B[\s\S]*7 项物料[\s\S]*company=Company\+B/);
	assert.doesNotMatch(html, /Empty Company|10 项物料/);
});

test("a changed snapshot stays visibly unresolved even if its old count was zero", () => {
	const html = warehouseActionSummaryHtml({ actions: [{ key: "shortage", status: "ready", _refresh_stale: true, has_pending: false }] }, esc, String);
	assert.match(html, /正在核对最新数据/);
	assert.doesNotMatch(html, /当前没有/);
});

test("rejected receipt followup remains visible independently of shortage and pending purchase counts", () => {
	const html = warehouseActionSummaryHtml({ company: "Factory A", actions: [
		{ key: "shortage", status: "ready", has_pending: false },
		{ key: "purchase_followup", status: "ready", has_pending: false },
		{ key: "rejection_followup", status: "ready", has_pending: true, receipt_count: 2 },
	] }, esc, String);
	assert.match(html, /拒收品待退回/);
	assert.match(html, /2 张收货单/);
	assert.match(html, /purchase-rejection-followup\?company=Factory\+A/);
	assert.doesNotMatch(html, /当前没有|0 张申请/);
});

test("rejection-only summaries preserve permission, failed-read and empty-state boundaries", () => {
	assert.equal(warehouseActionSummaryHtml({ actions: [{ key: "rejection_followup", status: "unavailable", receipt_count: 999 }] }, esc, String), "");
	const error = warehouseActionSummaryHtml({ actions: [{ key: "rejection_followup", status: "error", receipt_count: 99 }] }, esc, String);
	assert.match(error, /读取失败/);
	assert.doesNotMatch(error, /99|当前没有|href=/);
	const empty = warehouseActionSummaryHtml({ actions: [{ key: "rejection_followup", status: "ready", has_pending: false }] }, esc, String);
	assert.match(empty, /当前没有拒收品待退回/);
	assert.doesNotMatch(empty, /采购申请|缺料/);
});
