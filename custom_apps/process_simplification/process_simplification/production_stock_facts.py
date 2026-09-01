from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import flt


MATERIAL_TRANSFER_PURPOSE = "Material Transfer for Manufacture"
DIRECT_CONSUMPTION_PURPOSES = {
	"Manufacture",
	"Material Consumption for Manufacture",
}
SUPPORTED_PURPOSES = {MATERIAL_TRANSFER_PURPOSE, *DIRECT_CONSUMPTION_PURPOSES}


def aggregate_work_order_stock_facts(stock_entry_details, stock_entries=None):
	"""Aggregate submitted stock movement by Work Order, component and source.

	Alternative-item rows are attributed to ``original_item`` because that is the
	component requirement they fulfil.  Material transfers and returns deliberately
	use opposite warehouse sides: a normal transfer leaves ``s_warehouse`` while a
	return restores ``t_warehouse``.  Manufacture and material-consumption entries
	are direct component consumption from ``s_warehouse``.
	"""
	entry_by_name = {
		row.get("name"): frappe._dict(row)
		for row in stock_entries or []
		if row.get("name")
	}
	facts = defaultdict(
		lambda: frappe._dict(
			gross_issued_qty=0.0,
			returned_qty=0.0,
			returned_out_qty=0.0,
			consumed_qty=0.0,
		)
	)
	for source in stock_entry_details or []:
		detail = frappe._dict(source)
		entry = entry_by_name.get(detail.get("parent")) or detail
		if int(detail.get("docstatus", entry.get("docstatus", 1)) or 0) != 1:
			continue
		if int(entry.get("docstatus", 1) or 0) != 1:
			continue
		work_order = entry.get("work_order") or detail.get("work_order")
		purpose = entry.get("purpose") or detail.get("purpose")
		item_code = detail.get("original_item") or detail.get("item_code")
		qty = max(flt(detail.get("transfer_qty")), 0)
		if not work_order or not item_code or qty <= 0:
			continue
		if purpose == MATERIAL_TRANSFER_PURPOSE:
			is_return = bool(int(entry.get("is_return") or detail.get("is_return") or 0))
			if is_return:
				# The original source receives the returned material, while the WIP
				# side releases reservation consumption. Preserve both directions.
				if detail.get("t_warehouse"):
					facts[
						(work_order, item_code, detail.get("t_warehouse"))
					].returned_qty += qty
				if detail.get("s_warehouse"):
					facts[
						(work_order, item_code, detail.get("s_warehouse"))
					].returned_out_qty += qty
			else:
				if detail.get("s_warehouse"):
					facts[
						(work_order, item_code, detail.get("s_warehouse"))
					].gross_issued_qty += qty
		elif purpose in DIRECT_CONSUMPTION_PURPOSES:
			warehouse = detail.get("s_warehouse")
			if not warehouse:
				continue
			facts[(work_order, item_code, warehouse)].consumed_qty += qty

	result = {}
	for key, fact in facts.items():
		fact.net_issued_qty = max(fact.gross_issued_qty - fact.returned_qty, 0)
		fact.consumed_release_qty = fact.consumed_qty + fact.returned_out_qty
		result[key] = fact
	return result


def load_work_order_stock_facts(work_orders):
	"""Load submitted Stock Entry Detail facts for the requested Work Orders."""
	work_orders = sorted(set(filter(None, work_orders or [])))
	if not work_orders:
		return {}
	stock_entries = frappe.get_all(
		"Stock Entry",
		filters={
			"docstatus": 1,
			"work_order": ["in", work_orders],
			"purpose": ["in", sorted(SUPPORTED_PURPOSES)],
		},
		fields=["name", "work_order", "purpose", "is_return", "docstatus"],
		limit=0,
	)
	stock_entry_names = [row.get("name") for row in stock_entries if row.get("name")]
	if not stock_entry_names:
		return {}
	details = frappe.get_all(
		"Stock Entry Detail",
		filters={"parent": ["in", stock_entry_names], "docstatus": 1},
		fields=[
			"parent",
			"docstatus",
			"item_code",
			"original_item",
			"s_warehouse",
			"t_warehouse",
			"transfer_qty",
		],
		limit=0,
	)
	return aggregate_work_order_stock_facts(details, stock_entries)


def get_work_order_stock_fact(stock_facts, work_order, item_code, warehouse):
	return frappe._dict(
		(stock_facts or {}).get((work_order, item_code, warehouse))
		or {
			"gross_issued_qty": 0.0,
			"returned_qty": 0.0,
			"returned_out_qty": 0.0,
			"net_issued_qty": 0.0,
			"consumed_qty": 0.0,
			"consumed_release_qty": 0.0,
		}
	)
