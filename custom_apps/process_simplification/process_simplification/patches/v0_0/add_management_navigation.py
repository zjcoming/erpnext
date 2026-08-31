from __future__ import annotations

import frappe

from process_simplification.management_access import (
	APP_MANAGED_ROLES,
	LEGACY_APP_MANAGED_ROLES,
	ensure_management_roles,
)


SIDEBAR_NAME = "Process Simplification"
WORKSPACE_NAME = "process-simplification"

ITEMS = (
	{
		"label": "经营总览",
		"link_to": "executive-dashboard",
		"link_type": "Page",
		"icon": "dashboard",
		"before": "quick-sales-order",
	},
	{
		"label": "权限管理",
		"link_to": "process-access-management",
		"link_type": "Page",
		"icon": "users",
		"before": "shortage-purchase-planning",
	},
)


def _move_before(rows, row, before_link_to: str):
	rows.remove(row)
	index = next((i for i, item in enumerate(rows) if item.link_to == before_link_to), len(rows))
	rows.insert(index, row)
	for idx, item in enumerate(rows, start=1):
		item.idx = idx


def _recalculate_link_counts(rows):
	card = None
	count = 0
	for row in rows:
		if row.type == "Card Break":
			if card:
				card.link_count = count
			card = row
			count = 0
		elif card and row.type == "Link":
			count += 1
	if card:
		card.link_count = count


def _repair_sidebar():
	if not frappe.db.exists("Workspace Sidebar", SIDEBAR_NAME):
		return
	sidebar = frappe.get_doc("Workspace Sidebar", SIDEBAR_NAME)
	for values in ITEMS:
		matches = [row for row in sidebar.items if row.link_to == values["link_to"]]
		row = matches[0] if matches else sidebar.append("items", {"type": "Link"})
		for duplicate in matches[1:]:
			sidebar.items.remove(duplicate)
		row.update(
			{
				"type": "Link",
				"label": values["label"],
				"link_type": values["link_type"],
				"link_to": values["link_to"],
				"icon": values["icon"],
				"collapsible": 1,
			}
		)
		_move_before(sidebar.items, row, values["before"])
	sidebar.save(ignore_permissions=True)


def _repair_workspace():
	if not frappe.db.exists("Workspace", WORKSPACE_NAME):
		return
	workspace = frappe.get_doc("Workspace", WORKSPACE_NAME)
	for values in ITEMS:
		matches = [row for row in workspace.links if row.link_to == values["link_to"]]
		row = matches[0] if matches else workspace.append("links", {"type": "Link"})
		for duplicate in matches[1:]:
			workspace.links.remove(duplicate)
		row.update(
			{
				"type": "Link",
				"label": values["label"],
				"link_type": values["link_type"],
				"link_to": values["link_to"],
				"onboard": 1,
			}
		)
		_move_before(workspace.links, row, values["before"])
	_recalculate_link_counts(workspace.links)
	for row in list(workspace.roles):
		if row.role in LEGACY_APP_MANAGED_ROLES:
			workspace.roles.remove(row)
	existing_roles = {row.role for row in workspace.roles}
	for role in APP_MANAGED_ROLES:
		if role not in existing_roles:
			workspace.append("roles", {"role": role})
	workspace.save(ignore_permissions=True)


def execute():
	ensure_management_roles()
	_repair_sidebar()
	_repair_workspace()
	frappe.clear_cache()
