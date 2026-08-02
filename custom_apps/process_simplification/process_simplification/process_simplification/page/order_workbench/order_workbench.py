import frappe


@frappe.whitelist()
def get_context():
	return {"title": "订单履约总览"}
