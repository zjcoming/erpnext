from __future__ import annotations

import frappe


PRODUCTION_CENTER_LABEL = "生产计划中心"
PRODUCTION_CENTER_ICON = "factory"
SIDEBAR_NAME = "Process Simplification"


def _move_before(rows, row, before_link_to: str):
	rows.remove(row)
	before_index = next(
		(index for index, item in enumerate(rows) if item.link_to == before_link_to),
		len(rows),
	)
	rows.insert(before_index, row)
	for index, item in enumerate(rows, start=1):
		item.idx = index


def _repair_sidebar():
	if not frappe.db.exists("Workspace Sidebar", SIDEBAR_NAME):
		return

	sidebar = frappe.get_doc("Workspace Sidebar", SIDEBAR_NAME)
	for item in sidebar.items:
		if item.link_to == "order-workbench":
			item.label = "订单工作台"

	production = next(
		(item for item in sidebar.items if item.link_to == "production-workbench"),
		None,
	)
	if production is None:
		production = sidebar.append(
			"items",
			{
				"type": "Link",
				"label": PRODUCTION_CENTER_LABEL,
				"link_type": "Page",
				"link_to": "production-workbench",
				"icon": PRODUCTION_CENTER_ICON,
				"collapsible": 1,
			},
		)
	else:
		production.update(
			{
				"label": PRODUCTION_CENTER_LABEL,
				"link_type": "Page",
				"icon": PRODUCTION_CENTER_ICON,
			}
		)
	_move_before(sidebar.items, production, "shortage-purchase-planning")
	sidebar.save(ignore_permissions=True)


def _repair_workspace():
	if not frappe.db.exists("Workspace", "process-simplification"):
		return

	workspace = frappe.get_doc("Workspace", "process-simplification")
	for item in workspace.links:
		if item.link_to == "order-workbench":
			item.label = "订单工作台"
		if item.type == "Card Break" and item.label == "核心流程":
			item.link_count = 4

	production = next(
		(item for item in workspace.links if item.link_to == "production-workbench"),
		None,
	)
	if production is None:
		production = workspace.append(
			"links",
			{
				"type": "Link",
				"label": PRODUCTION_CENTER_LABEL,
				"link_type": "Page",
				"link_to": "production-workbench",
				"onboard": 1,
			},
		)
	else:
		production.update(
			{
				"label": PRODUCTION_CENTER_LABEL,
				"link_type": "Page",
				"onboard": 1,
			}
		)
	_move_before(workspace.links, production, "shortage-purchase-planning")
	workspace.save(ignore_permissions=True)


def _repair_page_titles():
	for page_name, title in {
		"order-workbench": "订单工作台",
		"production-workbench": PRODUCTION_CENTER_LABEL,
	}.items():
		if frappe.db.exists("Page", page_name):
			frappe.db.set_value("Page", page_name, "title", title, update_modified=False)


def execute():
	_repair_page_titles()
	_repair_sidebar()
	_repair_workspace()
	frappe.clear_cache()
