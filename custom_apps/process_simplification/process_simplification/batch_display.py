"""Read batch facts for authorized native documents, without choosing stock."""

from collections import defaultdict

import frappe
from frappe.permissions import get_user_permissions
from frappe.utils import cint, flt

from process_simplification.management_access import user_company_scope


STOCK_DOCUMENTS = {"Purchase Receipt", "Stock Entry", "Delivery Note"}
BATCH_REPORTS = (
	("批次库存", "Available Batch Report"),
	("批次收发历史", "Batch-Wise Balance History"),
	("批次追溯", "Serial No and Batch Traceability"),
)


def _row_key(row):
	return row.get("name") or str(row.get("idx") or "")


def document_batch_summary(doc, *, check_permission=True, include_item_flags=True, batch_items=None):
	"""Return saved stock-UOM batch quantities, scoped by the readable parent.

	A linked bundle wins over legacy fields, even when unreadable or invalid: an
	old batch_no must never disguise a different current bundle. Bundle permissions
	and its parent/item/warehouse association are checked before reading entries.
	"""
	if not doc or doc.get("doctype") not in STOCK_DOCUMENTS:
		return {}
	permission_doc = doc.get("name") if isinstance(doc, dict) else doc
	if check_permission and not frappe.has_permission(doc.doctype, "read", doc=permission_doc):
		frappe.throw("没有该单据的访问权限。", frappe.PermissionError)
	rows = doc.get("items") or []
	item_codes = sorted({row.get("item_code") for row in rows if row.get("item_code")})
	if batch_items is None:
		batch_items = set(frappe.get_all(
			"Item", filters={"name": ["in", item_codes], "has_batch_no": 1}, pluck="name",
		)) if item_codes and include_item_flags else set()
	bundle_names = sorted({
		row.get(field) for row in rows
		for field in ("serial_and_batch_bundle", "rejected_serial_and_batch_bundle") if row.get(field)
	})
	bundles = {}
	if bundle_names and frappe.has_permission("Serial and Batch Bundle", "read"):
		bundles = {row.name: row for row in frappe.get_list(
			"Serial and Batch Bundle", filters={"name": ["in", bundle_names]},
			fields=["name", "item_code", "company", "warehouse", "voucher_type", "voucher_no", "voucher_detail_no"],
			limit_page_length=0,
		)}
	valid_links = {}
	for row in rows:
		for rejected in (False, True):
			field = "rejected_serial_and_batch_bundle" if rejected else "serial_and_batch_bundle"
			bundle = bundles.get(row.get(field))
			warehouse = row.get("rejected_warehouse") if rejected else (
				row.get("s_warehouse") or row.get("t_warehouse") if doc.doctype == "Stock Entry" else row.get("warehouse")
			)
			if bundle and (
				bundle.item_code == row.item_code and bundle.company == doc.get("company")
				and bundle.warehouse == warehouse
				and bundle.voucher_type in (None, "", doc.doctype)
				and bundle.voucher_no in (None, "", doc.get("name"))
				and bundle.voucher_detail_no in (None, "", row.get("name"))
			):
				valid_links[(_row_key(row), rejected)] = bundle.name
	entries = defaultdict(list)
	if valid_links:
		for entry in frappe.get_all(
			"Serial and Batch Entry", filters={"parent": ["in", sorted(set(valid_links.values()))]},
			fields=["parent", "batch_no", "qty"], order_by="idx asc",
		):
			if entry.batch_no:
				entries[entry.parent].append(entry)
	result = {}
	for row in rows:
		groups = []
		for rejected in (False, True):
			field = "rejected_serial_and_batch_bundle" if rejected else "serial_and_batch_bundle"
			lots = defaultdict(float)
			if row.get(field):
				for entry in entries.get(valid_links.get((_row_key(row), rejected)), []):
					lots[entry.batch_no] += abs(flt(entry.qty))
			elif row.get("batch_no"):
				quantity = (
					flt(row.get("rejected_qty")) * (flt(row.get("conversion_factor")) or 1)
					if rejected else flt(row.get("transfer_qty") if doc.doctype == "Stock Entry" else row.get("stock_qty"))
				)
				if quantity:
					lots[row.batch_no] = abs(quantity)
			groups.append([
				{"batch_no": batch, "qty": quantity, "stock_uom": row.get("stock_uom") or row.get("uom")}
				for batch, quantity in lots.items() if quantity
			])
		result[_row_key(row)] = {
			"has_batch_no": row.item_code in batch_items or bool(groups[0] or groups[1] or row.get("batch_no")),
			"batches": groups[0], "rejected_batches": groups[1],
		}
	return result


def get_print_batch_summary(doc):
	return document_batch_summary(doc, include_item_flags=False)


def batch_navigation():
	"""Offer existing native reports only to unrestricted native managers.

	Report queries do not consistently enforce Company/Warehouse User Permissions.
	This adds no report role; scoped operators use their authorized source forms.
	"""
	user = frappe.session.user
	permissions = get_user_permissions(user)
	unrestricted = user == "Administrator" or (
		bool(set(frappe.get_roles(user)) & {"System Manager", "Stock Manager"})
		and not any(permissions.values())
		and not user_company_scope(user)
	)
	can_configure = bool(frappe.has_permission("Stock Settings", "write"))
	result = {"can_configure": can_configure, "reports": []}
	if not unrestricted:
		return result
	# Historical records remain reachable even if the item activation UI is off.
	has_batches = bool(cint(frappe.db.get_single_value("Stock Settings", "enable_serial_and_batch_no_for_item"))
		or frappe.db.exists("Batch"))
	if has_batches:
		for label, name in BATCH_REPORTS:
			if not frappe.db.exists("Report", name):
				continue
			report = frappe.get_doc("Report", name)
			if not report.disabled and report.is_permitted() and frappe.has_permission(report.ref_doctype, "report"):
				result["reports"].append({"label": label, "name": name})
	return result
