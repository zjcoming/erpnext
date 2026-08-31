import frappe

from process_simplification.management_access import PAGE_CAPABILITIES, user_has_capability


def can_access_app():
	return bool(
		any(user_has_capability(capability) for capability in set(PAGE_CAPABILITIES.values()))
		or frappe.has_permission("Sales Order", "read")
		or frappe.has_permission("Work Order", "read")
		or frappe.has_permission("Material Request", "read")
	)
