from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate

from process_simplification.api.utils import apply_current_item_names
from process_simplification.batch_display import batch_navigation, document_batch_summary
from process_simplification.management_access import (
	CAPABILITY_SHORTAGE_PURCHASE,
	CAPABILITY_WAREHOUSE_WORKBENCH,
	user_company_scope,
	user_has_capability,
)

# These queues open existing standard documents; posting stays in the native form.
QUEUES = {
	"issue": ("生产发料", "Stock Entry", {"docstatus": 0, "purpose": "Material Transfer for Manufacture"}),
	"manufacture": ("完工入库", "Stock Entry", {"docstatus": 0, "purpose": "Manufacture"}),
	"purchase": ("采购待收货", "Purchase Order", {
		"docstatus": 1, "per_received": ["<", 100],
		"status": ["not in", ["Closed", "On Hold", "Cancelled", "Completed"]],
	}),
	"receipt": ("收货草稿", "Purchase Receipt", {"docstatus": 0}),
	"delivery": ("销售出库 / 退货", "Delivery Note", {"docstatus": 0}),
	"other": ("其他库存草稿", "Stock Entry", {
		"docstatus": 0, "purpose": ["not in", ["Material Transfer for Manufacture", "Manufacture"]],
	}),
}
ITEM_DOCTYPES = {
	"Stock Entry": "Stock Entry Detail",
	"Purchase Order": "Purchase Order Item",
	"Purchase Receipt": "Purchase Receipt Item",
	"Delivery Note": "Delivery Note Item",
}


def _companies(company=None):
	if not user_has_capability(CAPABILITY_WAREHOUSE_WORKBENCH):
		frappe.throw(_("没有库房工作台的访问权限。"), frappe.PermissionError)
	scope = user_company_scope()
	if scope is not None and not scope:
		frappe.throw(_("请先为库房岗位配置可访问公司。"), frappe.PermissionError)
	# A Warehouse restriction can hide the Company form because its default WIP/FG
	# warehouses differ. Only return names explicitly granted by the company scope.
	company_query = frappe.get_all if scope is not None else frappe.get_list
	companies = company_query(
		"Company", filters={"name": ["in", sorted(scope)]} if scope is not None else {},
		pluck="name", order_by="name", limit_page_length=0,
	)
	if not companies:
		frappe.throw(_("没有可访问的公司，请检查岗位的数据范围。"), frappe.PermissionError)
	if company and company not in companies:
		frappe.throw(_("没有该公司的库房访问权限。"), frappe.PermissionError)
	return companies


def _shortage_action(companies):
	from process_simplification.page_refresh import company_shortages

	materials, orders = set(), set()
	stale = False
	for company in companies:
		# Reuse the shortage page's permission-scoped snapshot and shared-supply
		# calculation. Notification read state never represents business completion.
		data = company_shortages(company=company)
		if data.get("_refresh_pending"):
			return {"status": "pending"}
		stale = stale or bool(data.get("_refresh_stale"))
		for row in data.get("shortages") or []:
			if row.get("company") not in (None, "", company) or flt(row.get("shortage_qty")) <= 0:
				continue
			materials.add((company, row.get("item_code"), row.get("warehouse")))
			orders.update(source["sales_order"] for source in row.get("sources") or [] if source.get("sales_order"))
	result = {"status": "ready", "has_pending": bool(materials), "material_count": len(materials), "sales_order_count": len(orders)}
	if stale:
		result["_refresh_stale"] = True
	return result


def _purchase_followup_action(companies):
	from process_simplification.purchasing.allocation import get_request_page

	requests = set()
	for company in companies:
		start = 0
		while start is not None:
			data = get_request_page(company=company, view="to_order", start=start, page_length=50)
			requests.update(row["name"] for row in data["rows"] if row.get("company") == company)
			start = data["next_start"]
	return {"status": "ready", "has_pending": bool(requests), "request_count": len(requests)}


def _company_action_summary(company, can_purchase):
	actions = []
	for key, doctypes, callback in (
		("shortage", ("Material Request", "Sales Order", "Work Order"), _shortage_action),
		("purchase_followup", ("Material Request",), _purchase_followup_action),
	):
		action = {"key": key, "status": "unavailable"}
		if can_purchase and all(frappe.has_permission(doctype, "read") for doctype in doctypes):
			try:
				action.update(callback([company]))
			except frappe.PermissionError:
				pass
			except Exception:
				action["status"] = "error"
				frappe.log_error(title=f"Warehouse action summary: {key}")
		actions.append(action)
	result = {"company": company, "actions": actions}
	if any(action.get("_refresh_stale") or action["status"] in {"pending", "error"} for action in actions):
		# Let the shared loader display working sections and retry; a top-level
		# _refresh_pending would suppress even unrelated stock document queues.
		result["_refresh_stale"] = True
	return result


@frappe.whitelist()
def get_action_summary(company=None):
	"""Read next actions separately from stock-document queues; never create tasks.

	Each section can fail without hiding the other or the standard document queues.
	Unavailable/error/pending sections deliberately omit counts, so a failed read
	can never appear as a confirmed zero or leak a hidden purchasing workload.
	Company groups keep every action linked to the same scope as its target page.
	"""
	companies = _companies(company)
	can_purchase = user_has_capability(CAPABILITY_SHORTAGE_PURCHASE)
	groups = [_company_action_summary(name, can_purchase) for name in ([company] if company else companies)]
	if len(groups) == 1:
		return groups[0]
	result = {"company": None, "groups": groups}
	if any(group.get("_refresh_stale") for group in groups):
		result["_refresh_stale"] = True
	return result


def _search_filters(doctype, search, item_codes=None):
	if not search:
		return []
	pattern = "%" + search + "%"
	filters = [[doctype, "name", "like", pattern]]
	fields = ("work_order",) if doctype == "Stock Entry" else (
		("customer", "customer_name") if doctype == "Delivery Note" else ("supplier", "supplier_name")
	)
	filters.extend([doctype, field, "like", pattern] for field in fields)
	filters.extend([ITEM_DOCTYPES[doctype], field, "like", pattern] for field in ("item_code", "item_name"))
	if item_codes:
		filters.append([ITEM_DOCTYPES[doctype], "item_code", "in", item_codes])
	return filters


def _read_queue(queue, companies, search="", start=0, page_length=20, item_codes=None):
	"""Scan through unreadable child-warehouse documents without losing later work.

	The cursor is a candidate offset, not a limit on the age or total number of tasks.
	Native list permissions and a complete-document permission check both apply.
	"""
	_label, doctype, base_filters = QUEUES[queue]
	if not companies or not frappe.has_permission(doctype, "read"):
		return [], None
	filters = {**base_filters, "company": ["in", companies]}
	rows, offset = [], start
	while True:
		candidates = frappe.get_list(
			doctype, filters=filters, or_filters=_search_filters(doctype, search, item_codes),
			fields=["name"], order_by="creation asc, name asc", distinct=True,
			limit_start=offset, limit_page_length=50,
		)
		for candidate in candidates:
			doc = frappe.get_doc(doctype, candidate.name)
			if doc.company in companies and frappe.has_permission(doctype, "read", doc=doc):
				if len(rows) == page_length:
					return rows, offset
				rows.append(doc)
			offset += 1
		if len(candidates) < 50:
			return rows, None


def _document_row(doc, queue, batch_summary=None):
	items = []
	for item in doc.get("items") or []:
		qty = flt(item.qty)
		if queue == "purchase":
			qty = max(flt(item.qty) - flt(item.received_qty), 0)
			if not qty:
				continue
		items.append({
			"item_code": item.item_code, "item_name": item.item_name or item.item_code,
			"qty": qty, "uom": item.uom or item.stock_uom,
			"warehouse": item.get("warehouse"), "source_warehouse": item.get("s_warehouse"),
			"target_warehouse": item.get("t_warehouse"),
			"schedule_date": item.get("schedule_date"),
			**((batch_summary or {}).get(item.get("name") or str(item.get("idx") or ""), {})),
		})
	due_date = min((item["schedule_date"] for item in items if item["schedule_date"]), default=None) if queue == "purchase" else None
	return {
		"name": doc.name, "doctype": doc.doctype, "company": doc.company,
		"party": doc.get("supplier_name") or doc.get("supplier") or doc.get("customer_name") or doc.get("customer"),
		"work_order": doc.get("work_order"), "purpose": _(doc.get("purpose")) if doc.get("purpose") else None,
		"date": doc.get("posting_date") or doc.get("transaction_date"),
		"due_date": due_date, "overdue": bool(due_date and getdate(due_date) < getdate(nowdate())),
		"is_return": bool(doc.get("is_return")), "items": items,
		"can_write": bool(frappe.has_permission(doc.doctype, "write", doc=doc)),
	}


@frappe.whitelist()
def get_workbench(company=None, queue=None, search=None, start=0, page_length=20):
	companies = _companies(company)
	selected_companies = [company] if company else companies
	search = str(search or "").strip()[:140]
	start, page_length = max(cint(start), 0), min(max(cint(page_length) or 20, 1), 50)
	if queue and queue not in QUEUES:
		frappe.throw(_("未知的库房待办类型。"))
	item_codes = frappe.get_list(
		"Item", filters={"item_name": ["like", "%" + search + "%"]}, pluck="name", limit_page_length=0,
	) if search and frappe.has_permission("Item", "read") else []
	categories = []
	for key, (label, _doctype, _filters) in QUEUES.items():
		first, _next = _read_queue(key, selected_companies, search, page_length=1, item_codes=item_codes)
		categories.append({"key": key, "label": label, "has_pending": bool(first)})
	queue = queue or next((row["key"] for row in categories if row["has_pending"]), "issue")
	documents, next_start = _read_queue(queue, selected_companies, search, start, page_length, item_codes)
	batch_item_codes = sorted({item.item_code for doc in documents for item in doc.get("items") or []})
	batch_items = set(frappe.get_all(
		"Item", filters={"name": ["in", batch_item_codes], "has_batch_no": 1}, pluck="name",
	)) if batch_item_codes and queue != "purchase" else set()
	rows = [_document_row(doc, queue, document_batch_summary(doc, check_permission=False, batch_items=batch_items)) for doc in documents]
	apply_current_item_names([item for row in rows for item in row["items"]])
	return {
		"companies": companies, "company": company, "queue": queue, "categories": categories,
		"rows": rows,
		"batch_navigation": batch_navigation(),
		"start": start, "next_start": next_start, "has_more": next_start is not None,
	}
