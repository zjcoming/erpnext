from __future__ import annotations

from collections import defaultdict
from copy import deepcopy

import frappe
from frappe.utils import flt


TERMINAL_WORK_ORDER_STATUSES = {"Completed", "Stopped", "Closed", "Cancelled"}


def plan_priority_key(plan) -> tuple:
	plan = frappe._dict(plan or {})
	return (
		str(plan.get("planned_date") or plan.get("posting_date") or "9999-12-31"),
		str(plan.get("creation") or ""),
		str(plan.get("name") or ""),
	)


def build_work_order_graph(
	plan,
	work_orders,
	required_items,
	sub_assemblies,
	active_bom_items=None,
):
	plan = frappe._dict(plan or {})
	active_bom_items = set(active_bom_items or [])
	required_by_work_order = defaultdict(list)
	for item in required_items or []:
		required_by_work_order[item.get("parent")].append(frappe._dict(item))

	sub_assembly_by_name = {row.get("name"): frappe._dict(row) for row in sub_assemblies or []}
	work_orders_by_name = {}
	work_orders_by_item = defaultdict(list)
	for source in work_orders or []:
		row = frappe._dict(dict(source))
		sub_assembly = sub_assembly_by_name.get(row.get("production_plan_sub_assembly_item"))
		row.bom_level = int(sub_assembly.get("bom_level") or 0) + 1 if sub_assembly else 0
		row.priority_date = str(
			(sub_assembly or {}).get("schedule_date")
			or row.get("planned_start_date")
			or plan.get("planned_date")
			or plan.get("posting_date")
			or ""
		)
		row.parent_item_code = (sub_assembly or {}).get("parent_item_code")
		row.parent_work_order = None
		row.child_work_orders = []
		row.required_items = required_by_work_order.get(row.name, [])
		row.is_finished_good = not bool(sub_assembly)
		for item in row.required_items:
			item.is_manufactured = item.get("item_code") in active_bom_items
		work_orders_by_name[row.name] = row
		work_orders_by_item[row.get("production_item")].append(row)

	for rows in work_orders_by_item.values():
		rows.sort(key=lambda row: (str(row.get("creation") or ""), row.name))

	for row in work_orders_by_name.values():
		if not row.parent_item_code:
			continue
		parents = work_orders_by_item.get(row.parent_item_code) or []
		if not parents:
			continue
		parent = parents[0]
		row.parent_work_order = parent.name
		parent.child_work_orders.append(row.name)

	for row in work_orders_by_name.values():
		row.child_work_orders.sort(
			key=lambda name: (
				-work_orders_by_name[name].bom_level,
				str(work_orders_by_name[name].get("creation") or ""),
				name,
			)
		)

	execution_order = sorted(
		work_orders_by_name,
		key=lambda name: (
			-work_orders_by_name[name].bom_level,
			str(work_orders_by_name[name].get("creation") or ""),
			name,
		),
	)
	return frappe._dict(
		{
			"name": plan.get("name"),
			"planned_date": str(plan.get("planned_date") or plan.get("posting_date") or ""),
			"priority_key": plan_priority_key(plan),
			"work_orders_by_name": work_orders_by_name,
			"execution_order": execution_order,
		}
	)


def _child_work_order_for_item(graph, work_order, item_code):
	for child_name in work_order.get("child_work_orders") or []:
		child = graph.work_orders_by_name[child_name]
		if child.get("production_item") == item_code:
			return child_name
	return None


def _work_order_readiness_status(work_order):
	if work_order.get("status") in TERMINAL_WORK_ORDER_STATUSES:
		return "completed" if work_order.get("status") == "Completed" else "blocked"
	if flt(work_order.get("produced_qty")) > 0 or work_order.get("status") not in {
		None,
		"",
		"Submitted",
		"Not Started",
	}:
		return "in_progress"

	items = work_order.get("required_items") or []
	if not items or all(flt(item.get("required_qty")) <= 0 for item in items):
		return "materials_transferred"
	manufactured_gaps = [
		item
		for item in items
		if item.get("supply_type") == "manufactured" and flt(item.get("current_gap_qty")) > 0
	]
	if any(not item.get("child_work_order") for item in manufactured_gaps):
		return "production_task_missing"
	if any(
		item.get("supply_type") == "purchased" and flt(item.get("current_gap_qty")) > 0
		for item in items
	):
		return "purchase_shortage"
	if manufactured_gaps:
		return "waiting_subassembly"
	return "ready_now"


def summarize_plan(plan) -> dict:
	counts = defaultdict(int)
	for work_order in plan.work_orders_by_name.values():
		counts[work_order.get("readiness_status")] += 1
	return {
		"ready_work_order_count": counts["ready_now"],
		"waiting_subassembly_count": counts["waiting_subassembly"],
		"purchase_shortage_work_order_count": counts["purchase_shortage"],
		"awaiting_supply_work_order_count": counts["awaiting_purchase_receipt"]
		+ counts["purchase_request_pending"],
		"blocked_work_order_count": counts["blocked"] + counts["production_task_missing"],
		"completed_work_order_count": counts["completed"],
		"total_work_order_count": len(plan.work_orders_by_name),
	}


def allocate_work_order_readiness(plans, stock_snapshots):
	"""Allocate current stock once, by Production Plan date and deepest Work Order first."""
	result = [deepcopy(plan) for plan in plans or []]
	result.sort(key=lambda plan: tuple(plan.get("priority_key") or ()))
	remaining_stock = {
		key: max(flt((snapshot or {}).get("available_qty")), 0)
		for key, snapshot in (stock_snapshots or {}).items()
	}

	for plan in result:
		for work_order_name in plan.execution_order:
			work_order = plan.work_orders_by_name[work_order_name]
			allocated_items = []
			for source_item in work_order.get("required_items") or []:
				item = frappe._dict(deepcopy(dict(source_item)))
				remaining_required = max(
					flt(item.get("required_qty")) - flt(item.get("transferred_qty")),
					0,
				)
				key = (item.get("item_code"), item.get("source_warehouse"))
				available = remaining_stock.setdefault(
					key,
					max(flt((stock_snapshots or {}).get(key, {}).get("available_qty")), 0),
				)
				allocated = min(remaining_required, available)
				remaining_stock[key] = max(available - allocated, 0)
				item.original_required_qty = flt(item.get("required_qty"))
				item.required_qty = remaining_required
				item.actual_qty = flt((stock_snapshots or {}).get(key, {}).get("actual_qty"))
				item.available_qty = allocated
				item.current_gap_qty = max(remaining_required - allocated, 0)
				item.supply_type = "manufactured" if item.get("is_manufactured") else "purchased"
				item.child_work_order = (
					_child_work_order_for_item(plan, work_order, item.get("item_code"))
					if item.supply_type == "manufactured"
					else None
				)
				allocated_items.append(item)
			work_order.required_items = allocated_items
			work_order.readiness_status = _work_order_readiness_status(work_order)
		plan.summary = summarize_plan(plan)
	return result
