import frappe


def can_access_app():
	return bool(
		set(frappe.get_roles()).intersection(
			{"Production Worker", "Production Supervisor", "Production Wage Manager", "System Manager"}
		)
		or frappe.has_permission("Sales Order", "read")
		or frappe.has_permission("Work Order", "read")
		or frappe.has_permission("Material Request", "read")
	)
