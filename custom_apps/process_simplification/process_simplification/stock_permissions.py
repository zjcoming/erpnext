"""Keep Stock Entry lists consistent with native child-row warehouse checks."""

import frappe
from frappe.permissions import get_allowed_docs_for_doctype, get_user_permissions


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
