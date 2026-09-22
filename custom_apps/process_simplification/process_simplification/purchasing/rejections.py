"""Keep rejected deliveries actionable without inventing replacement purchases.

Stock remains in native Purchase Receipt/return documents. This projection is
rebuilt from submitted source rows, so reading a notification cannot complete it.
"""

import frappe
from frappe.utils import flt

from process_simplification.api.utils import apply_current_item_names, normalize_purchase_qty
from process_simplification.notifications import _user_matches_company
from process_simplification.purchasing.query import document_page
from process_simplification.purchasing.receipt_quantities import rejected_stock_qty


def _check_company(company):
	if company and not _user_matches_company(frappe.session.user, company):
		frappe.throw("无权查看其他公司的拒收记录。", frappe.PermissionError)


def _receipt(name):
	doc = frappe.get_doc("Purchase Receipt", name)
	doc.check_permission("read")
	_check_company(doc.company)
	if doc.docstatus != 1 or doc.is_return:
		frappe.throw("请选择已提交的原采购入库单。")
	return doc


def _readable(doctype, name):
	if not name or not frappe.has_permission(doctype, "read"):
		return None
	doc = frappe.get_doc(doctype, name)
	if _user_matches_company(frappe.session.user, doc.company) and frappe.has_permission(doctype, "read", doc=doc):
		return doc
	return None


def _returned_by_row(doc):
	returned = {}
	for row in frappe.db.sql(
		"""select i.purchase_receipt_item, i.stock_qty, i.rejected_qty,
		i.conversion_factor, i.return_qty_from_rejected_warehouse
		from `tabPurchase Receipt Item` i
		join `tabPurchase Receipt` p on p.name=i.parent
		where p.return_against=%s and p.is_return=1 and p.docstatus=1""",
		doc.name, as_dict=True,
	):
		key = row.purchase_receipt_item
		returned[key] = returned.get(key, 0) - rejected_stock_qty(row)
	return returned


def _return_drafts(doc):
	result = []
	for name in frappe.get_list(
		"Purchase Receipt", filters={"docstatus": 0, "is_return": 1, "return_against": doc.name},
		pluck="name", order_by="creation asc", limit_page_length=0,
	):
		draft = _readable("Purchase Receipt", name)
		if draft and any(rejected_stock_qty(row) < 0 for row in draft.items):
			result.append(name)
	return result


def project_receipt(doc):
	"""One receipt's physical rejected balance; every quantity uses stock UOM."""
	rejected = [row for row in doc.items if flt(row.rejected_qty) > 0]
	if not rejected:
		return None
	returned = _returned_by_row(doc)
	orders, requests, items = {}, {}, []
	for row in rejected:
		for cache, doctype, name in ((orders, "Purchase Order", row.purchase_order), (requests, "Material Request", row.material_request)):
			if name and name not in cache:
				cache[name] = _readable(doctype, name)
		po = orders.get(row.purchase_order)
		mr = requests.get(row.material_request)
		po_item = next((item for item in po.items if item.name == row.purchase_order_item), None) if po else None
		qty = rejected_stock_qty(row)
		returned_qty = max(returned.get(row.name, 0), 0)
		items.append({
			"name": row.name, "item_code": row.item_code, "item_name": row.item_name,
			"stock_uom": row.stock_uom, "accepted_qty": normalize_purchase_qty(row.stock_qty),
			"rejected_qty": normalize_purchase_qty(qty),
			"returned_rejected_qty": normalize_purchase_qty(returned_qty),
			"pending_return_qty": max(normalize_purchase_qty(qty - returned_qty), 0),
			"warehouse": row.warehouse, "rejected_warehouse": row.rejected_warehouse,
			"purchase_order": po.name if po else None, "material_request": mr.name if mr else None,
			"order_status": po.status if po else None,
			"order_pending_qty": max(normalize_purchase_qty(
				(flt(po_item.qty) - flt(po_item.received_qty)) * (flt(po_item.conversion_factor) or 1)
			), 0) if po_item and po.status not in {"Closed", "Cancelled"} else 0,
		})
	apply_current_item_names(items)
	drafts = _return_drafts(doc)
	return {
		"name": doc.name, "supplier": doc.supplier, "company": doc.company, "date": doc.posting_date,
		"items": items, "return_drafts": drafts,
		"can_return": bool(any(item["pending_return_qty"] > 0 for item in items) and frappe.has_permission("Purchase Receipt", "create")),
	}


@frappe.whitelist()
def get_page(company=None, start=0, page_length=20, search="", view="pending"):
	_check_company(company)
	if view not in {"pending", "all"}:
		frappe.throw("无效的拒收记录范围。")
	filters = {"docstatus": 1, "is_return": 0}
	if company:
		filters["company"] = company
	search = str(search or "").strip().lower()
	def project(doc):
		result = project_receipt(doc)
		if not result:
			return None
		if view == "pending" and not (result["return_drafts"] or any(row["pending_return_qty"] > 0 for row in result["items"])):
			return None
		words = " ".join([doc.name, doc.supplier or "", *[str(row.get(key) or "") for row in result["items"] for key in ("item_code", "item_name", "purchase_order", "material_request")]])
		return result if not search or search in words.lower() else None
	return document_page("Purchase Receipt", filters, project, start=start, page_length=page_length)


def get_summary(company):
	names, start = set(), 0
	while start is not None:
		result = get_page(company=company, start=start, page_length=50)
		names.update(row["name"] for row in result["rows"])
		start = result["next_start"]
	return {"status": "ready", "has_pending": bool(names), "receipt_count": len(names)}


@frappe.whitelist()
def get_context(receipt):
	result = project_receipt(_receipt(receipt))
	if not result:
		frappe.throw("这张采购入库单没有拒收物料。")
	result.update(can_check_shortage=False, can_purchase=False, shortage_rows=[])
	from process_simplification.management_access import CAPABILITY_SHORTAGE_PURCHASE, user_has_capability
	if user_has_capability(CAPABILITY_SHORTAGE_PURCHASE):
		try:
			from process_simplification.page_refresh import company_shortages
			data = company_shortages(company=result["company"])
			if data.get("_refresh_pending") or data.get("_refresh_stale"):
				result["shortage_error"] = "缺料数据正在更新，请刷新后核对。"
			else:
				keys = {(row["item_code"], row["warehouse"]) for row in result["items"]}
				result.update(
					can_check_shortage=True,
					can_purchase=bool(frappe.has_permission("Material Request", "create")),
					shortage_rows=[row for row in data.get("shortages", []) if (row.get("item_code"), row.get("warehouse")) in keys],
				)
		except frappe.PermissionError:
			result["shortage_error"] = "当前岗位无权查询完整生产缺料，请由采购负责人核对。"
		except Exception:
			frappe.log_error(title="拒收跟进读取生产缺料失败")
			result["shortage_error"] = "生产缺料暂时读取失败，请稍后刷新核对；仍可处理拒收退货。"
	return result


@frappe.whitelist()
def make_rejected_return(source_name, target_doc=None):
	"""Prepare the standard return form; native submit validates stock and limits."""
	frappe.has_permission("Purchase Receipt", "create", throw=True)
	doc = _receipt(source_name)
	context = project_receipt(doc)
	if not context or not any(row["pending_return_qty"] > 0 for row in context["items"]):
		frappe.throw("拒收物料已退完，请刷新页面核对最新记录。")
	if context["return_drafts"]:
		frappe.throw("已有拒收退货草稿，请先打开并处理：" + "、".join(context["return_drafts"]))
	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import (
		make_purchase_return_against_rejected_warehouse,
	)
	return make_purchase_return_against_rejected_warehouse(doc.name)
