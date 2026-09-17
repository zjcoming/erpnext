"""Scope the native batch selector's reads without replacing its stock calculations.

These guards apply only to the simplified owner/warehouse roles. Native users and
server-side ERPNext calls retain their existing behavior. Batch names are global
item master data; quantities returned by these selectors are warehouse-specific.
"""

from __future__ import annotations

from collections.abc import Mapping

import frappe
from frappe.utils import flt

from process_simplification.management_access import (
	OWNER_ROLE,
	WAREHOUSE_OPERATOR_ROLE,
	user_company_scope,
)

NATIVE_BATCH_QUERY = "erpnext.controllers.queries.get_batch_no"
SCOPED_BATCH_QUERY = "process_simplification.batch_permissions.get_batch_no"
NATIVE_BATCH = "erpnext.stock.doctype.batch.batch"
NATIVE_BUNDLE = "erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle"
RETURN_SOURCE_FIELDS = {
	"Sales Invoice Item": ("Sales Invoice", "sales_invoice_item"),
	"Delivery Note Item": ("Delivery Note", "dn_detail"),
	"Purchase Receipt Item": ("Purchase Receipt", "purchase_receipt_item"),
	"Purchase Invoice Item": ("Purchase Invoice", "purchase_invoice_item"),
	"POS Invoice Item": ("POS Invoice", "pos_invoice_item"),
}


def _native_call(method, *args, **kwargs):
	# Direct Python calls intentionally do not traverse RPC overrides.
	return frappe.get_attr(method)(*args, **kwargs)


def _scope(user=None):
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	if user == "Administrator" or "System Manager" in roles:
		return None
	if not roles.intersection({OWNER_ROLE, WAREHOUSE_OPERATOR_ROLE}):
		return None
	return frappe._dict(user=user, companies=user_company_scope(user))


def _deny(message="没有该批次所属公司、仓库或原单的查看权限。"):
	frappe.throw(message, frappe.PermissionError)


def _check_company(company, scope):
	if scope.companies is not None and company not in scope.companies:
		_deny()


def _check_warehouses(warehouse, scope):
	warehouses = warehouse if isinstance(warehouse, (list, tuple)) else [warehouse]
	if not warehouses or any(not isinstance(value, str) or not value.strip() for value in warehouses):
		_deny("请先选择有权限的仓库，再查看批次库存。")
	for name in set(warehouses):
		doc = frappe.get_doc("Warehouse", name)
		_check_company(doc.company, scope)
		if not frappe.has_permission("Warehouse", "read", doc=doc, user=scope.user):
			_deny()
	return set(warehouses)


def _check_document(doctype, name, scope):
	doc = frappe.get_doc(doctype, name)
	if doc.get("company"):
		_check_company(doc.company, scope)
	if not frappe.has_permission(doctype, "read", doc=doc, user=scope.user):
		_deny()
	return doc


def _check_bundle_scope(doc, scope, item_code=None):
	# Do not ask has_permission on this Bundle here: this function is also used
	# by its controller permission hook, after Frappe's ordinary checks.
	_check_company(doc.company, scope)
	_check_warehouses([doc.warehouse] + [row.get("warehouse") or doc.warehouse for row in doc.get("entries") or []], scope)
	if item_code and doc.item_code != item_code:
		_deny()
	if doc.get("voucher_no"):
		if not doc.get("voucher_type") or doc.voucher_type == "Serial and Batch Bundle":
			_deny()
		_check_document(doc.voucher_type, doc.voucher_no, scope)
	elif doc.get("owner") != scope.user or doc.get("docstatus") != 0:
		# A selector can create an unlinked draft before its parent has been saved.
		# Its creator can reopen it; other operators need a readable original voucher.
		_deny()
	return doc


def _check_bundle(name, scope, item_code=None):
	doc = _check_document("Serial and Batch Bundle", name, scope)
	return _check_bundle_scope(doc, scope, item_code)


def bundle_permission(doc, ptype=None, user=None, debug=False):
	"""Deny unsafe direct reads; True lets native role/User Permissions continue."""
	if ptype not in (None, "read", "select", "print", "email", "export", "report"):
		return True
	scope = _scope(user)
	if not scope:
		return True
	try:
		_check_bundle_scope(doc, scope)
	except (frappe.PermissionError, frappe.DoesNotExistError):
		return False
	return True


def _readable_names_query(doctype, user, filters=None):
	"""Compile a native permission-aware subquery, without loading voucher names.

	This keeps existing Customer/Supplier/Company/Warehouse permissions and each
	parent's permission_query_conditions, including Stock Entry child warehouses.
	"""
	kwargs = dict(fields=["name"], user=user, limit=0, order_by="", run=False)
	if filters:
		kwargs["filters"] = filters
	return frappe.get_list(doctype, **kwargs).get_sql()


def bundle_query(user=None):
	"""Apply the same boundary to generic list/get_value/child-field queries."""
	scope = _scope(user)
	if not scope:
		return ""
	if not scope.companies:
		return "1=0"
	table = "`tabSerial and Batch Bundle`"
	companies = ", ".join(frappe.db.escape(value) for value in sorted(scope.companies))
	company_filter = {"company": ["in", sorted(scope.companies)]}
	try:
		warehouses = _readable_names_query("Warehouse", scope.user, company_filter)
	except frappe.PermissionError:
		return "1=0"
	conditions = [f"{table}.company in ({companies})", f"{table}.warehouse in ({warehouses})"]
	# Legacy or inconsistent children must not bypass the header warehouse.
	conditions.append("not exists (select 1 from `tabSerial and Batch Entry` scope_batch "
		f"where scope_batch.parent = {table}.name and scope_batch.parenttype = 'Serial and Batch Bundle' "
		f"and ifnull(scope_batch.warehouse, '') != '' and scope_batch.warehouse not in ({warehouses}))")
	allowed = [f"({table}.owner = {frappe.db.escape(scope.user)} and {table}.docstatus = 0 "
		f"and ifnull({table}.voucher_no, '') = '')"]
	# Only the small set of voucher *types* in this company's bundles is read.
	# The database then intersects names using native permission subqueries.
	voucher_types = frappe.get_all("Serial and Batch Bundle",
		filters={"company": ["in", sorted(scope.companies)]},
		fields=["voucher_type"], distinct=True, order_by="voucher_type", limit=0)
	for row in voucher_types:
		doctype = row.voucher_type
		if not doctype or doctype == "Serial and Batch Bundle":
			continue
		if not frappe.has_permission(doctype, "read", user=scope.user):
			continue
		try:
			filters = company_filter if frappe.get_meta(doctype).has_field("company") else None
			query = _readable_names_query(doctype, scope.user, filters)
		except frappe.PermissionError:
			continue
		allowed.append(f"({table}.voucher_type = {frappe.db.escape(doctype)} and {table}.voucher_no in ({query}))")
	return " and ".join(conditions + ["(" + " or ".join(allowed) + ")"])


def _mapping(value):
	if value is None or value == "":
		return frappe._dict()
	if isinstance(value, str):
		value = frappe.parse_json(value)
	if value is None:
		return frappe._dict()
	if not isinstance(value, Mapping):
		_deny("批次查询条件无效。")
	return frappe._dict(value)


def _check_reserved_source(detail, item_code, scope):
	# The native scio_detail branch ignores its warehouse argument and reads SREs
	# directly. Check the real source voucher and every actual SRE warehouse first.
	rows = frappe.get_all("Stock Reservation Entry", filters={"voucher_detail_no": detail, "docstatus": 1},
		fields=["name", "voucher_type", "voucher_no", "item_code", "warehouse", "company"])
	for row in rows:
		_check_company(row.company, scope)
		_check_warehouses(row.warehouse, scope)
		if item_code and row.item_code != item_code:
			_deny()
		if not row.voucher_type or not row.voucher_no:
			_deny()
		_check_document(row.voucher_type, row.voucher_no, scope)
	return rows


@frappe.whitelist()
def get_batch_qty(batch_no=None, warehouse=None, item_code=None, creation=None,
	posting_datetime=None, posting_date=None, posting_time=None, ignore_voucher_nos=None,
	for_stock_levels=False, consider_negative_batches=False, do_not_check_future_batches=False,
	ignore_reserved_stock=False):
	kwargs = dict(batch_no=batch_no, warehouse=warehouse, item_code=item_code, creation=creation,
		posting_datetime=posting_datetime, posting_date=posting_date, posting_time=posting_time,
		ignore_voucher_nos=ignore_voucher_nos, for_stock_levels=for_stock_levels,
		consider_negative_batches=consider_negative_batches,
		do_not_check_future_batches=do_not_check_future_batches, ignore_reserved_stock=ignore_reserved_stock)
	if scope := _scope():
		_check_warehouses(warehouse, scope)
	return _native_call(f"{NATIVE_BATCH}.get_batch_qty", **kwargs)


@frappe.whitelist()
def get_auto_data(**kwargs):
	scope = _scope()
	if scope:
		_check_warehouses(kwargs.get("warehouse"), scope)
		if kwargs.get("company"):
			_check_company(kwargs["company"], scope)
		if kwargs.get("against_sales_order"):
			_check_document("Sales Order", kwargs["against_sales_order"], scope)
		if kwargs.get("scio_detail"):
			_check_reserved_source(kwargs["scio_detail"], kwargs.get("item_code"), scope)
	rows = _native_call(f"{NATIVE_BUNDLE}.get_auto_data", **kwargs)
	if scope:
		for row in rows or []:
			_check_warehouses(row.get("warehouse"), scope)
	return rows


def _return_source_bundle(child_row, item_code, scope):
	info = RETURN_SOURCE_FIELDS.get(child_row.get("doctype"))
	if not info or not child_row.get(info[1]):
		_deny("请从有权限的原收发单据建立退货单，再选择原批次。")
	row = frappe.get_doc(child_row.doctype, child_row.get(info[1]))
	if row.parenttype != info[0] or (item_code and row.item_code != item_code):
		_deny()
	_check_document(row.parenttype, row.parent, scope)
	return row.get("serial_and_batch_bundle")


@frappe.whitelist()
def get_serial_batch_ledgers(item_code=None, docstatus=None, voucher_no=None, name=None, child_row=None):
	scope = _scope()
	if not scope:
		return _native_call(f"{NATIVE_BUNDLE}.get_serial_batch_ledgers", item_code=item_code,
			docstatus=docstatus, voucher_no=voucher_no, name=name, child_row=child_row)
	child = _mapping(child_row)
	if not name and child and flt(child.get("qty")) < 0:
		name = _return_source_bundle(child, item_code, scope)
		if not name:
			# A legacy return without a bundle must not become an unfiltered query.
			return []
		voucher_no = None
	if isinstance(name, (list, tuple)):
		names = list(name)
	elif isinstance(name, str) and name:
		names = [name]
	elif voucher_no:
		filters = {"voucher_no": voucher_no, "is_cancelled": 0}
		if item_code:
			filters["item_code"] = item_code
		names = frappe.get_all("Serial and Batch Bundle", filters=filters, pluck="name")
	else:
		_deny("请先打开有权限的原单，再查看批次明细。")
	if not names:
		return []
	for bundle in names:
		if not isinstance(bundle, str) or not bundle:
			_deny()
		_check_bundle(bundle, scope, item_code)
	return _native_call(f"{NATIVE_BUNDLE}.get_serial_batch_ledgers", item_code=item_code,
		docstatus=docstatus, voucher_no=voucher_no, name=names, child_row=child_row)


@frappe.whitelist()
def get_batch_no(doctype, txt, searchfield, start, page_len, filters):
	scope = _scope()
	if not scope:
		return _native_call(NATIVE_BATCH_QUERY, doctype, txt, searchfield, start, page_len, filters)
	filters = _mapping(filters)
	_check_warehouses(filters.get("warehouse"), scope)
	if not isinstance(filters.warehouse, str):
		_deny("请先选择一个仓库，再选择批次。")
	is_inward = filters.get("is_inward")
	outward_filters = dict(filters, is_inward=False)
	rows = _native_call(NATIVE_BATCH_QUERY, doctype, txt, searchfield, start, page_len, outward_filters)
	if is_inward:
		# Native inward candidates also include global Batch masters. Their stored
		# batch_qty is a total across warehouses, not a quantity this user may read.
		# Existing stock rows keep the native warehouse quantity; other batches have
		# zero in this warehouse and remain selectable for an incoming receipt.
		extra = _native_call("erpnext.controllers.queries.get_empty_batches", filters, start, 300, rows, txt)
		rows = list(rows) + [(row[0], 0) for row in extra]
	return rows


def _search_query(query):
	return SCOPED_BATCH_QUERY if query == NATIVE_BATCH_QUERY else query


@frappe.whitelist()
def search_link(doctype: str, txt: str, query: str | None = None,
	filters: str | dict | list | None = None, page_length: int = 10,
	searchfield: str | None = None, reference_doctype: str | None = None,
	ignore_user_permissions: bool = False, *, link_fieldname: str | None = None):
	return _native_call("frappe.desk.search.search_link", doctype=doctype, txt=txt,
		query=_search_query(query), filters=filters, page_length=page_length,
		searchfield=searchfield, reference_doctype=reference_doctype,
		ignore_user_permissions=ignore_user_permissions, link_fieldname=link_fieldname)


@frappe.whitelist()
def search_widget(doctype: str, txt: str, query: str | None = None,
	searchfield: str | None = None, start: int = 0, page_length: int = 10,
	filters: str | None | dict | list = None, filter_fields: str | None = None,
	as_dict: bool = False, reference_doctype: str | None = None,
	ignore_user_permissions: bool = False, *, link_fieldname: str | None = None,
	for_link_validation: bool = False, query_filters_as_dict: bool = False):
	return _native_call("frappe.desk.search.search_widget", doctype=doctype, txt=txt,
		query=_search_query(query), searchfield=searchfield, start=start,
		page_length=page_length, filters=filters, filter_fields=filter_fields,
		as_dict=as_dict, reference_doctype=reference_doctype,
		ignore_user_permissions=ignore_user_permissions, link_fieldname=link_fieldname,
		for_link_validation=for_link_validation, query_filters_as_dict=query_filters_as_dict)
