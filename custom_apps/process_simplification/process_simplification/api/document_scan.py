"""Resolve a scan without creating or changing any business document."""

import re

import frappe
from frappe import _

from process_simplification.document_scan import SCAN_TYPES, decode_document_scan, get_scan_site_id
from process_simplification.production_reporting.domain import employee_for_user, require_worker, user_roles

NO_TASK_MESSAGE = "这张任务单不在你当前可操作的任务中，请联系主管确认。"
SEARCH_TYPES = tuple(SCAN_TYPES.values())
SEARCH_LIMIT = 20
SEARCH_FIELDS = {
	"Purchase Order": ["name", "supplier_name", "status", "transaction_date"],
	"Job Card": ["name", "item_name", "production_item", "operation", "work_order", "status"],
	"Purchase Receipt": ["name", "supplier_name", "status", "posting_date", "is_return"],
	"Stock Entry": ["name", "stock_entry_type", "purpose", "work_order", "posting_date", "docstatus"],
	"Delivery Note": ["name", "customer_name", "status", "posting_date", "is_return"],
}
SEARCHABLE_FIELDS = {
	"Purchase Order": ["name", "supplier_name"],
	"Job Card": ["name", "item_name", "production_item", "operation", "work_order"],
	"Purchase Receipt": ["name", "supplier_name"],
	"Stock Entry": ["name", "stock_entry_type", "work_order"],
	"Delivery Note": ["name", "customer_name"],
}


def _validate_search_input(value, doctype):
	if frappe.session.user == "Guest":
		frappe.throw(_("请先登录后再查找单据。"), frappe.PermissionError)
	value = value.strip() if isinstance(value, str) else ""
	if (
		not value or len(value) > 140 or re.search(r"[\x00-\x1f\x7f]", value)
		or value.startswith("HSERP|")
		or re.match(r"(?i)^(?:[a-z][a-z0-9+.-]*:|//|/desk/)", value)
	):
		frappe.throw(_("请输入单据编号或名称关键词；二维码请使用扫一扫。"))
	if doctype not in (None, "", *SEARCH_TYPES):
		frappe.throw(_("请选择支持的采购、生产或库存单据类型。"))
	return value


@frappe.whitelist(methods=["GET"])
def search_documents(query: str | None = None, doctype: str | None = None):
	"""Return bounded, visible candidates only. No routing or business actions during search."""
	query = _validate_search_input(query, doctype)
	# Treat LIKE wildcards as literal input, not as a request to enumerate all documents.
	pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
	worker = "Production Worker" in user_roles()
	results = []
	for kind in ([doctype] if doctype else SEARCH_TYPES):
		filters = {}
		reader = frappe.get_list
		if kind == "Job Card" and worker:
			require_worker()
			employee = employee_for_user(required=False)
			if not employee:
				continue
			# Workers use the existing assignment API scope, not native Job Card form permissions.
			assigned = frappe.get_all(
				"Job Card Worker Assignment", filters={"employee": employee, "status": "Active"},
				pluck="job_card",
			)
			if not assigned:
				continue
			filters["name"] = ["in", list(set(assigned))]
			reader = frappe.get_all
		fields = SEARCH_FIELDS[kind]
		searchable = SEARCHABLE_FIELDS[kind]
		previous_mute = frappe.flags.mute_messages
		frappe.flags.mute_messages = True
		try:
			# Give an exact number priority even if it is older than the first page of matches.
			exact_filters = [[key, *value] for key, value in filters.items()]
			exact_filters.append(["name", "=", query])
			exact = reader(kind, fields=fields, filters=exact_filters, limit_page_length=1)
			matches = reader(
				kind, fields=fields, filters=filters,
				or_filters=[[field, "like", pattern] for field in searchable],
				order_by="modified desc, name asc", limit_page_length=SEARCH_LIMIT + 1,
			)
		except frappe.PermissionError:
			continue
		finally:
			frappe.flags.mute_messages = previous_mute
		seen = set()
		for row in [*exact, *matches]:
			if row.name in seen:
				continue
			seen.add(row.name)
			if kind == "Job Card":
				title = row.get("item_name") or row.get("production_item")
				detail = " · ".join(str(row.get(key)) for key in ("operation", "work_order") if row.get(key))
			else:
				title = row.get("supplier_name") or row.get("customer_name") or _(row.get("stock_entry_type") or "")
				detail = " · ".join(str(value) for value in [row.get("work_order"), row.get("transaction_date") or row.get("posting_date"), "退货" if row.get("is_return") else None] if value)
			state = {0: "草稿", 1: "已提交", 2: "已取消"}.get(row.get("docstatus"), "") if kind == "Stock Entry" else row.get("status")
			results.append({
				"doctype": kind, "name": row.name,
				"title": title, "detail": detail,
				"status": _(state or ""),
			})
	results.sort(key=lambda row: (row["name"].casefold() != query.casefold(), SEARCH_TYPES.index(row["doctype"])))
	return {"results": results[:SEARCH_LIMIT], "has_more": len(results) > SEARCH_LIMIT}


@frappe.whitelist(methods=["GET"])
def open_result(doctype: str, name: str):
	"""Recheck access at the click; candidates are not an authorization token."""
	name = _validate_search_input(name, doctype)
	if doctype not in SEARCH_TYPES:
		frappe.throw(_("请选择要打开的单据。"))
	if doctype == "Job Card" and "Production Worker" not in user_roles():
		return _read_document_route(doctype, name)
	return _resolve_target(doctype, name)


@frappe.whitelist(methods=["GET"])
def resolve(code: str | None = None):
	if frappe.session.user == "Guest":
		frappe.throw(_("请先登录后再扫码。"), frappe.PermissionError)
	try:
		doctype, name = decode_document_scan(code, get_scan_site_id())
	except ValueError as exc:
		frappe.throw(_(str(exc)))
	return _resolve_target(doctype, name)


@frappe.whitelist(methods=["GET"])
def find_by_name(name: str | None = None, doctype: str | None = None):
	"""Exact manual lookup; cameras continue to require a site-bound internal code."""
	if frappe.session.user == "Guest":
		frappe.throw(_("请先登录后再查找单据。"), frappe.PermissionError)
	name = name.strip() if isinstance(name, str) else ""
	if not name or len(name) > 140 or re.search(r"[\x00-\x1f\x7f]", name) or name.startswith("HSERP|") or re.match(r"(?i)^(?:[a-z][a-z0-9+.-]*:|//|/desk/)", name):
		frappe.throw(_("请输入完整单据编号，或扫描系统内二维码。"))
	if doctype not in (None, "", "Purchase Order", "Job Card"):
		frappe.throw(_("仅支持采购订单和生产任务单。"))
	if doctype:
		return _resolve_target(doctype, name)
	# Return only accessible results; never disclose the existence of hidden documents.
	results = [_resolve_target(kind, name) for kind in ("Purchase Order", "Job Card")]
	found = [result for result in results if result["found"]]
	if len(found) == 1:
		return found[0]
	if len(found) > 1:
		return {"found": False, "message": _("存在相同编号，请选择单据类型后再查找。")}
	return {"found": False, "message": _("未找到可查看的单据。请核对完整编号；生产任务还需由主管派给当前账号。")}


def _resolve_target(doctype, name):

	if doctype in SEARCH_TYPES and doctype != "Job Card":
		return _read_document_route(doctype, name)
	if doctype != "Job Card":
		return {"found": False, "message": _("不支持的单据类型。")}

	# Resolve only the signed-in worker's assignment, never another employee's task.
	if "Production Worker" not in user_roles():
		return {"found": False, "message": _(NO_TASK_MESSAGE)}
	require_worker()
	employee = employee_for_user(required=False)
	if not employee:
		return {"found": False, "message": _(NO_TASK_MESSAGE)}
	assignment = frappe.db.get_value(
		"Job Card Worker Assignment",
		{"employee": employee, "job_card": name, "status": "Active"},
		"name",
	)
	if not assignment:
		return {"found": False, "message": _(NO_TASK_MESSAGE)}
	active = frappe.db.exists("Job Card Work Report", {"assignment": assignment, "status": "In Progress"})
	return {
		"found": True,
		"route": ["active-production-work" if active else "my-production-reporting"],
		"job_card": name,
	}


def _read_document_route(doctype, name):
	previous_mute = frappe.flags.mute_messages
	frappe.flags.mute_messages = True
	try:
		doc = frappe.get_doc(doctype, name)
		doc.check_permission("read")
	except (frappe.DoesNotExistError, frappe.PermissionError):
		message = "找不到这张单据，或你没有查看权限。"
		return {"found": False, "message": _(message)}
	finally:
		frappe.flags.mute_messages = previous_mute
	return {"found": True, "route": ["Form", doctype, doc.name]}
