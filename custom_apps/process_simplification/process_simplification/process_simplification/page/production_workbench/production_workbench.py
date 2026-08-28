import frappe


@frappe.whitelist()
def get_context():
	return {"title": "生产计划中心"}
