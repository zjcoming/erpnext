"""Keep Stock Entry lists consistent with native child-row warehouse checks."""

import frappe
from frappe.permissions import get_allowed_docs_for_doctype, get_user_permissions


def validate_batch_bundle_scope(doc, method=None):
	"""Keep native selector writes inside the simplified operator's document scope.

	ERPNext's bundle update RPC saves with ignore_permissions after checking only
	DocType write rights. Check both the saved and proposed document so changing a
	warehouse cannot disguise a bundle outside this operator's existing access.
	Native generation before the parent is inserted still checks company/warehouse.
	"""
	# Native delivery from a Stock Reservation Entry omits company while creating
	# its bundle. A warehouse has exactly one company; fill only that missing
	# value before both native validation and our scoped operator checks run.
	if not doc.get("company") and doc.get("warehouse"):
		doc.company = frappe.db.get_value("Warehouse", doc.warehouse, "company")

	from process_simplification.management_access import (
		OWNER_ROLE, WAREHOUSE_OPERATOR_ROLE, user_company_scope,
	)

	user = frappe.session.user
	roles = set(frappe.get_roles(user))
	if user == "Administrator" or "System Manager" in roles or not roles.intersection({OWNER_ROLE, WAREHOUSE_OPERATOR_ROLE}):
		return
	company_scope = user_company_scope(user)
	previous = doc.get_doc_before_save()
	for candidate in (previous, doc):
		if not candidate:
			continue
		if company_scope is not None and candidate.get("company") not in company_scope:
			frappe.throw("没有该批次明细所属公司的访问权限。", frappe.PermissionError)
		ptype = "create" if candidate is doc and doc.is_new() else "write"
		if not frappe.has_permission("Serial and Batch Bundle", ptype, doc=candidate, user=user):
			frappe.throw("没有该批次明细的公司或仓库操作权限。", frappe.PermissionError)
		voucher_type, voucher_no = candidate.get("voucher_type"), candidate.get("voucher_no")
		if voucher_type and voucher_no and frappe.db.exists(voucher_type, voucher_no):
			if not frappe.has_permission(voucher_type, "write", doc=voucher_no, user=user):
				frappe.throw("没有关联库存单据的操作权限。", frappe.PermissionError)


def ensure_company_warehouse_reference_permissions():
	"""Default warehouse references do not grant access to stock transactions.

	Keep Company User Permissions effective while allowing a warehouse operator to
	select their company even when its default WIP/FG warehouses belong to another
	operator. Native forms also read the shared Stock Settings singleton even when
	its default/sample warehouses differ. Transaction warehouse fields and ledger
	row permissions stay intact; existing role rights still gate settings access.
	"""
	from frappe.custom.doctype.property_setter.property_setter import make_property_setter

	for doctype, fieldnames in {
		"Company": (
			"default_warehouse_for_sales_return", "default_in_transit_warehouse",
			"default_wip_warehouse", "default_fg_warehouse", "default_scrap_warehouse",
			"custom_default_semi_finished_warehouse", "custom_material_quarantine_warehouse",
			"custom_material_rework_warehouse",
		),
		"Stock Settings": ("default_warehouse", "sample_retention_warehouse"),
	}.items():
		for fieldname in fieldnames:
			field = frappe.get_meta(doctype).get_field(fieldname)
			if field and field.options == "Warehouse" and not field.ignore_user_permissions:
				make_property_setter(doctype, fieldname, "ignore_user_permissions", 1, "Check")


def stock_entry_query(user=None):
	user = user or frappe.session.user
	allowed = get_allowed_docs_for_doctype(
		get_user_permissions(user).get("Warehouse", []), "Stock Entry"
	)
	if not allowed:
		return ""

	values = ", ".join(frappe.db.escape(name) for name in sorted(allowed))
	strict = frappe.get_system_settings("apply_strict_user_permissions")
	conditions = []
	meta = frappe.get_meta("Stock Entry Detail")
	for fieldname in ("s_warehouse", "t_warehouse"):
		if meta.get_field(fieldname).ignore_user_permissions:
			continue
		column = f"scope_item.`{fieldname}`"
		outside = f"{column} not in ({values})"
		conditions.append(
			f"({column} is null or {column} = '' or {outside})"
			if strict else f"(ifnull({column}, '') != '' and {outside})"
		)
	if not conditions:
		return ""

	# A transfer or mixed receipt needs permission for every populated warehouse,
	# including entries whose optional header warehouses are empty.
	return (
		"not exists (select 1 from `tabStock Entry Detail` scope_item "
		"where scope_item.parent = `tabStock Entry`.name "
		"and scope_item.parenttype = 'Stock Entry' and scope_item.parentfield = 'items' "
		f"and ({' or '.join(conditions)}))"
	)
