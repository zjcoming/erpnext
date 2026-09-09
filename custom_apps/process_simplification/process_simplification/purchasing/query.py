"""Permission-aware cursor pages for purchasing task lists."""

import frappe
from frappe.utils import cint

from process_simplification.notifications import _user_matches_company


def document_page(doctype, filters, project, *, start=0, page_length=20, order_by="creation asc, name asc"):
	frappe.has_permission(doctype, "read", throw=True)
	start = max(cint(start), 0)
	page_length = min(max(cint(page_length), 1), 50)
	rows, offset = [], start
	while True:
		candidates = frappe.get_list(
			doctype, filters=filters, pluck="name", order_by=order_by,
			limit_start=offset, limit_page_length=50,
		)
		for name in candidates:
			doc = frappe.get_doc(doctype, name)
			cursor = offset
			offset += 1
			if not _user_matches_company(frappe.session.user, doc.company):
				continue
			if not frappe.has_permission(doctype, "read", doc=doc):
				continue
			row = project(doc)
			if row is None:
				continue
			if len(rows) == page_length:
				return {"rows": rows, "next_start": cursor, "page_length": page_length}
			rows.append(row)
		if len(candidates) < 50:
			return {"rows": rows, "next_start": None, "page_length": page_length}


def item_search_codes(search):
	if not search:
		return set()
	return set(frappe.get_all("Item", or_filters={"name": ["like", f"%{search}%"], "item_name": ["like", f"%{search}%"]}, pluck="name"))
