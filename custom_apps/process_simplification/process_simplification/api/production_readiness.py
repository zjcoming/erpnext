from __future__ import annotations

from collections import defaultdict
from copy import deepcopy

import frappe
from frappe.utils import flt, getdate

from process_simplification.api.workbench import order_item_priority_key


TERMINAL_WORK_ORDER_STATUSES = {"Completed", "Stopped", "Closed", "Cancelled"}


def plan_priority_key(plan) -> tuple:
	plan = frappe._dict(plan or {})
	return (
		str(plan.get("planned_date") or plan.get("posting_date") or "9999-12-31"),
		str(plan.get("creation") or ""),
		str(plan.get("name") or ""),
	)


def work_order_priority_key(plan, work_order) -> tuple:
	"""Prioritize order-linked work by its customer delivery commitment."""
	plan = frappe._dict(plan or {})
	work_order = frappe._dict(work_order or {})
	if work_order.get("sales_order") or work_order.get("sales_order_item"):
		return (
			False,
			*order_item_priority_key(
				{
					"delivery_date": work_order.get("order_delivery_date"),
					"order_creation": work_order.get("order_creation"),
					"sales_order": work_order.get("sales_order"),
					"sales_order_item_idx": work_order.get("sales_order_item_idx"),
					"sales_order_item": work_order.get("sales_order_item"),
				}
			),
		)
	return (True, *plan_priority_key(plan))


def _remaining_work_order_item_qty(work_order, item) -> float:
	completed_field = "consumed_qty" if work_order.get("skip_transfer") else "transferred_qty"
	return max(flt(item.get("required_qty")) - flt(item.get(completed_field)), 0)


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
		item.get("supply_type") == "purchased" and item.get("status") == "new_purchase_required"
		for item in items
	):
		return "purchase_shortage"
	if any(item.get("status") == "awaiting_purchase_receipt" for item in items):
		return "awaiting_purchase_receipt"
	if any(item.get("status") == "purchase_request_pending" for item in items):
		return "purchase_request_pending"
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


def allocate_work_order_readiness(plans, stock_snapshots, supply_documents=None):
	"""Allocate stock and inbound supply once by Sales Order Item delivery priority."""
	result = [deepcopy(plan) for plan in plans or []]
	supply_documents = supply_documents or {}
	reserved_stock = defaultdict(float)
	for plan in result:
		for work_order in plan.work_orders_by_name.values():
			if work_order.get("status") in TERMINAL_WORK_ORDER_STATUSES:
				work_order.required_items = []
				work_order.readiness_status = _work_order_readiness_status(work_order)
				continue
			for item in work_order.get("required_items") or []:
				key = (item.get("item_code"), item.get("source_warehouse"))
				reserved_stock[key] += min(
					max(flt(item.get("stock_reserved_qty")), 0),
					_remaining_work_order_item_qty(work_order, item),
				)
	remaining_stock = {
		key: max(flt((snapshot or {}).get("available_qty")) - reserved_stock.get(key, 0), 0)
		for key, snapshot in (stock_snapshots or {}).items()
	}
	remaining_supply = {}

	allocation_units = []
	for plan in result:
		for work_order_name in plan.execution_order:
			work_order = plan.work_orders_by_name[work_order_name]
			if work_order.get("status") in TERMINAL_WORK_ORDER_STATUSES:
				continue
			allocation_units.append(
				(
					work_order_priority_key(plan, work_order),
					-int(work_order.get("bom_level") or 0),
					str(work_order.get("creation") or ""),
					str(work_order.get("name") or ""),
					str(plan.get("name") or ""),
					plan,
					work_order,
				)
			)

	allocation_units.sort(key=lambda unit: unit[:5])
	for _, _, _, _, _, plan, work_order in allocation_units:
		allocated_items = []
		for source_item in work_order.get("required_items") or []:
			item = frappe._dict(deepcopy(dict(source_item)))
			remaining_required = _remaining_work_order_item_qty(work_order, item)
			key = (item.get("item_code"), item.get("source_warehouse"))
			available = remaining_stock.setdefault(
				key,
				max(
					flt((stock_snapshots or {}).get(key, {}).get("available_qty"))
					- reserved_stock.get(key, 0),
					0,
				),
			)
			reserved = min(max(flt(item.get("stock_reserved_qty")), 0), remaining_required)
			allocated_free = min(max(remaining_required - reserved, 0), available)
			allocated = reserved + allocated_free
			remaining_stock[key] = max(available - allocated_free, 0)
			item.original_required_qty = flt(item.get("required_qty"))
			item.required_qty = remaining_required
			item.actual_qty = flt((stock_snapshots or {}).get(key, {}).get("actual_qty"))
			item.committed_qty = flt((stock_snapshots or {}).get(key, {}).get("committed_qty"))
			item.available_qty = allocated
			item.current_gap_qty = max(remaining_required - allocated, 0)
			item.supply_type = "manufactured" if item.get("is_manufactured") else "purchased"
			item.child_work_order = (
				_child_work_order_for_item(plan, work_order, item.get("item_code"))
				if item.supply_type == "manufactured"
				else None
			)
			item.open_purchase_order_qty = 0
			item.open_material_request_qty = 0
			item.supply_documents = []
			if item.supply_type == "manufactured":
				item.shortage_qty = 0
				item.status = "waiting_subassembly" if item.current_gap_qty > 0 else "ready_now"
			else:
				uncovered = item.current_gap_qty
				for doctype in ("Purchase Order", "Material Request"):
					for source_document in sorted(
						[
							document
							for document in supply_documents.get(key, [])
							if document.get("doctype") == doctype
						],
						key=lambda document: (
							str(document.get("schedule_date") or "9999-12-31"),
							str(document.get("name") or ""),
							str(document.get("detail_name") or ""),
						),
					):
						document = frappe._dict(deepcopy(dict(source_document)))
						document_key = (
							key,
							document.get("doctype"),
							document.get("detail_name") or document.get("name"),
						)
						available_supply = remaining_supply.setdefault(
							document_key,
							max(flt(document.get("outstanding_qty")), 0),
						)
						deadline_comparable = bool(
							document.get("schedule_date") and work_order.get("order_delivery_date")
						)
						document.deadline_unknown = not deadline_comparable
						document.is_late = bool(
							deadline_comparable
							and getdate(document.get("schedule_date"))
							> getdate(work_order.get("order_delivery_date"))
						)
						document.allocated_qty = (
							0
							if document.is_late or document.deadline_unknown
							else min(uncovered, available_supply)
						)
						remaining_supply[document_key] = max(
							available_supply - document.allocated_qty,
							0,
						)
						uncovered = max(uncovered - document.allocated_qty, 0)
						if doctype == "Purchase Order":
							item.open_purchase_order_qty += document.allocated_qty
						else:
							item.open_material_request_qty += document.allocated_qty
						item.supply_documents.append(document)
				item.shortage_qty = uncovered
				if item.current_gap_qty <= 0:
					item.status = "ready_now"
				elif item.shortage_qty > 0:
					item.status = "new_purchase_required"
				elif item.open_material_request_qty > 0:
					item.status = "purchase_request_pending"
				else:
					item.status = "awaiting_purchase_receipt"
			allocated_items.append(item)
		work_order.required_items = allocated_items
		work_order.readiness_status = _work_order_readiness_status(work_order)
	for plan in result:
		plan.summary = summarize_plan(plan)
		work_order_priorities = [
			work_order_priority_key(plan, work_order)
			for work_order in plan.work_orders_by_name.values()
		]
		plan.material_priority_key = min(work_order_priorities) if work_order_priorities else (True,)
	result.sort(key=lambda plan: tuple(plan.get("material_priority_key") or (True,)))
	return result


def _earliest_plan_date(plan, plan_items, sub_assemblies):
	dates = [
		str(row.get("planned_start_date"))
		for row in plan_items
		if row.get("planned_start_date")
	]
	if not dates:
		dates = [
			str(row.get("schedule_date"))
			for row in sub_assemblies
			if row.get("schedule_date")
		]
	return min(dates) if dates else str(plan.get("posting_date") or "")


def _serialize_readiness_plan(plan):
	material_priority_dates = [
		str(work_order.get("order_delivery_date"))
		for work_order in plan.work_orders_by_name.values()
		if work_order.get("order_delivery_date")
	]
	return {
		"name": plan.get("name"),
		"company": plan.get("company"),
		"planned_date": plan.get("planned_date"),
		"material_priority_date": min(material_priority_dates) if material_priority_dates else None,
		"posting_date": plan.get("posting_date"),
		"status": plan.get("status"),
		"summary": plan.get("summary"),
		"work_orders": [
			plan.work_orders_by_name[name]
			for name in plan.get("execution_order") or []
		],
	}


def get_production_plan_readiness(company=None, sales_order_items=None):
	"""Return Work Order readiness grouped by Sales Order Item.

	Stock and inbound supply are allocated globally by Sales Order Item delivery
	priority. Required items are the direct Work Order requirements, so a manufactured
	item remains a subassembly dependency even when the Item is also purchasable.
	"""
	work_order_filters = {
		"docstatus": 1,
		"production_plan": ["!=", ""],
	}
	if company:
		work_order_filters["company"] = company
	work_orders = frappe.get_all(
		"Work Order",
		filters=work_order_filters,
		fields=[
			"name",
			"production_item",
			"bom_no",
			"production_plan",
			"production_plan_item",
			"production_plan_sub_assembly_item",
			"sales_order",
			"sales_order_item",
			"company",
			"status",
			"skip_transfer",
			"qty",
			"produced_qty",
			"process_loss_qty",
			"material_transferred_for_manufacturing",
			"source_warehouse",
			"wip_warehouse",
			"fg_warehouse",
			"planned_start_date",
			"expected_delivery_date",
			"creation",
		],
	)
	if not work_orders:
		return {}

	plan_names = sorted({row.get("production_plan") for row in work_orders if row.get("production_plan")})
	work_order_names = [row.get("name") for row in work_orders]
	required_items = frappe.get_all(
		"Work Order Item",
		filters={"parent": ["in", work_order_names]},
		fields=[
			"parent",
			"item_code",
			"item_name",
			"stock_uom",
			"source_warehouse",
			"required_qty",
			"transferred_qty",
			"consumed_qty",
			"stock_reserved_qty",
		],
	)
	plans = frappe.get_all(
		"Production Plan",
		filters={"name": ["in", plan_names]},
		fields=["name", "company", "posting_date", "creation", "status"],
	)
	plan_items = frappe.get_all(
		"Production Plan Item",
		filters={"parent": ["in", plan_names]},
		fields=[
			"name",
			"parent",
			"item_code",
			"planned_start_date",
			"sales_order",
			"sales_order_item",
		],
	)
	sub_assemblies = frappe.get_all(
		"Production Plan Sub Assembly Item",
		filters={"parent": ["in", plan_names]},
		fields=[
			"name",
			"parent",
			"production_item",
			"parent_item_code",
			"bom_level",
			"schedule_date",
			"type_of_manufacturing",
			"production_plan_item",
			"sales_order",
			"sales_order_item",
		],
	)
	linked_order_item_names = sorted(
		{
			row.get("sales_order_item")
			for row in [*work_orders, *plan_items, *sub_assemblies]
			if row.get("sales_order_item")
		}
	)
	order_items = (
		frappe.get_all(
			"Sales Order Item",
			filters={"name": ["in", linked_order_item_names]},
			fields=["name", "parent", "delivery_date", "idx"],
		)
		if linked_order_item_names
		else []
	)
	order_item_by_name = {row.get("name"): row for row in order_items}
	linked_order_names = sorted(
		{
			row.get("sales_order")
			for row in [*work_orders, *plan_items, *sub_assemblies]
			if row.get("sales_order")
		}
		| {row.get("parent") for row in order_items if row.get("parent")}
	)
	orders = (
		frappe.get_all(
			"Sales Order",
			filters={"name": ["in", linked_order_names]},
			fields=["name", "creation"],
		)
		if linked_order_names
		else []
	)
	order_by_name = {row.get("name"): row for row in orders}

	all_item_codes = {
		row.get("production_item") for row in work_orders if row.get("production_item")
	} | {row.get("item_code") for row in required_items if row.get("item_code")}
	active_bom_items = set(
		frappe.get_all(
			"BOM",
			filters={
				"item": ["in", sorted(all_item_codes)],
				"is_default": 1,
				"is_active": 1,
				"docstatus": 1,
			},
			pluck="item",
		)
	)
	item_rows = frappe.get_all(
		"Item",
		filters={"name": ["in", sorted(all_item_codes)]},
		fields=["name", "is_purchase_item"],
	)
	item_by_name = {row.get("name"): row for row in item_rows}
	for item in required_items:
		item.is_purchase_item = (item_by_name.get(item.get("item_code")) or {}).get(
			"is_purchase_item"
		)

	work_orders_by_plan = defaultdict(list)
	items_by_work_order = defaultdict(list)
	plan_items_by_plan = defaultdict(list)
	sub_assemblies_by_plan = defaultdict(list)
	for row in work_orders:
		work_orders_by_plan[row.get("production_plan")].append(row)
	for row in required_items:
		items_by_work_order[row.get("parent")].append(row)
	for row in plan_items:
		plan_items_by_plan[row.get("parent")].append(row)
	for row in sub_assemblies:
		sub_assemblies_by_plan[row.get("parent")].append(row)

	graphs = []
	for source_plan in plans:
		plan = frappe._dict(dict(source_plan))
		plan.planned_date = _earliest_plan_date(
			plan,
			plan_items_by_plan[plan.name],
			sub_assemblies_by_plan[plan.name],
		)
		plan_work_orders = work_orders_by_plan[plan.name]
		plan_item_by_name = {
			row.get("name"): row for row in plan_items_by_plan[plan.name]
		}
		sub_assembly_by_name = {
			row.get("name"): row for row in sub_assemblies_by_plan[plan.name]
		}
		for work_order in plan_work_orders:
			linked_row = (
				plan_item_by_name.get(work_order.get("production_plan_item"))
				or sub_assembly_by_name.get(work_order.get("production_plan_sub_assembly_item"))
				or {}
			)
			work_order.sales_order = work_order.get("sales_order") or linked_row.get("sales_order")
			work_order.sales_order_item = work_order.get("sales_order_item") or linked_row.get(
				"sales_order_item"
			)
			order_item = order_item_by_name.get(work_order.get("sales_order_item")) or {}
			work_order.sales_order = (
				work_order.get("sales_order")
				or linked_row.get("sales_order")
				or order_item.get("parent")
			)
			order = order_by_name.get(work_order.get("sales_order")) or {}
			work_order.order_delivery_date = str(order_item.get("delivery_date") or "") or None
			work_order.order_creation = str(order.get("creation") or "")
			work_order.sales_order_item_idx = order_item.get("idx")
		graph = build_work_order_graph(
			plan,
			plan_work_orders,
			[
				item
				for work_order in plan_work_orders
				for item in items_by_work_order[work_order.get("name")]
			],
			sub_assemblies_by_plan[plan.name],
			active_bom_items=active_bom_items,
		)
		graph.company = plan.get("company")
		graph.posting_date = str(plan.get("posting_date") or "")
		graph.status = plan.get("status")
		graphs.append(graph)

	from process_simplification.api.shortage import (
		_mr_documents,
		_po_documents,
		get_material_stock_snapshot,
	)

	stock_snapshots = {}
	supply_documents = {}
	for graph in graphs:
		for work_order in graph.work_orders_by_name.values():
			for item in work_order.get("required_items") or []:
				key = (item.get("item_code"), item.get("source_warehouse"))
				if key not in stock_snapshots:
					stock_snapshots[key] = get_material_stock_snapshot(*key)
				if not item.get("is_manufactured") and key not in supply_documents:
					supply_company = graph.get("company") or company
					supply_documents[key] = [
						*_po_documents(*key, supply_company, None),
						*_mr_documents(*key, supply_company, None),
					]

	readiness_plans = allocate_work_order_readiness(graphs, stock_snapshots, supply_documents)
	result = defaultdict(list)
	requested_order_items = set(sales_order_items or [])
	for plan in readiness_plans:
		serialized = _serialize_readiness_plan(plan)
		order_item_names = {
			row.get("sales_order_item")
			for row in plan.work_orders_by_name.values()
			if row.get("sales_order_item")
		}
		if not order_item_names:
			order_item_names = {
				row.get("sales_order_item")
				for row in plan_items_by_plan[plan.name]
				if row.get("sales_order_item")
			}
		for order_item_name in sorted(order_item_names):
			if requested_order_items and order_item_name not in requested_order_items:
				continue
			result[order_item_name].append(serialized)
	return dict(result)
