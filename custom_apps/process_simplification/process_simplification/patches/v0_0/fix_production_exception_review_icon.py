from __future__ import annotations

import frappe


SIDEBAR_NAME = "Process Simplification"
PAGE_NAME = "production-exception-review"
ICON = "triangle-alert"


def execute():
	if frappe.db.exists("Page", PAGE_NAME):
		frappe.db.set_value("Page", PAGE_NAME, "icon", ICON, update_modified=False)

	frappe.db.sql(
		"""
		update `tabWorkspace Sidebar Item`
		set icon = %(icon)s
		where parent = %(parent)s
		  and link_to = %(page)s
		""",
		{"icon": ICON, "parent": SIDEBAR_NAME, "page": PAGE_NAME},
	)

	frappe.clear_cache()
