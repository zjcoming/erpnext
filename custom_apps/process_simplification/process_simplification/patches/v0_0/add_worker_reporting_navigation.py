from __future__ import annotations

import frappe


SIDEBAR_NAME = "Process Simplification"
LEGACY_SIDEBAR_NAMES = ("process-simplification", "流程简化")
APP_NAME = "process_simplification"


ITEMS = (
	{
		"label": "我的报工",
		"link_to": "my-production-reporting",
		"link_type": "Page",
		"icon": "edit",
	},
	{
		"label": "报工审核",
		"link_to": "production-report-review",
		"link_type": "Page",
		"icon": "task-complete",
	},
	{
		"label": "计价规则",
		"link_to": "Operation Wage Rate",
		"link_type": "DocType",
		"icon": "money-coins-1",
	},
	{
		"label": "工资汇总",
		"link_to": "Monthly Worker Wage Summary",
		"link_type": "DocType",
		"icon": "accounting",
	},
)

LEGACY_PAGE_ROUTES = ("my-production-tasks", "production-wage-management")


def _move_before(rows, row, before_link_to: str):
	rows.remove(row)
	index = next((i for i, item in enumerate(rows) if item.link_to == before_link_to), len(rows))
	rows.insert(index, row)
	for idx, item in enumerate(rows, start=1):
		item.idx = idx


def _remove_legacy_rows(rows):
	for row in list(rows):
		if row.link_to in LEGACY_PAGE_ROUTES:
			rows.remove(row)


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


def _remove_legacy_pages():
	legacy_names = frappe.db.sql(
		"""
		select name from `tabPage`
		where name in %(routes)s or page_name in %(routes)s
		""",
		{"routes": LEGACY_PAGE_ROUTES},
		pluck=True,
	)
	for page_name in legacy_names:
		# The old page source no longer exists. Direct database cleanup avoids the
		# Page controller trying to remove code folders during an upgrade.
		frappe.db.delete(
			"Has Role",
			{"parenttype": "Page", "parent": page_name},
		)
		frappe.db.delete("Page", {"name": page_name})


def _ensure_native_sidebar_identity():
	if not frappe.db.exists("Workspace Sidebar", SIDEBAR_NAME):
		legacy_name = next(
			(
				name
				for name in LEGACY_SIDEBAR_NAMES
				if frappe.db.exists("Workspace Sidebar", name)
			),
			None,
		)
		if legacy_name:
			frappe.rename_doc(
				"Workspace Sidebar",
				legacy_name,
				SIDEBAR_NAME,
				force=True,
			)

	for legacy_name in LEGACY_SIDEBAR_NAMES:
		if not frappe.db.exists("Workspace Sidebar", legacy_name):
			continue
		# The old and native titles scrub to the same exported filename. A normal
		# delete would remove the new app-owned JSON through Workspace Sidebar's
		# on_trash hook, so remove only the obsolete database rows here.
		frappe.db.delete(
			"Workspace Sidebar Item",
			{"parenttype": "Workspace Sidebar", "parent": legacy_name},
		)
		frappe.db.delete("Workspace Sidebar", {"name": legacy_name})

	if not frappe.db.exists("Workspace Sidebar", SIDEBAR_NAME):
		return None

	sidebar = frappe.get_doc("Workspace Sidebar", SIDEBAR_NAME)
	sidebar.update(
		{
			"title": SIDEBAR_NAME,
			"module": SIDEBAR_NAME,
			"app": "process_simplification",
			"standard": 1,
			"header_icon": "workflow",
		}
	)
	return sidebar


def _repair_sidebar():
	sidebar = _ensure_native_sidebar_identity()
	if not sidebar:
		return
	_remove_legacy_rows(sidebar.items)
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
		_move_before(sidebar.items, row, "shortage-purchase-planning")
	sidebar.save(ignore_permissions=True)


def _repair_workspace():
	if not frappe.db.exists("Workspace", "process-simplification"):
		return
	workspace = frappe.get_doc("Workspace", "process-simplification")
	_remove_legacy_rows(workspace.links)
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
		_move_before(workspace.links, row, "shortage-purchase-planning")
	_recalculate_link_counts(workspace.links)
	existing_roles = {row.role for row in workspace.roles}
	for role in ("Production Worker", "Production Supervisor", "Production Wage Manager"):
		if role not in existing_roles:
			workspace.append("roles", {"role": role})
	workspace.save(ignore_permissions=True)


def _repair_desktop_icon():
	app_icon_name = frappe.db.get_value(
		"Desktop Icon",
		{"app": APP_NAME, "icon_type": "App"},
		"name",
	)
	if not app_icon_name:
		return

	if app_icon_name != SIDEBAR_NAME and not frappe.db.exists("Desktop Icon", SIDEBAR_NAME):
		frappe.rename_doc(
			"Desktop Icon",
			app_icon_name,
			SIDEBAR_NAME,
			force=True,
		)
		app_icon_name = SIDEBAR_NAME

	if app_icon_name != SIDEBAR_NAME:
		frappe.db.set_value(
			"Desktop Icon",
			app_icon_name,
			"hidden",
			1,
			update_modified=False,
		)

	icon = frappe.get_doc("Desktop Icon", SIDEBAR_NAME)
	icon.update(
		{
			"label": SIDEBAR_NAME,
			"icon_type": "App",
			"link_type": "Workspace Sidebar",
			"link_to": SIDEBAR_NAME,
			"sidebar": SIDEBAR_NAME,
			"link": None,
			"hidden": 0,
			"app": APP_NAME,
		}
	)
	icon.save(ignore_permissions=True)

	# The desktop icon and boot payload are cached per user. Clear the complete
	# hashes so existing worker sessions do not retain the obsolete external URL.
	frappe.cache.delete_key("desktop_icons")
	frappe.cache.delete_key("bootinfo")


def execute():
	_remove_legacy_pages()
	_repair_sidebar()
	_repair_workspace()
	_repair_desktop_icon()
	frappe.clear_cache()
