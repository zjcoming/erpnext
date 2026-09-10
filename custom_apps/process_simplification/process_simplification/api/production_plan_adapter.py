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

from process_simplification.production_workflow.planning_stock import commit_plan_stock, net_subassemblies
from process_simplification.production_workflow.stock_reservation import (
	allow_guided_stock_reservations_for,
)

_AUTO_SUBMIT_SAVEPOINT = "production_task_auto_submit"
_AUTO_JOB_CARD_WORK_ORDER_FLAG = "simplified_flow_job_card_work_order"


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


@contextmanager
def _allow_generated_job_cards_for(work_order: str):
	"""Permit only Job Cards generated while submitting this exact Work Order.

	Production managers intentionally have read-only native Job Card access. The
	simplified action still has to let ERPNext create the operation Job Cards that
	are a mandatory side effect of Work Order submission, without granting users
	a generic Job Card create permission.
	"""
	previous = getattr(frappe.flags, _AUTO_JOB_CARD_WORK_ORDER_FLAG, None)
	setattr(frappe.flags, _AUTO_JOB_CARD_WORK_ORDER_FLAG, work_order)
	try:
		yield
	finally:
		if previous is None:
			frappe.flags.pop(_AUTO_JOB_CARD_WORK_ORDER_FLAG, None)
		else:
			setattr(frappe.flags, _AUTO_JOB_CARD_WORK_ORDER_FLAG, previous)


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


def _submit_work_orders(work_orders: list[str]) -> None:
	for name in work_orders:
		with _allow_generated_job_cards_for(name), allow_guided_stock_reservations_for(
			"Work Order", name
		):
			frappe.get_doc("Work Order", name).submit()


def _apply_guided_source_warehouse(work_orders: list[str], source_warehouse: str | None) -> None:
	"""Keep ERPNext v16 from falling back to the finished-goods warehouse.

	Production Plan v16 derives a Work Order's source warehouse only from
	``BOM.default_source_warehouse``.  When that optional field is empty it
	falls back to the Work Order's finished-goods warehouse, which makes raw
	material procurement target the finished-goods warehouse.  The simplified
	flow already validated one guided source warehouse, so apply it to every
	generated draft Work Order and rebuild its direct required-item rows before
	submission.
	"""
	if not source_warehouse:
		return

	for name in work_orders:
		work_order = frappe.get_doc("Work Order", name)
		work_order.source_warehouse = source_warehouse
		work_order.set_required_items(reset_source_warehouse=True)
		work_order.save()


def _populate_raw_material_reservation_rows(plan, source_warehouse: str | None) -> None:
	"""Populate the child rows that ERPNext actually uses for Plan SREs.

	``reserve_stock`` is only an enable flag. Production Plan submission creates
	reservations from ``mr_items`` (and sub-assembly rows), so leaving that table
	empty produces no raw-material lock. Keep the same source warehouse that is
	later applied to generated Work Orders, otherwise the Plan reservation cannot
	be transferred to those Work Order Item rows.
	"""
	from erpnext.manufacturing.doctype.production_plan.production_plan import (
		get_items_for_material_requests,
	)

	plan.for_warehouse = source_warehouse
	plan.set("mr_items", [])
	for row in get_items_for_material_requests(plan.as_dict()):
		plan.append("mr_items", row)


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
	source_warehouse: str | None = None,
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

	frappe.db.savepoint(_AUTO_SUBMIT_SAVEPOINT)
	try:
		# Persist first so expanded sub-assembly rows receive stable names and
		# Work Orders can keep valid Production Plan back-references.
		plan.insert()
		commitments = net_subassemblies(plan)
		plan.reserve_stock = 0
		plan.save()
		with allow_guided_stock_reservations_for("Production Plan", plan.name):
			plan.submit()

		with _muted_messages():
			# Create the finished-good WO plus one WO per remaining in-house level.
			plan.make_work_order()

		work_orders = _work_orders_for_plan(plan.name)
		_apply_guided_source_warehouse(work_orders, source_warehouse)
		_submit_work_orders(work_orders)
		commit_plan_stock(plan, work_orders, commitments)
	except frappe.QueryDeadlockError:
		raise
	except Exception:
		frappe.db.rollback(save_point=_AUTO_SUBMIT_SAVEPOINT)
		raise

	return {
		"production_plan": plan.name,
		"work_orders": work_orders,
		"sub_assembly_count": len(plan.sub_assembly_items or []),
	}


def create_replenishment_work_orders_via_production_plan(
	*,
	material_request: str,
	company: str,
	source_warehouse: str | None,
	sub_assembly_warehouse: str | None,
	target_work_order: str,
	target_work_order_item: str,
):
	"""Create a traceable multi-level supplement chain from a Manufacture MR."""
	plan = frappe.new_doc("Production Plan")
	plan.company = company
	plan.get_items_from = "Material Request"
	# A supplement task is a recovery commitment, not merely a forecast. Let
	# ERPNext move exact stock reservations from the plan into every generated
	# Work Order so another urgent order cannot silently consume its inputs.
	plan.reserve_stock = 1
	plan.for_warehouse = source_warehouse
	plan.skip_available_sub_assembly_item = 1
	plan.sub_assembly_warehouse = sub_assembly_warehouse
	plan.combine_sub_items = 0
	plan.append(
		"material_requests",
		{
			"material_request": material_request,
			"material_request_date": frappe.db.get_value(
				"Material Request", material_request, "transaction_date"
			),
		},
	)

	frappe.db.savepoint(_AUTO_SUBMIT_SAVEPOINT)
	try:
		plan.get_items()
		if not plan.po_items:
			frappe.throw("补产申请没有可生成生产任务的物料。")
		plan.insert()
		commitments = net_subassemblies(plan)
		_populate_raw_material_reservation_rows(plan, source_warehouse)
		plan.reserve_stock = 0
		plan.save()
		with allow_guided_stock_reservations_for("Production Plan", plan.name):
			plan.submit()

		with _muted_messages():
			plan.make_work_order()

		work_orders = _work_orders_for_plan(plan.name)
		_apply_guided_source_warehouse(work_orders, source_warehouse)
		replenishment_work_orders = []
		for name in work_orders:
			work_order = frappe.get_doc("Work Order", name)
			if work_order.get("material_request") == material_request:
				work_order.custom_replenishes_work_order = target_work_order
				work_order.custom_replenishes_work_order_item = target_work_order_item
				work_order.save()
				replenishment_work_orders.append(name)
		if len(replenishment_work_orders) != 1:
			frappe.throw("补产申请必须且只能生成一个直接目标工单，请检查生产计划明细。")
		_submit_work_orders(work_orders)
		commit_plan_stock(plan, work_orders, commitments, reserve_raw=True)
	except frappe.QueryDeadlockError:
		raise
	except Exception:
		frappe.db.rollback(save_point=_AUTO_SUBMIT_SAVEPOINT)
		raise

	return {
		"production_plan": plan.name,
		"work_orders": work_orders,
		"replenishment_work_order": replenishment_work_orders[0],
		"sub_assembly_count": len(plan.sub_assembly_items or []),
	}
