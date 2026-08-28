from __future__ import annotations

import frappe


SIDEBAR_NAME = "Process Simplification"


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
			{
				"label": "process-simplification",
				"title": "process-simplification",
				"type": "Workspace",
			},
			update_modified=False,
		)

	if frappe.db.exists("Workspace Sidebar", "流程简化") and not frappe.db.exists(
		"Workspace Sidebar", SIDEBAR_NAME
	):
		frappe.rename_doc(
			"Workspace Sidebar",
			"流程简化",
			SIDEBAR_NAME,
			force=True,
		)

	if frappe.db.exists("Workspace Sidebar", "process-simplification") and not frappe.db.exists(
		"Workspace Sidebar", SIDEBAR_NAME
	):
		frappe.rename_doc(
			"Workspace Sidebar",
			"process-simplification",
			SIDEBAR_NAME,
			force=True,
		)

	if frappe.db.exists("Workspace Sidebar", SIDEBAR_NAME):
		sidebar = frappe.get_doc("Workspace Sidebar", SIDEBAR_NAME)
		sidebar.title = SIDEBAR_NAME
		sidebar.module = SIDEBAR_NAME
		for item in sidebar.items:
			if item.label == "流程简化":
				item.link_type = "Workspace"
				item.link_to = "process-simplification"
		sidebar.save(ignore_permissions=True)

	app_icon = frappe.db.get_value(
		"Desktop Icon",
		{"app": "process_simplification", "icon_type": "App"},
		"name",
	)
	if app_icon:
		frappe.db.set_value(
			"Desktop Icon",
			app_icon,
			"link",
			"/desk/process-simplification",
			update_modified=False,
		)

	if frappe.db.exists("Desktop Icon", "process-simplification"):
		frappe.db.set_value(
			"Desktop Icon",
			"process-simplification",
			{"parent_icon": None, "hidden": 1},
			update_modified=False,
		)

	frappe.clear_cache()
