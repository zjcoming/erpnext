const test = require("node:test");
const assert = require("node:assert/strict");
const { allocationStockTotal } = require("../../process_simplification/page/purchase_supplier_allocation/purchase_supplier_allocation.js");

test("warehouse followup links preserve company and actionable scope on navigation and reload", () => {
	const { purchaseListScope, purchaseListRoute } = require("../../process_simplification/page/purchase_supplier_allocation/purchase_supplier_allocation.js");
	const link = purchaseListRoute("工厂 A & B", "to_order");
	assert.deepEqual(purchaseListScope({}, link.split("?")[1]), { company: "工厂 A & B", view: "to_order" });
	assert.deepEqual(purchaseListScope({ company: "Company B", view: "history" }, "?company=Company+A&view=to_order"), { company: "Company B", view: "history" });
	assert.deepEqual(purchaseListScope({}, "?view=unknown"), { company: "", view: "pending" });
	assert.equal(purchaseListRoute("", "pending"), "/desk/purchase-supplier-allocation");
});

test("mixed supplier purchase units are compared in the request stock unit", () => {
	const item = { stock_uom: "Nos", uoms: [{ uom: "Box", conversion_factor: 10 }] };
	assert.equal(allocationStockTotal(item, [{ uom: "Box", qty: 6 }, { uom: "Nos", qty: 40 }]), 100);
	assert.equal(allocationStockTotal(item, [{ uom: "Box", qty: 5 }, { uom: "Nos", qty: 30 }]), 80);
});

test("each item uses its own unit conversion without sharing another item's factor", () => {
	const rows = [{ uom: "Box", qty: 2 }];
	assert.equal(allocationStockTotal({ stock_uom: "Nos", uoms: [{ uom: "Box", conversion_factor: 10 }] }, rows), 20);
	assert.equal(allocationStockTotal({ stock_uom: "Kg", uoms: [{ uom: "Box", conversion_factor: 2.5 }] }, rows), 5);
});

const { purchaseOrderStage, purchaseOverview, supplierAllocationProgress } = require("../../process_simplification/page/purchase_supplier_allocation/purchase_supplier_allocation.js");

test("draft guidance respects the current role's submit capability", () => {
	const model = { orders: [{ docstatus: 0, status: "Draft" }], items: [], can_submit: false };
	assert.match(purchaseOverview(model).hint, /待有采购单提交权限的负责人/);
	assert.match(purchaseOverview({ ...model, can_submit: true }).hint, /核对供应商、数量和价格后提交/);
});

test("partly received orders remain in supplier followup", () => {
	const po = { docstatus: 1, status: "To Receive and Bill", per_received: 50 };
	assert.equal(purchaseOrderStage(po).label, "部分到货");
	assert.equal(purchaseOverview({ orders: [po], items: [] }).waiting, 1);
});

test("supplier followup preserves per-line units, overdue days, escaping and receiving permission", () => {
	const { purchaseFollowupHtml } = require("../../process_simplification/page/purchase_supplier_allocation/purchase_supplier_allocation.js");
	const helpers = { esc: (text) => String(text || "").replaceAll("<", "&lt;").replaceAll('"', "&quot;"), number: (value) => String(value || 0) };
	const order = { name: "PO-1", supplier: "<script>", docstatus: 1, status: "To Receive and Bill", can_receive: false, items: [{ item_code: "A", qty: 10, received_qty: 4, pending_qty: 6, returned_qty: 2, uom: "Box", schedule_date: "2026-09-01", overdue_days: 7 }] };
	const html = purchaseFollowupHtml([order], helpers);
	assert.match(html, /订单待收/);
	assert.match(html, /Box/);
	assert.match(html, /逾期 7 天/);
	assert.match(html, /&lt;script>/);
	assert.doesNotMatch(html, /data-receive-purchase-order/);
	assert.match(purchaseFollowupHtml([{ ...order, can_receive: true }], helpers), /data-receive-purchase-order="PO-1"/);
});

test("draft orders take priority over remaining allocation in the next action", () => {
	const view = purchaseOverview({ orders: [{ docstatus: 0, status: "Draft" }, { docstatus: 0, status: "Draft" }], items: [{ stock_qty: 90, available_qty: 10, received_qty: 0 }] });
	assert.equal(view.title, "2 张采购单待提交");
	assert.equal(view.unallocated, 1);
	assert.equal(view.waiting, 0);
});

test("closed, cancelled and fully received orders are not waiting for receipt", () => {
	for (const [status, docstatus] of [["Closed", 1], ["Cancelled", 2], ["To Bill", 1], ["Completed", 1], ["On Hold", 1]]) {
		assert.notEqual(purchaseOrderStage({ status, docstatus }).label, "待收货");
	}
	assert.equal(purchaseOrderStage({ status: "To Receive and Bill", docstatus: 1 }).label, "待收货");
});

test("overview counts materials instead of adding incompatible units", () => {
	const view = purchaseOverview({ orders: [], items: [{ stock_qty: 9, received_qty: 9, available_qty: 0, stock_uom: "Nos" }, { stock_qty: 135000, received_qty: 135000, available_qty: 0, stock_uom: "Gram" }] });
	assert.equal(view.received, 2);
	assert.equal(view.title, "本次申请已到齐");
});

test("zero-quantity and partly received requests are not described as complete", () => {
	for (const items of [[], [{ stock_qty: 0, received_qty: 0 }], [{ stock_qty: 90, received_qty: 40 }]]) {
		assert.notEqual(purchaseOverview({ orders: [], items }).title, "本次申请已到齐");
	}
});

test("a filled quantity without a supplier is still waiting for allocation", () => {
	const item = { stock_uom: "Nos", uoms: [] };
	const progress = supplierAllocationProgress(item, 100, [{ qty: 100, uom: "Nos", supplier: "  " }]);
	assert.deepEqual(progress, { total: 0, remaining: 100, missingSupplier: true });
});

test("split suppliers use converted quantities and reveal over-allocation", () => {
	const item = { stock_uom: "Nos", uoms: [{ uom: "Box", conversion_factor: 10 }] };
	const rows = [{ supplier: "A", qty: 6, uom: "Box" }, { supplier: "B", qty: 40, uom: "Nos" }];
	assert.deepEqual(supplierAllocationProgress(item, 100, rows), { total: 100, remaining: 0, missingSupplier: false });
	assert.equal(supplierAllocationProgress(item, 90, rows).remaining, -10);
});
