import frappe

from process_simplification.management_access import APP_MANAGED_ROLES


def can_access_app():
	return bool(
		set(frappe.get_roles()).intersection(
			set(APP_MANAGED_ROLES)
			| {"Production Supervisor", "Production Wage Manager", "System Manager"}
		)
		or frappe.has_permission("Sales Order", "read")
		or frappe.has_permission("Work Order", "read")
		or frappe.has_permission("Material Request", "read")
	)
