from __future__ import annotations

import frappe


def execute():
	if frappe.db.exists("Page", "process-simplification"):
		frappe.delete_doc("Page", "process-simplification", force=True)

	if frappe.db.exists("Workspace", "流程简化") and not frappe.db.exists(
		"Workspace", "process-simplification"
	):
		frappe.rename_doc(
			"Workspace",
			"流程简化",
			"process-simplification",
			force=True,
		)

	if frappe.db.exists("Workspace", "process-simplification"):
		frappe.db.set_value(
			"Workspace",
			"process-simplification",
			{"label": "流程简化", "title": "流程简化", "type": "Workspace"},
			update_modified=False,
		)

	if frappe.db.exists("Workspace Sidebar", "流程简化") and not frappe.db.exists(
		"Workspace Sidebar", "process-simplification"
	):
		frappe.rename_doc(
			"Workspace Sidebar",
			"流程简化",
			"process-simplification",
			force=True,
		)

	if frappe.db.exists("Workspace Sidebar", "process-simplification"):
		sidebar = frappe.get_doc("Workspace Sidebar", "process-simplification")
		for item in sidebar.items:
			if item.label == "流程简化":
				item.link_type = "Workspace"
				item.link_to = "process-simplification"
		sidebar.save(ignore_permissions=True)
