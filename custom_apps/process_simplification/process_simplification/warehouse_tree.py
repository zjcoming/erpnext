"""Keep permitted warehouses reachable without granting access to their parents."""

import frappe
from frappe import _
from frappe.core.doctype.user_permission.user_permission import get_user_permissions

from erpnext.stock.doctype.warehouse.warehouse import get_children as native_get_children


@frappe.whitelist()
def get_children(doctype, parent=None, company=None, is_root=False, include_disabled=False):
	if doctype != "Warehouse":
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	is_root = frappe.sbool(is_root)
	include_disabled = frappe.sbool(include_disabled)
	warehouse_permissions = get_user_permissions().get("Warehouse", [])
	if not any(not p.applicable_for or p.applicable_for == "Warehouse" for p in warehouse_permissions):
		return native_get_children(doctype, parent, company, is_root, include_disabled)

	filters = [["company", "in", (company, None, "")]]
	if not include_disabled:
		filters.append(["disabled", "=", 0])
	fields = ["name", "parent_warehouse", "is_group", "company"]
	# This is the authority for actual warehouse visibility: retain native role,
	# company, user-permission and sharing checks, including an empty result.
	permitted = frappe.get_list("Warehouse", fields=fields, filters=filters, limit=0)
	nodes = {row.name: row for row in permitted}
	readable = set(nodes)
	frontier = permitted
	visited = set(nodes)
	while frontier:
		parents = {row.parent_warehouse for row in frontier if row.parent_warehouse} - visited
		if not parents:
			break
		visited.update(parents)
		# Only fetch group names on a path from a permitted warehouse. Never
		# fetch their other children or create User Permissions for these groups:
		# native group permissions would also authorize sibling warehouses.
		frontier = frappe.get_all(
			"Warehouse",
			fields=fields,
			filters=[*filters, ["name", "in", sorted(parents)], ["is_group", "=", 1]],
			limit=0,
		)
		nodes.update({row.name: row for row in frontier})

	parent = "" if is_root else (parent or "")
	return [
		frappe._dict(
			value=row.name,
			expandable=row.is_group,
			**({"ps_navigation_only": True} if row.name not in readable else {}),
		)
		for row in sorted(nodes.values(), key=lambda row: row.name)
		if (row.parent_warehouse or "") == parent
	]
