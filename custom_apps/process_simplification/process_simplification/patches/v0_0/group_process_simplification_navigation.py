from __future__ import annotations

import frappe


SIDEBAR_NAME = "Process Simplification"
HOME_LINK = "process-simplification"

GROUPS = (
	("总览与报表", ("executive-dashboard",)),
	("销售与订单", ("quick-sales-order", "order-workbench")),
	(
		"生产",
		(
			"production-workbench",
			"active-production-work",
			"my-production-reporting",
			"production-report-history",
			"production-report-review",
			"production-exception-review",
		),
	),
	("采购", ("shortage-purchase-planning",)),
	("工资管理", ("Operation Wage Rate", "Monthly Worker Wage Summary")),
	("系统管理", ("Process Simplification Settings", "process-access-management")),
)

GROUP_LABELS = {label for label, _links in GROUPS}
GROUPED_LINKS = tuple(link for _label, links in GROUPS for link in links)
MANAGED_LINKS = (HOME_LINK, *GROUPED_LINKS)


def _section_break(sidebar, label: str):
	return sidebar.append(
		"items",
		{
			"type": "Section Break",
			"label": label,
			"link_type": "DocType",
			"child": 0,
			"collapsible": 1,
			"indent": 0,
			"keep_closed": 0,
			"show_arrow": 0,
		},
	)


def _group_sidebar():
	if not frappe.db.exists("Workspace Sidebar", SIDEBAR_NAME):
		return

	sidebar = frappe.get_doc("Workspace Sidebar", SIDEBAR_NAME)
	managed_rows = {}
	duplicate_rows = set()

	for row in sidebar.items:
		if row.link_to not in MANAGED_LINKS:
			continue
		if row.link_to in managed_rows:
			duplicate_rows.add(row.name)
			continue
		managed_rows[row.link_to] = row

	managed_row_names = {row.name for row in managed_rows.values()}
	remaining_rows = [
		row
		for row in sidebar.items
		if row.name not in managed_row_names
		and row.name not in duplicate_rows
		and not (row.type == "Section Break" and row.label in GROUP_LABELS)
	]

	ordered_rows = []
	home = managed_rows.get(HOME_LINK)
	if home:
		home.child = 0
		ordered_rows.append(home)

	for label, links in GROUPS:
		group_rows = [managed_rows[link] for link in links if link in managed_rows]
		if not group_rows:
			continue
		section = _section_break(sidebar, label)
		sidebar.items.remove(section)
		ordered_rows.append(section)
		for row in group_rows:
			row.child = 1
			ordered_rows.append(row)

	sidebar.items = [*ordered_rows, *remaining_rows]
	for index, row in enumerate(sidebar.items, start=1):
		row.idx = index

	sidebar.save(ignore_permissions=True)


def execute():
	_group_sidebar()
	frappe.clear_cache()
