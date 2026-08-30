import frappe


@frappe.whitelist()
def get_context():
	return {"title": "流程简化权限管理"}
