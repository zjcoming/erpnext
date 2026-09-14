"""Native purchase-return and rework documents linked to material handling."""

import frappe
from frappe.utils import flt, nowdate
from process_simplification.production_exceptions.material_routes import stock_rows, EPSILON


def receipt_options(name, company):
	from process_simplification.production_exceptions.handling import authorize

	authorize(company)
	doc = frappe.get_doc("Purchase Receipt", name)
	if doc.company != company or doc.docstatus != 1 or doc.is_return:
		frappe.throw("请选择同公司已提交的原采购收货单。")
	return doc


def make_supplier_return(doc, routes):
	source = receipt_options(doc.purchase_receipt, doc.company)
	if not doc.purchase_receipt:
		frappe.throw("退供应商必须选择原采购收货单。")
	# Preserve original commercial terms. Native return validation computes
	# quantities, valuations, taxes and prevents returning more than received.
	result = frappe.copy_doc(source)
	result.docstatus = 0
	result.ignore_pricing_rule = 1
	result.set("pricing_rules", [])
	result.set_warehouse = None
	for tax in result.taxes:
		tax.docstatus = 0
		if tax.charge_type == "Actual":
			tax.tax_amount = -abs(flt(tax.tax_amount))
	result.is_return = 1
	result.return_against = source.name
	result.posting_date = nowdate()
	result.set_posting_time = 0
	result.status = "Draft"
	result.set("items", [])
	result.custom_material_handling_request = doc.name
	result.remarks = "物料处理 " + doc.name + "：" + doc.reason
	for approved in doc.items:
		candidates = [
			r
			for r in source.items
			if r.item_code == approved.item_code
			and (not approved.purchase_receipt_item or r.name == approved.purchase_receipt_item)
		]
		if len(candidates) != 1:
			frappe.throw("原采购收货单的物料来源不唯一，请在物料明细中选择原收货行。")
		original = candidates[0]
		if flt(original.conversion_factor) <= 0:
			frappe.throw("原采购收货单位换算无效。")
		for selected in stock_rows(routes[approved.material_key], approved.qty, None):
			row = frappe.copy_doc(original).as_dict()
			for key in (
				"name",
				"parent",
				"parenttype",
				"parentfield",
				"serial_and_batch_bundle",
				"rejected_serial_and_batch_bundle",
			):
				row.pop(key, None)
			qty = -flt(selected["qty"]) / flt(original.conversion_factor)
			row.update(
				docstatus=0,
				qty=qty,
				received_qty=qty,
				rejected_qty=0,
				stock_qty=-flt(selected["qty"]),
				warehouse=approved.source_warehouse,
				rejected_warehouse=None,
				purchase_receipt_item=original.name,
				batch_no=selected.get("batch_no"),
				serial_no=selected.get("serial_no"),
				use_serial_batch_fields=1,
				custom_material_source_detail=approved.source_detail,
				amount=qty * flt(original.rate),
			)
			result.append("items", row)
	result.insert(ignore_permissions=True)
	return result


def make_rework_order(doc):
	if doc.rework_order and frappe.db.exists("Work Order", doc.rework_order):
		return frappe.get_doc("Work Order", doc.rework_order)
	if not doc.rework_bom or len(doc.items) != 1:
		frappe.throw("返工请每次选择一种物料，并选择有效返工 BOM。")
	bom = frappe.get_doc("BOM", doc.rework_bom)
	if bom.docstatus != 1 or not bom.is_active or bom.company != doc.company:
		frappe.throw("返工 BOM 必须已提交、启用且属于同公司。")
	item = doc.items[0]
	required = sum(flt(r.stock_qty) for r in bom.items if r.item_code == item.item_code)
	if required <= 0 or flt(bom.quantity) <= 0:
		frappe.throw("返工 BOM 必须包含当前待返工物料。")
	qty = flt(item.qty) * flt(bom.quantity) / required
	wo = frappe.new_doc("Work Order")
	wo.company = doc.company
	wo.production_item = bom.item
	wo.bom_no = bom.name
	wo.qty = qty
	wo.source_warehouse = item.target_warehouse
	original = frappe.get_doc("Work Order", doc.work_order) if doc.work_order else None
	wo.wip_warehouse = original.wip_warehouse if original else None
	wo.fg_warehouse = original.fg_warehouse if original else None
	wo.description = "物料异常返工 " + doc.name + "：" + doc.reason
	wo.get_items_and_operations_from_bom()
	for r in wo.required_items:
		if r.item_code == item.item_code:
			r.source_warehouse = item.target_warehouse
	wo.insert(ignore_permissions=True)
	return wo


def _linked(receipt):
	return receipt.get("custom_material_handling_request") or frappe.db.get_value(
		"Material Handling Request", {"purchase_return": receipt.name}, "name"
	)


def validate_return(receipt, method=None):
	name = _linked(receipt)
	if not name:
		return
	from process_simplification.production_exceptions.handling import (
		lock_request,
		current_routes,
		validate_lots,
		validate_physical_stock,
	)

	receipt.custom_material_handling_request = name
	doc = lock_request(name)
	if (
		doc.status != "Awaiting Stock Entry"
		or doc.purchase_return != receipt.name
		or doc.action != "Supplier Return"
	):
		frappe.throw("关联物料处理已撤回或不等待此退货单。")
	if (
		not receipt.is_return
		or receipt.return_against != doc.purchase_receipt
		or receipt.company != doc.company
	):
		frappe.throw("退货单的公司或原收货单被修改。")
	source = receipt_options(doc.purchase_receipt, doc.company)
	if receipt.supplier != source.supplier:
		frappe.throw("退货供应商必须与原收货单一致。")
	expected = {}
	actual = {}
	for r in doc.items:
		candidates = [
			x
			for x in source.items
			if x.item_code == r.item_code
			and (not r.purchase_receipt_item or x.name == r.purchase_receipt_item)
		]
		if len(candidates) != 1:
			frappe.throw("原收货行不唯一，请重新申请。")
		key = (r.item_code, r.source_warehouse, r.source_detail, candidates[0].name)
		expected[key] = expected.get(key, 0) + r.qty
	for r in receipt.items:
		if r.qty >= 0:
			frappe.throw("供应商退货必须使用负数数量。")
		key = (r.item_code, r.warehouse, r.get("custom_material_source_detail"), r.purchase_receipt_item)
		actual[key] = actual.get(key, 0) + abs(flt(r.stock_qty))
	if {k: flt(v, 6) for k, v in expected.items()} != {k: flt(v, 6) for k, v in actual.items()}:
		frappe.throw("采购退货物料、数量或仓库与已确认的申请不一致。")
	routes = current_routes(doc)
	for r in doc.items:
		if r.material_key not in routes or r.qty > routes[r.material_key].requestable_qty + EPSILON:
			frappe.throw("待检料数量已变化。")
	# Reuse the same lot source check as Stock Entry, with stock-UOM quantities.
	adapter = frappe._dict(
		items=[
			frappe._dict(
				item_code=r.item_code,
				s_warehouse=r.warehouse,
				transfer_qty=abs(flt(r.stock_qty)),
				batch_no=r.batch_no,
				serial_no=r.serial_no,
				serial_and_batch_bundle=r.serial_and_batch_bundle,
				custom_material_source_detail=r.get("custom_material_source_detail"),
			)
			for r in receipt.items
		]
	)
	validate_lots(adapter, doc, routes)
	validate_physical_stock(adapter)


def complete_return(receipt, method=None):
	name = _linked(receipt)
	if not name:
		return
	from process_simplification.production_exceptions.handling import lock_request, save, notify
	from frappe.utils import now_datetime

	doc = lock_request(name)
	doc.status = "Completed"
	doc.processed_by = frappe.session.user
	doc.processed_at = now_datetime()
	save(doc)
	notify(doc)


def before_cancel_return(receipt, method=None):
	name = _linked(receipt)
	if name:
		from process_simplification.production_exceptions.handling import lock_request

		lock_request(name)


def cancel_return(receipt, method=None):
	name = _linked(receipt)
	if not name:
		return
	from process_simplification.production_exceptions.handling import lock_request, save, notify

	doc = lock_request(name)
	doc.purchase_return = None
	doc.status = "Pending Approval"
	doc.processed_by = None
	doc.processed_at = None
	save(doc)
	notify(doc)


def prevent_delete(receipt, method=None):
	if _linked(receipt):
		frappe.throw("关联物料处理的采购退货单不能删除，请撤回或取消。")


def validate_rework_order(work_order, method=None):
	name = frappe.db.get_value("Material Handling Request", {"rework_order": work_order.name}, "name")
	if not name:
		return
	from process_simplification.production_exceptions.handling import lock_request

	doc = lock_request(name)
	if doc.status != "Completed":
		frappe.throw("请先由库房完成待返工物料转仓；撤回的申请不能启动返工。")
	if doc.company != work_order.company or doc.rework_bom != work_order.bom_no:
		frappe.throw("返工工单公司或 BOM 与已确认处理单不一致。")
	required = [r for r in work_order.required_items if r.item_code == doc.items[0].item_code]
	if not required or any(r.source_warehouse != doc.items[0].target_warehouse for r in required):
		frappe.throw("返工物料必须从确认的待返工仓领取。")

	if abs(sum(flt(r.required_qty) for r in required) - flt(doc.items[0].qty)) > EPSILON:
		frappe.throw("返工工单的待返工料数量必须与确认转仓量一致；改变处理数量请重新申请。")
