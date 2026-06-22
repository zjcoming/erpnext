import frappe


def execute():
	if frappe.db.exists("Workspace", "process-simplification"):
		frappe.db.set_value(
			"Workspace",
			"process-simplification",
			"type",
			"Workspace",
			update_modified=False,
		)
