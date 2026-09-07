const test = require("node:test");
const assert = require("node:assert/strict");
const { allocationStockTotal } = require("../../process_simplification/page/purchase_supplier_allocation/purchase_supplier_allocation.js");

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
