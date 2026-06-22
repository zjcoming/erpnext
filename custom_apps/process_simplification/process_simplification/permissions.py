import frappe


def can_access_app():
	return bool(
		frappe.has_permission("Sales Order", "read")
		or frappe.has_permission("Work Order", "read")
		or frappe.has_permission("Material Request", "read")
	)
