"""Drive ERPNext's Production Plan sub-assembly engine from the simplified flow.

The simplified production workbench needs a single "create Work Orders" action
that also creates Work Orders for every in-house sub-assembly of a multi-level
BOM. Rather than re-implement the recursion, we build a saved Production Plan
for one finished-good demand and reuse the standard engine:

  po_items (one finished good, delivery-priority net qty, Sales Order link)
    -> get_sub_assembly_items()  (recursively explode the multi-level BOM,
       skipping levels already covered by sub-assembly stock)
    -> make_work_order()         (create the finished-good WO and one WO per
       remaining in-house sub-assembly level)

The Production Plan is persisted so the created Work Orders keep a real
``production_plan`` back-reference and remain traceable. ``combine_sub_items``
is left off so each sub-assembly Work Order keeps the Sales Order link.

The delivery-priority net quantity is decided by the caller
(``get_allocated_production_row``); this module never re-derives demand.
"""

from __future__ import annotations

from contextlib import contextmanager

import frappe
from frappe.utils import now_datetime


@contextmanager
def _muted_messages():
	"""Swallow the engine's English msgprint output ("N created", warnings)
	so it never surfaces in the simplified UI."""
	original = frappe.msgprint
	frappe.msgprint = lambda *args, **kwargs: None
	try:
		yield
	finally:
		frappe.msgprint = original


def _work_orders_for_plan(production_plan: str):
	"""Work Orders created by the engine for this Production Plan, finished good
	first then sub-assemblies, in creation order."""
	if not production_plan:
		return []
	return frappe.get_all(
		"Work Order",
		filters={"production_plan": production_plan},
		pluck="name",
		order_by="creation asc",
	)


def create_work_orders_via_production_plan(
	*,
	sales_order: str,
	sales_order_item: str,
	company: str,
	item_code: str,
	bom_no: str,
	planned_qty: float,
	fg_warehouse: str | None,
	sub_assembly_warehouse: str | None,
	delivery_date=None,
):
	"""Create the finished-good Work Order and one Work Order per in-house
	sub-assembly level, via a saved Production Plan.

	Returns ``{production_plan, work_orders, sub_assembly_count}``.
	"""
	plan = frappe.new_doc("Production Plan")
	plan.company = company
	# Sub-assembly controls: explode and build every in-house level, but skip a
	# level already covered by sub-assembly stock in the given warehouse.
	plan.skip_available_sub_assembly_item = 1
	plan.sub_assembly_warehouse = sub_assembly_warehouse
	# Keep per-order Work Order links: combining would drop sales_order on the
	# aggregated sub-assembly rows.
	plan.combine_sub_items = 0

	plan.append(
		"po_items",
		{
			"item_code": item_code,
			"bom_no": bom_no,
			"planned_qty": planned_qty,
			"planned_start_date": now_datetime(),
			"warehouse": fg_warehouse,
			"sales_order": sales_order,
			"sales_order_item": sales_order_item,
			"include_exploded_items": 1,
		},
	)

	plan.flags.ignore_permissions = True
	# Persist first so make_work_order can stamp production_plan back-references.
	plan.insert(ignore_permissions=True)

	with _muted_messages():
		# Recursively resolve the multi-level BOM into sub_assembly_items,
		# skipping levels already covered by stock.
		plan.get_sub_assembly_items()
		# Create the finished-good WO plus one WO per remaining in-house level.
		plan.make_work_order()

	work_orders = _work_orders_for_plan(plan.name)
	return {
		"production_plan": plan.name,
		"work_orders": work_orders,
		"sub_assembly_count": len(plan.sub_assembly_items or []),
	}
