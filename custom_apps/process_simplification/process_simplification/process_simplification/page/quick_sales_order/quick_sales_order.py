import frappe


@frappe.whitelist()
def get_context():
	return {"title": "快速开单"}
