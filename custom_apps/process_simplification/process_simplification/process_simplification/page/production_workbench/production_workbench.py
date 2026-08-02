import frappe


@frappe.whitelist()
def get_context():
	return {"title": "生产工作台"}

