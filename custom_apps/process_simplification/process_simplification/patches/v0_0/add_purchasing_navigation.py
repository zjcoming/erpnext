from __future__ import annotations

import frappe

from process_simplification.navigation_layout import arrange_workspace_card_links
from process_simplification.patches.v0_0.group_process_simplification_navigation import _group_sidebar


ITEMS = (
	("采购跟进", "purchase-supplier-allocation", "users"),
	("到货通知", "purchase-receipt-notice", "bell"),
)


def execute():
	for doctype, name, fieldname in (
		("Workspace Sidebar", "Process Simplification", "items"),
		("Workspace", "process-simplification", "links"),
	):
		if not frappe.db.exists(doctype, name):
			continue
		doc = frappe.get_doc(doctype, name)
		rows = doc.get(fieldname)
		for label, route, icon in ITEMS:
			matches = [row for row in rows if row.link_to == route]
			row = matches[0] if matches else doc.append(fieldname, {})
			for duplicate in matches[1:]:
				rows.remove(duplicate)
			row.update(dict(type="Link", label=label, link_type="Page", link_to=route))
			if fieldname == "items":
				row.update(dict(icon=icon, child=1, collapsible=1))
			else:
				row.onboard = 1
		if fieldname == "links":
			arrange_workspace_card_links(rows)
		doc.save(ignore_permissions=True)
	_group_sidebar()
	frappe.clear_cache()
