"""An explicitly selected customer return can authorize one bounded replacement chain.

The selection is a production decision, not a quality-inspection result. Native
MR/PP/WO links remain the quantity ledger; no allowance or old WO is changed.
"""
from __future__ import annotations

from contextlib import contextmanager
from math import isfinite

import frappe
from frappe.utils import flt, nowdate

RETURN_FIELD = "custom_replacement_delivery_note"
RETURN_ITEM_FIELD = "custom_replacement_delivery_note_item"
_REQUEST_FLAG = "customer_replacement_request"
_PLAN_FLAG = "customer_replacement_plan"


@contextmanager
def _scope(key, value):
	previous = frappe.flags.get(key)
	frappe.flags[key] = value
	try:
		yield
	finally:
		if previous is None:
			frappe.flags.pop(key, None)
		else:
			frappe.flags[key] = previous


def get_return_source(return_item, sales_order, sales_order_item, *, lock=False):
	so = frappe.get_doc("Sales Order", sales_order)
	so.check_permission("read")
	item = next((r for r in so.items if r.name == sales_order_item), None)
	if so.docstatus != 1 or not item:
		frappe.throw("请选择已提交销售订单中的准确明细。")
	parent = frappe.db.get_value("Delivery Note Item", return_item, "parent")
	if not parent:
		frappe.throw("退货来源明细不存在。")
	# Frappe cancel locks its parent before before_cancel. Keep DN before SOI.
	dn = frappe.get_doc("Delivery Note", parent, for_update=lock)
	dn.check_permission("read")
	row = next((r for r in dn.items if r.name == return_item), None)
	if not row or dn.docstatus != 1 or not dn.is_return or not dn.return_against:
		frappe.throw("补产来源必须是已提交、关联原发货单的客户退货明细。")
	original = frappe.get_doc("Delivery Note", dn.return_against)
	original_row = next((r for r in original.items if r.name == row.dn_detail), None)
	qty = -flt(row.stock_qty)
	if (not isfinite(qty) or qty <= 0 or original.docstatus != 1 or original.is_return
		or not original_row or original.company != so.company or dn.company != so.company
		or original.customer != so.customer or dn.customer != so.customer
		or any(r.against_sales_order != so.name or r.so_detail != item.name
			or r.item_code != item.item_code or r.stock_uom != item.stock_uom for r in (row, original_row))
		or qty > flt(original_row.stock_qty) + 1e-8):
		frappe.throw("退货来源的公司、订单明细、产品或库存数量不匹配。")
	return frappe._dict(return_item=row.name, return_delivery=dn.name, return_row=row.idx,
		returned_stock_qty=qty, warehouse=row.warehouse, posting_date=dn.posting_date,
		sales_order=so.name, sales_order_item=item.name, company=so.company, item_code=item.item_code)


def _request_chain(material_request):
	plans = frappe.get_all("Production Plan Item", filters={"material_request": material_request}, pluck="parent")
	return frappe.db.sql("""SELECT name,docstatus,status,qty,produced_qty,material_transferred_for_manufacturing
		FROM `tabWork Order` WHERE material_request=%s OR production_plan IN %s""",
		(material_request, tuple(plans) or ("",)), as_dict=True), plans


def _has_execution_history(work_orders):
	if any(flt(r.produced_qty) or flt(r.material_transferred_for_manufacturing) for r in work_orders):
		return True
	names = [r.name for r in work_orders]
	return bool(names and (
		frappe.db.exists("Stock Entry", {"work_order": ("in", names), "docstatus": ("in", [1, 2])})
		or frappe.db.exists("Job Card Work Report", {"work_order": ("in", names)})
		or frappe.db.exists("Job Card", {"work_order": ("in", names), "total_completed_qty": (">", 0)})
	))


def get_committed_qty(return_item, *, lock=False):
	# Current reads after the return lock serialize two selections of the same
	# source. Existing MR cancellation never takes the return lock in reverse.
	rows = frappe.db.sql(f"""SELECT mr.name,mr.docstatus,i.stock_qty
		FROM `tabMaterial Request` mr JOIN `tabMaterial Request Item` i ON i.parent=mr.name
		WHERE mr.{RETURN_ITEM_FIELD}=%s ORDER BY mr.name,i.name{" FOR UPDATE" if lock else ""}""",
		(return_item,), as_dict=True)
	total = 0.0
	for r in rows:
		if r.docstatus != 2:
			total += flt(r.stock_qty)
		else:
			work_orders, _ = _request_chain(r.name)
			if _has_execution_history(work_orders):
				total += flt(r.stock_qty)
	return total


@frappe.whitelist(methods=["GET"])
def get_replacement_options(sales_order, sales_order_item):
	from process_simplification.api.actions import _row_from_workbench
	_row_from_workbench(sales_order, sales_order_item)
	rows = frappe.get_all("Delivery Note Item", filters={"against_sales_order": sales_order,
		"so_detail": sales_order_item, "docstatus": 1, "stock_qty": ("<", 0)},
		fields=["name"], order_by="creation asc,name asc")
	options = []
	for row in rows:
		source = get_return_source(row.name, sales_order, sales_order_item)
		remaining = max(source.returned_stock_qty - get_committed_qty(row.name), 0)
		if remaining > 1e-8:
			options.append({key: source[key] for key in ("return_item", "return_delivery", "return_row",
				"returned_stock_qty", "warehouse", "posting_date")} | {"remaining_qty": remaining})
	return {"options": options}


def lock_current_order_coverage(sales_order, sales_order_item, item_code):
	# Native manufacture updates WO before SOI. The replacement action calls
	# here after DN, before its normal SOI/stock locks; never invert WO -> SOI.
	# This is deliberately not a repeatable-read UI snapshot.
	rows = frappe.db.sql("""SELECT name,docstatus,status,qty,produced_qty,process_loss_qty
		FROM `tabWork Order` WHERE sales_order=%s AND sales_order_item=%s AND production_item=%s
		ORDER BY name FOR UPDATE""", (sales_order, sales_order_item, item_code), as_dict=True)
	item = frappe.db.get_value("Sales Order Item", sales_order_item,
		["stock_qty", "delivered_qty", "conversion_factor"], as_dict=True, for_update=True)
	active = sum(max(flt(r.qty) - flt(r.produced_qty) - flt(r.process_loss_qty), 0) for r in rows
		if r.docstatus != 2 and r.status not in ("Completed", "Closed", "Cancelled"))
	return max(flt(item.stock_qty) - flt(item.delivered_qty) * flt(item.conversion_factor or 1)
		- active, 0)


def lock_current_reserved_qty(sales_order, sales_order_item):
	# Called after the normal Bin -> SRE pool lock. SOI.stock_reserved_qty can
	# lag native delivery/return updates; use the same raw SRE counters as the UI.
	rows = frappe.db.sql("""SELECT reserved_qty,delivered_qty,transferred_qty,consumed_qty
		FROM `tabStock Reservation Entry` WHERE docstatus=1 AND voucher_type='Sales Order'
		AND voucher_no=%s AND voucher_detail_no=%s ORDER BY name FOR UPDATE""",
		(sales_order, sales_order_item), as_dict=True)
	return sum(max(flt(r.reserved_qty) - flt(r.delivered_qty) - flt(r.transferred_qty)
		- flt(r.consumed_qty), 0) for r in rows)


def create_replacement_request(source, *, qty, bom_no, fg_warehouse, delivery_date):
	remaining = source.returned_stock_qty - get_committed_qty(source.return_item, lock=True)
	if not isfinite(flt(qty)) or flt(qty) <= 0 or flt(qty) > remaining + 1e-8:
		frappe.throw("本次补产数量超过所选退货明细尚未用于补产的库存数量。")
	doc = frappe.new_doc("Material Request")
	doc.update({"company": source.company, "material_request_type": "Manufacture",
		"transaction_date": nowdate(), "schedule_date": delivery_date or nowdate(),
		"set_warehouse": fg_warehouse, RETURN_FIELD: source.return_delivery, RETURN_ITEM_FIELD: source.return_item})
	uom = frappe.get_cached_value("Item", source.item_code, "stock_uom")
	doc.append("items", {"item_code": source.item_code, "qty": qty, "stock_qty": qty,
		"uom": uom, "stock_uom": uom, "conversion_factor": 1, "bom_no": bom_no,
		"warehouse": fg_warehouse, "schedule_date": delivery_date or nowdate(),
		"sales_order": source.sales_order, "sales_order_item": source.sales_order_item})
	# Authorized guided PP/WO action only; no public MR create/submit permission
	# is granted. Both permissions and creation-source scope end on this instance.
	previous = doc.flags.get("ignore_permissions")
	try:
		with _scope(_REQUEST_FLAG, doc):
			doc.flags.ignore_permissions = True
			doc.insert()
			doc.submit()
	finally:
		if previous is None:
			doc.flags.pop("ignore_permissions", None)
		else:
			doc.flags.ignore_permissions = previous
	return doc


def validate_replacement_request(doc, method=None):
	old = doc.get_doc_before_save()
	if not doc.get(RETURN_FIELD) and not doc.get(RETURN_ITEM_FIELD) and not (old and old.get(RETURN_ITEM_FIELD)):
		return
	if doc.is_new():
		if frappe.flags.get(_REQUEST_FLAG) is not doc:
			frappe.throw("退货补产来源只能由订单的按退货补产操作生成，不能手填或复制。")
	elif old:
		fields = ("company", "material_request_type", RETURN_FIELD, RETURN_ITEM_FIELD)
		item_fields = ("name", "item_code", "qty", "stock_qty", "uom", "stock_uom", "conversion_factor",
			"sales_order", "sales_order_item", "warehouse", "bom_no")
		if any(doc.get(k) != old.get(k) for k in fields) or len(doc.items) != len(old.items) or any(
			any(row.get(k) != previous.get(k) for k in item_fields) for row, previous in zip(doc.items, old.items)):
			frappe.throw("退货补产申请的来源、产品和数量不能修改；未执行时请按原生流程撤销整条计划。")
	if not doc.get(RETURN_FIELD) or not doc.get(RETURN_ITEM_FIELD) or doc.material_request_type != "Manufacture" or len(doc.items) != 1:
		frappe.throw("退货补产申请必须保留唯一来源和一条制造需求。")


@contextmanager
def allow_replacement_plan(plan):
	with _scope(_PLAN_FLAG, plan):
		yield


def _managed_request(name):
	return name and frappe.db.get_value("Material Request", name, RETURN_ITEM_FIELD)


def _managed_plan_request(name):
	if name:
		for mr in frappe.get_all("Production Plan Item", filters={"parent": name}, pluck="material_request"):
			if _managed_request(mr):
				return mr


def validate_replacement_plan(doc, method=None):
	old = doc.get_doc_before_save()
	if not any(_managed_request(r.material_request) for r in list(doc.po_items or []) + list(old.po_items if old else [])):
		return
	if doc.is_new() and frappe.flags.get(_PLAN_FLAG) is not doc:
		frappe.throw("退货补产计划不能复制或重复生成，请从原订单选择退货来源。")
	if old:
		fields = ("name", "material_request", "material_request_item", "sales_order", "sales_order_item", "item_code", "bom_no", "planned_qty")
		if doc.company != old.company or doc.get_items_from != old.get_items_from or len(doc.po_items) != len(old.po_items) or any(
			any(row.get(k) != previous.get(k) for k in fields) for row, previous in zip(doc.po_items, old.po_items)):
			frappe.throw("退货补产计划的来源和数量不能修改。")


def validate_replacement_work_order(doc, method=None):
	old = doc.get_doc_before_save()
	mr = next((name for name in (doc.material_request, old.material_request if old else None)
		if _managed_request(name)), None)
	plan_mr = _managed_plan_request(doc.production_plan) or _managed_plan_request(old.production_plan if old else None)
	if not mr and not plan_mr:
		return
	if old:
		fields = ("company", "material_request", "material_request_item", "sales_order", "sales_order_item",
			"production_plan", "production_plan_item", "production_plan_sub_assembly_item", "production_item", "bom_no", "qty")
		if any(doc.get(k) != old.get(k) for k in fields):
			frappe.throw("退货补产工单的来源和数量不能修改或移到其他订单。")
	if doc.is_new():
		plan = frappe.flags.get(_PLAN_FLAG)
		if not plan or doc.production_plan != plan.name:
			frappe.throw("退货补产工单只能随对应计划生成，不能复制或重复创建。")
	if doc.production_plan_sub_assembly_item:
		if doc.material_request or doc.material_request_item:
			frappe.throw("子装配工单不能重复占用成品制造申请的数量。")
		return
	if not mr or doc.material_request != plan_mr:
		frappe.throw("成品补产工单必须保留生产计划的制造申请来源。")
	if doc.is_new():
		request = frappe.get_doc("Material Request", mr)
		row = next((r for r in request.items if r.name == doc.material_request_item), None)
		if not row or request.docstatus != 1 or request.company != doc.company or doc.production_item != row.item_code or (
			doc.sales_order, doc.sales_order_item) != (row.sales_order, row.sales_order_item):
			frappe.throw("补产工单与制造申请来源不一致。")
		others = frappe.db.sql("""SELECT name,docstatus,status,qty,produced_qty,material_transferred_for_manufacturing
			FROM `tabWork Order` WHERE material_request=%s AND material_request_item=%s AND name!=%s
			ORDER BY name FOR UPDATE""", (mr, row.name, doc.name or ""), as_dict=True)
		committed = sum(flt(r.qty) for r in others if r.docstatus != 2 or _has_execution_history([r]))
		if not isfinite(flt(doc.qty)) or flt(doc.qty) <= 0 or committed + flt(doc.qty) > flt(row.stock_qty) + 1e-8:
			frappe.throw("该退货制造申请已被工单覆盖，不能重复或超量生产。")
	if doc.docstatus == 1 and frappe.db.get_value("Material Request", mr, "docstatus") != 1:
		frappe.throw("退货补产制造申请已撤销，不能提交工单。")


def before_cancel_return(doc, method=None):
	if not doc.is_return:
		return
	# Parent/DNI locks are already held by Frappe's cancel lifecycle.
	for row in sorted(doc.items, key=lambda r: r.name):
		try:
			committed_qty = get_committed_qty(row.name, lock=True)
		except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			# Abort the request for its native transaction owner; never interpret
			# an uncertain current read as permission to cancel this return.
			frappe.throw("退货单或补产计划正在更新，请刷新后重试。")
		if committed_qty > 1e-8:
			frappe.throw("该退货明细已有补产承诺或执行记录，不能撤销退货单。请先撤销完全未执行的补产链。")


def before_cancel_replacement_request(doc, method=None):
	if not doc.get(RETURN_ITEM_FIELD):
		return
	work_orders, plans = _request_chain(doc.name)
	if _has_execution_history(work_orders):
		frappe.throw("该退货补产链已有领料、生产或报工记录，不能撤销申请释放补产额度。")
	if any(r.docstatus != 2 for r in work_orders) or (plans and frappe.db.exists("Production Plan", {"name": ("in", plans), "docstatus": ("!=", 2)})):
		frappe.throw("请先按原生流程撤销全部未执行工单和生产计划，再撤销补产申请。")


def prevent_delete_replacement_request(doc, method=None):
	if doc.get(RETURN_ITEM_FIELD):
		frappe.throw("退货补产来源须保留追溯记录，不能删除申请。")
