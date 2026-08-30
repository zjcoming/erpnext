import frappe


@frappe.whitelist()
def get_context():
	return {"title": "经营总览"}
