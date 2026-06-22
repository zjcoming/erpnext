import frappe


@frappe.whitelist()
def get_context():
	return {"title": "缺料采购"}
