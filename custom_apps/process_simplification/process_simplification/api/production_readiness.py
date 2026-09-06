from __future__ import annotations

from collections import defaultdict
from copy import deepcopy

import frappe
from frappe.utils import flt, getdate

from process_simplification.api.utils import resolve_item_display_name
from process_simplification.api.workbench import order_item_priority_key
from process_simplification.production_stock_facts import (
	get_work_order_stock_fact,
	load_work_order_stock_facts,
)


TERMINAL_WORK_ORDER_STATUSES = {"Completed", "Stopped", "Closed", "Cancelled"}
COMPLETED_JOB_CARD_STATUS = "Completed"
MANUFACTURE_PURPOSE = "Manufacture"
MATERIAL_ISSUE_PURPOSE = "Material Transfer for Manufacture"
QTY_EPSILON = 1e-9


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
	if item.get("stock_fact_loaded"):
		completed_qty = flt(item.get("stock_fact_completed_qty"))
	elif work_order.get("skip_transfer"):
		completed_qty = flt(item.get("consumed_qty"))
	else:
		# A submitted material return reduces what is actually available in WIP.
		# ERPNext keeps the gross transfer and return on separate child fields, so
		# using transferred_qty alone would incorrectly treat returned material as
		# still issued to the Work Order.
		completed_qty = max(flt(item.get("transferred_qty")) - flt(item.get("returned_qty")), 0)
	remaining_qty = max(flt(item.get("required_qty")) - completed_qty, 0)
	return 0 if remaining_qty <= QTY_EPSILON else remaining_qty


def _work_order_item_issue_warehouse(work_order, item):
	"""Return the warehouse that must physically cover this Work Order item."""
	if work_order.get("skip_transfer") and work_order.get("from_wip_warehouse"):
		return work_order.get("wip_warehouse")
	return item.get("issue_warehouse") or item.get("source_warehouse")


def attach_work_order_item_issue_warehouses(work_orders, required_items):
	"""Attach the effective issue warehouse before exact SRE and stock loading."""
	work_order_by_name = {row.get("name"): frappe._dict(row) for row in work_orders or []}
	for item in required_items or []:
		work_order = work_order_by_name.get(item.get("parent")) or frappe._dict()
		item["issue_warehouse"] = _work_order_item_issue_warehouse(work_order, item)
	return required_items


def attach_work_order_stock_facts(work_orders, required_items, stock_facts=None):
	"""Attach one submitted-ledger quantity per material/source group.

	ERPNext repeats aggregate transfer/consumption values on operation-split Work
	Order Item rows.  Distribute the real group fact across those rows once so the
	readiness allocator neither duplicates completed quantity nor loses a partial
	group gap.  Exact-source reservations are redistributed over the remaining
	rows for the same reason.
	"""
	work_order_by_name = {
		row.get("name"): frappe._dict(row) for row in work_orders or [] if row.get("name")
	}
	if stock_facts is None:
		stock_facts = load_work_order_stock_facts(sorted(work_order_by_name))
	groups = defaultdict(list)
	for index, source in enumerate(required_items or []):
		item = source
		work_order = work_order_by_name.get(item.get("parent")) or frappe._dict()
		key = (
			item.get("parent"),
			item.get("item_code"),
			_work_order_item_issue_warehouse(work_order, item),
		)
		groups[key].append((index, item))

	for (work_order_name, item_code, warehouse), members in groups.items():
		work_order = work_order_by_name.get(work_order_name) or frappe._dict()
		fact = get_work_order_stock_fact(
			stock_facts, work_order_name, item_code, warehouse
		)
		completed_qty = max(
			flt(fact.consumed_qty)
			if work_order.get("skip_transfer")
			else flt(fact.net_issued_qty),
			0,
		)
		members.sort(
			key=lambda member: (
				int(member[1].get("idx") or 0),
				str(member[1].get("name") or ""),
				member[0],
			)
		)
		remaining_completed = completed_qty
		for _index, item in members:
			row_completed = min(
				max(flt(item.get("required_qty")), 0), remaining_completed
			)
			item["stock_fact_loaded"] = 1
			item["stock_fact_completed_qty"] = row_completed
			item["stock_fact_group_qty"] = completed_qty
			item["stock_fact_gross_issued_qty"] = flt(fact.gross_issued_qty)
			item["stock_fact_returned_qty"] = flt(fact.returned_qty)
			remaining_completed = max(remaining_completed - row_completed, 0)
		if remaining_completed > QTY_EPSILON and members:
			# Keep an over-issue visible without allowing it to satisfy another group.
			members[0][1]["stock_fact_completed_qty"] += remaining_completed

		remaining_reservation = sum(
			_source_reserved_qty(item) for _index, item in members
		)
		for _index, item in members:
			row_remaining = max(
				flt(item.get("required_qty"))
				- flt(item.get("stock_fact_completed_qty")),
				0,
			)
			item["source_reserved_qty"] = min(row_remaining, remaining_reservation)
			remaining_reservation = max(
				remaining_reservation - item["source_reserved_qty"], 0
			)
	return required_items


def attach_work_order_execution_facts(
	work_orders,
	job_cards=None,
	draft_stock_entries=None,
	work_order_operations=None,
):
	"""Attach batch-loaded Job Card and draft Stock Entry facts to Work Orders."""
	operation_facts_loaded = work_order_operations is not None
	operation_count_by_work_order = defaultdict(int)
	for operation in work_order_operations or []:
		if operation.get("parent"):
			operation_count_by_work_order[operation.get("parent")] += 1

	job_cards_by_work_order = defaultdict(list)
	for source in job_cards or []:
		job_card = frappe._dict(source)
		if job_card.get("work_order"):
			job_cards_by_work_order[job_card.work_order].append(job_card)

	drafts_by_work_order = defaultdict(lambda: defaultdict(list))
	for source in draft_stock_entries or []:
		stock_entry = frappe._dict(source)
		if stock_entry.get("work_order") and stock_entry.get("purpose"):
			drafts_by_work_order[stock_entry.work_order][stock_entry.purpose].append(stock_entry)

	for work_order in work_orders or []:
		cards = sorted(
			job_cards_by_work_order.get(work_order.get("name"), []),
			key=lambda row: (str(row.get("creation") or ""), str(row.get("name") or "")),
		)
		completed_cards = [
			row
			for row in cards
			if int(row.get("docstatus") or 0) == 1
			and row.get("status") == COMPLETED_JOB_CARD_STATUS
		]
		started_cards = [
			row
			for row in cards
			if row.get("status") in {"Work In Progress", "On Hold", COMPLETED_JOB_CARD_STATUS}
		]
		work_order["operation_count"] = operation_count_by_work_order.get(
			work_order.get("name"),
			0 if operation_facts_loaded else len(cards),
		)
		work_order["job_card_count"] = len(cards)
		work_order["completed_job_card_count"] = len(completed_cards)
		work_order["started_job_card_count"] = len(started_cards)
		work_order["job_card_names"] = [row.get("name") for row in cards if row.get("name")]
		work_order["incomplete_job_card_names"] = [
			row.get("name")
			for row in cards
			if row.get("name") and row.get("status") != COMPLETED_JOB_CARD_STATUS
		]

		receipt_drafts = sorted(
			drafts_by_work_order[work_order.get("name")].get(MANUFACTURE_PURPOSE, []),
			key=lambda row: (str(row.get("creation") or ""), str(row.get("name") or "")),
		)
		issue_drafts = sorted(
			drafts_by_work_order[work_order.get("name")].get(MATERIAL_ISSUE_PURPOSE, []),
			key=lambda row: (str(row.get("creation") or ""), str(row.get("name") or "")),
		)
		work_order["draft_receipt_stock_entries"] = [
			row.get("name") for row in receipt_drafts if row.get("name")
		]
		work_order["draft_issue_stock_entries"] = [
			row.get("name") for row in issue_drafts if row.get("name")
		]
		work_order["draft_receipt_stock_entry"] = (
			work_order["draft_receipt_stock_entries"][0]
			if work_order["draft_receipt_stock_entries"]
			else None
		)
		work_order["draft_issue_stock_entry"] = (
			work_order["draft_issue_stock_entries"][0]
			if work_order["draft_issue_stock_entries"]
			else None
		)
	return work_orders


def attach_replenishment_work_orders(required_items, supply_work_orders=None):
	"""Attach active supplement Work Orders to their exact target material row."""
	by_target_item = defaultdict(list)
	by_target_and_code = defaultdict(list)
	for source in supply_work_orders or []:
		supply_work_order = frappe._dict(source)
		target_work_order = supply_work_order.get("custom_replenishes_work_order")
		target_item = supply_work_order.get("custom_replenishes_work_order_item")
		if not target_work_order:
			continue
		if target_item:
			by_target_item[target_item].append(supply_work_order)
		by_target_and_code[(target_work_order, supply_work_order.get("production_item"))].append(
			supply_work_order
		)

	for item in required_items or []:
		matches = list(by_target_item.get(item.get("name"), []))
		if not matches:
			matches = list(
				by_target_and_code.get((item.get("parent"), item.get("item_code")), [])
			)
		matches.sort(key=lambda row: (str(row.get("creation") or ""), str(row.get("name") or "")))
		item["supply_work_orders"] = [row.get("name") for row in matches if row.get("name")]
		item["replenishment_in_progress"] = bool(item["supply_work_orders"])
	return required_items


def attach_work_order_source_reservations(required_items, stock_reservation_entries=None):
	"""Attach remaining reservations for each row's exact effective issue warehouse.

	``Work Order Item.stock_reserved_qty`` is aggregated by voucher detail and can
	therefore include reservations in a WIP warehouse after a partial transfer.
	Readiness must only protect stock that still belongs to the row's current
	effective warehouse (WIP for direct consumption configured from WIP).
	"""
	reservation_delta_by_source = defaultdict(float)
	for source in stock_reservation_entries or []:
		entry = frappe._dict(source)
		key = (
			entry.get("voucher_detail_no"),
			entry.get("item_code"),
			entry.get("warehouse"),
		)
		if not all(key):
			continue
		reservation_delta_by_source[key] += (
			flt(entry.get("reserved_qty"))
			- flt(entry.get("delivered_qty"))
			- flt(entry.get("transferred_qty"))
			- flt(entry.get("consumed_qty"))
		)

	for item in required_items or []:
		item["source_reserved_qty"] = max(
			reservation_delta_by_source.get(
				(
					item.get("name"),
					item.get("item_code"),
					item.get("issue_warehouse") or item.get("source_warehouse"),
				),
				0,
			),
			0,
		)
	return required_items


def _source_reserved_qty(item) -> float:
	"""Return exact-source reservation, with legacy pure-input compatibility."""
	value = item.get("source_reserved_qty")
	if value is None:
		value = item.get("stock_reserved_qty")
	return max(flt(value), 0)


def _load_active_replenishment_work_orders(target_work_orders):
	if not target_work_orders:
		return []
	meta = frappe.get_meta("Work Order")
	required_fields = {
		"custom_replenishes_work_order",
		"custom_replenishes_work_order_item",
	}
	if not all(meta.has_field(fieldname) for fieldname in required_fields):
		return []
	return frappe.get_list(
		"Work Order",
		filters={
			"docstatus": 1,
			"status": ["not in", sorted(TERMINAL_WORK_ORDER_STATUSES)],
			"custom_replenishes_work_order": ["in", sorted(target_work_orders)],
		},
		fields=[
			"name",
			"production_item",
			"qty",
			"produced_qty",
			"status",
			"creation",
			"custom_replenishes_work_order",
			"custom_replenishes_work_order_item",
		],
		limit=0,
	)


def attach_work_order_item_names(work_orders, item_by_name):
	"""Attach the Item master name without replacing the transactional item code."""
	for work_order in work_orders or []:
		item = item_by_name.get(work_order.get("production_item")) or {}
		work_order["production_item_name"] = resolve_item_display_name(
			work_order.get("production_item"),
			item.get("item_name"),
			work_order.get("production_item_name"),
		)
	return work_orders


def _free_stock_qty(snapshot, loaded_reserved_qty=0) -> float:
	snapshot = frappe._dict(snapshot or {})
	if snapshot.get("free_qty") is not None:
		return max(flt(snapshot.get("free_qty")), 0)
	return max(flt(snapshot.get("available_qty")) - flt(loaded_reserved_qty), 0)


def _loaded_work_order_stock_pool(
	snapshot,
	loaded_commitment_qty=0,
	loaded_plan_reservation_qty=0,
) -> float:
	"""Return stock allocatable across the Work Orders loaded by this workbench.

	ERPNext v16 includes every active Work Order's remaining requirement in
	``Bin.reserved_qty_for_production`` even when the child row has no explicit
	exact-source reservation.  For manufactured subassemblies it also puts
	the loaded Production Plan's outstanding subassembly output in
	``reserved_qty_for_production_plan``.  Both figures describe commitments that
	are already represented by the loaded graph, so add them back without
	stealing quantity committed to standalone Work Orders or unloaded plans.
	"""
	snapshot = frappe._dict(snapshot or {})
	if snapshot.get("production_committed_qty") is None:
		return _free_stock_qty(snapshot)
	external_commitment_qty = max(
		flt(snapshot.get("production_committed_qty"))
		- flt(loaded_commitment_qty)
		- flt(loaded_plan_reservation_qty),
		0,
	)
	return max(flt(snapshot.get("available_qty")) - external_commitment_qty, 0)


def build_allocation_conflict(
	*,
	current_gap_qty,
	gap_without_priority_qty,
	other_hard_reserved_qty=0,
	prior_allocations=None,
):
	"""Explain only the part of a stock gap caused by loaded Work Order commitments."""
	gap_qty = max(flt(current_gap_qty), 0)
	gap_without_priority_qty = min(max(flt(gap_without_priority_qty), 0), gap_qty)
	priority_impact_qty = max(gap_qty - gap_without_priority_qty, 0)
	hard_reserved_impact_qty = min(
		gap_without_priority_qty,
		max(flt(other_hard_reserved_qty), 0),
	)

	remaining_priority_impact = priority_impact_qty
	sources = []
	for source in prior_allocations or []:
		if remaining_priority_impact <= QTY_EPSILON:
			break
		source = frappe._dict(source)
		allocated_qty = max(flt(source.get("allocated_qty")), 0)
		if allocated_qty <= QTY_EPSILON:
			continue
		impact_qty = min(allocated_qty, remaining_priority_impact)
		sources.append(
			{
				"source_type": "priority_allocation",
				"work_order": source.get("work_order"),
				"sales_order": source.get("sales_order"),
				"delivery_date": source.get("delivery_date"),
				"allocated_qty": allocated_qty,
				"impact_qty": impact_qty,
			}
		)
		remaining_priority_impact = max(remaining_priority_impact - impact_qty, 0)

	# The formula above guarantees enough ledger quantity; normalize defensively
	# so source impact and the reported total can never diverge on stale inputs.
	priority_impact_qty = sum(flt(source.get("impact_qty")) for source in sources)
	return frappe._dict(
		priority_impact_qty=priority_impact_qty,
		hard_reserved_impact_qty=hard_reserved_impact_qty,
		sources=sources,
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

	sub_assembly_rows = [frappe._dict(row) for row in sub_assemblies or []]
	sub_assembly_by_name = {row.get("name"): row for row in sub_assembly_rows}
	plan_reservations = defaultdict(float)
	for row in sub_assembly_rows:
		item_code = row.get("production_item")
		warehouse = row.get("fg_warehouse")
		if not item_code or not warehouse:
			continue
		planned_qty = flt(row.get("qty")) if flt(row.get("qty")) > 0 else flt(row.get("required_qty"))
		plan_reservations[(item_code, warehouse)] += max(
			planned_qty - flt(row.get("wo_produced_qty")),
			0,
		)
	work_orders_by_name = {}
	work_orders_by_item = defaultdict(list)
	for source in work_orders or []:
		row = frappe._dict(dict(source))
		sub_assembly = sub_assembly_by_name.get(row.get("production_plan_sub_assembly_item"))
		row.production_plan_item = row.get("production_plan_item") or (sub_assembly or {}).get(
			"production_plan_item"
		)
		row.sales_order = row.get("sales_order") or (sub_assembly or {}).get("sales_order")
		row.sales_order_item = row.get("sales_order_item") or (sub_assembly or {}).get(
			"sales_order_item"
		)
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
		row.graph_link_ambiguous = False
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
		parents = [
			parent
			for parent in work_orders_by_item.get(row.parent_item_code) or []
			if parent.get("bom_level") == row.get("bom_level") - 1
		]
		for fieldname in ("production_plan_item", "sales_order_item"):
			value = row.get(fieldname)
			if value:
				parents = [parent for parent in parents if parent.get(fieldname) == value]
		parents = [
			parent
			for parent in parents
			if any(
				item.get("item_code") == row.get("production_item")
				for item in parent.get("required_items") or []
			)
		]
		if not parents:
			continue
		if len(parents) > 1:
			row.graph_link_ambiguous = True
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
			"plan_reservations": dict(plan_reservations),
		}
	)


def _child_work_order_for_item(graph, work_order, item_code):
	for child_name in work_order.get("child_work_orders") or []:
		child = graph.work_orders_by_name[child_name]
		if (
			child.get("production_item") == item_code
			and child.get("status") not in TERMINAL_WORK_ORDER_STATUSES
		):
			return child_name
	return None


def _completed_child_work_orders_for_item(graph, work_order, item_code):
	return [
		child_name
		for child_name in work_order.get("child_work_orders") or []
		if graph.work_orders_by_name[child_name].get("production_item") == item_code
		and graph.work_orders_by_name[child_name].get("status") == "Completed"
	]


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
	if any(item.get("blocked") or item.get("status") == "cannot_calculate" for item in items):
		return "blocked"
	manufactured_gaps = [
		item
		for item in items
		if item.get("supply_type") == "manufactured" and flt(item.get("current_gap_qty")) > 0
	]
	if any(item.get("replenishment_required") for item in manufactured_gaps):
		return "replenishment_required"
	if any(
		not item.get("child_work_order") and not item.get("replenishment_in_progress")
		for item in manufactured_gaps
	):
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


def _flow_item_net_qty(work_order, item) -> float:
	net_qty = (
		item.get("stock_fact_completed_qty")
		if item.get("stock_fact_loaded")
		else item.get("net_transferred_qty")
	)
	if net_qty is None:
		net_qty = (
			item.get("consumed_qty")
			if work_order.get("skip_transfer")
			else flt(item.get("transferred_qty")) - flt(item.get("returned_qty"))
		)
	return max(flt(net_qty), 0)


def _flow_material_groups(work_order, items):
	"""Merge operation-split rows whose transfer quantity is stored as one aggregate."""
	groups = {}
	for index, item in enumerate(items or []):
		if item.get("include_item_in_manufacturing") not in (None, 1, "1"):
			continue
		required_qty = flt(item.get("original_required_qty", item.get("required_qty")))
		if required_qty <= QTY_EPSILON:
			continue
		item_code = item.get("item_code")
		source_warehouse = _work_order_item_issue_warehouse(work_order, item)
		key = (
			(item_code, source_warehouse)
			if item_code
			else ("__row__", index)
		)
		group = groups.setdefault(
			key,
			frappe._dict(
				item_code=item_code,
				source_warehouse=source_warehouse,
				required_qty=0,
				net_qty=0,
				available_qty=0,
				blocked=False,
				stock_fact_loaded=False,
			),
		)
		group.required_qty += required_qty
		if item.get("stock_fact_loaded"):
			# The authoritative group fact was distributed once across its member
			# rows, so summing reconstructs that one submitted-ledger quantity.
			if not group.stock_fact_loaded:
				group.net_qty = 0
			group.stock_fact_loaded = True
			group.net_qty += _flow_item_net_qty(work_order, item)
		elif not group.stock_fact_loaded:
			# Legacy pure fixtures repeat one child aggregate on every split row.
			group.net_qty = max(group.net_qty, _flow_item_net_qty(work_order, item))
		group.available_qty += max(flt(item.get("available_qty")), 0)
		group.blocked = bool(
			group.blocked
			or item.get("blocked")
			or item.get("status") == "cannot_calculate"
		)

	result = []
	for group in groups.values():
		group.remaining_qty = max(group.required_qty - group.net_qty, 0)
		if group.remaining_qty <= QTY_EPSILON:
			group.remaining_qty = 0
		result.append(group)
	return result


def derive_work_order_flow_state(work_order) -> dict:
	"""Derive execution states without changing the legacy material readiness state.

	``readiness_status`` remains the stock/supply answer used by existing callers.
	These additional fields describe the operational hand-offs: Job Cards, receipt,
	material issue, and the one next state shown by the production workbench.
	"""
	work_order = frappe._dict(work_order or {})
	status = work_order.get("status")
	terminal = status in TERMINAL_WORK_ORDER_STATUSES
	completed_work_order = status == "Completed"

	job_card_count = int(work_order.get("job_card_count") or 0)
	completed_job_card_count = int(work_order.get("completed_job_card_count") or 0)
	started_job_card_count = int(work_order.get("started_job_card_count") or 0)
	operation_count_value = work_order.get("operation_count")
	operation_count = int(
		job_card_count if operation_count_value is None else operation_count_value or 0
	)
	if completed_work_order:
		operation_code = "completed"
	elif terminal:
		operation_code = "pending"
	elif not operation_count:
		operation_code = "not_applicable"
	elif (
		job_card_count >= operation_count
		and job_card_count > 0
		and completed_job_card_count >= job_card_count
	):
		operation_code = "completed"
	elif completed_job_card_count > 0 or started_job_card_count > 0:
		operation_code = "in_progress"
	else:
		operation_code = "pending"
	operation_state = frappe._dict(
		code=operation_code,
		total_operations=operation_count,
		total_job_cards=job_card_count,
		completed_job_cards=completed_job_card_count,
	)

	material_groups = _flow_material_groups(
		work_order,
		work_order.get("required_items") or [],
	)
	remaining_issue_qty = sum(group.remaining_qty for group in material_groups)
	net_issued_qty = sum(group.net_qty for group in material_groups)
	net_transfer_coverage_fraction = (
		min(
			min(group.net_qty / group.required_qty, 1)
			for group in material_groups
		)
		if material_groups
		else 1
	)
	issue_not_required = not material_groups or bool(work_order.get("skip_transfer"))
	all_materials_issued = not material_groups or remaining_issue_qty <= QTY_EPSILON
	remaining_items = [
		group for group in material_groups if group.remaining_qty > 0
	]
	if remaining_items and not any(group.blocked for group in remaining_items):
		issueable_fraction = min(
			min(
				group.available_qty / group.remaining_qty,
				1,
			)
			for group in remaining_items
		)
	else:
		issueable_fraction = 0
	materials_available_now = bool(remaining_items) and issueable_fraction + QTY_EPSILON >= 1
	materials_partially_available = bool(remaining_items) and (
		QTY_EPSILON < issueable_fraction < 1 - QTY_EPSILON
	)
	remaining_finished_qty_to_issue = max(
		flt(work_order.get("qty")) * (1 - net_transfer_coverage_fraction),
		0,
	)
	if remaining_finished_qty_to_issue <= QTY_EPSILON:
		remaining_finished_qty_to_issue = 0
	additional_issueable_qty = min(
		remaining_finished_qty_to_issue * issueable_fraction,
		remaining_finished_qty_to_issue,
	)
	has_issue_draft = bool(work_order.get("draft_issue_stock_entry"))
	if completed_work_order:
		issue_code = "issued"
	elif terminal:
		issue_code = "waiting_material"
	elif issue_not_required:
		issue_code = "not_required"
	elif all_materials_issued:
		issue_code = "issued"
	elif has_issue_draft:
		issue_code = "draft_pending"
	elif materials_available_now:
		issue_code = "ready"
	elif materials_partially_available:
		issue_code = "partially_ready"
	elif net_issued_qty > 0:
		issue_code = "partially_issued"
	else:
		issue_code = "waiting_material"
	issue_state = frappe._dict(
		code=issue_code,
		net_issued_qty=net_issued_qty,
		remaining_qty=remaining_issue_qty,
		issueable_fraction=issueable_fraction,
		net_transfer_coverage_fraction=net_transfer_coverage_fraction,
		additional_issueable_qty=additional_issueable_qty,
	)
	if has_issue_draft:
		issue_state.draft_entry = frappe._dict(name=work_order.get("draft_issue_stock_entry"))

	can_request_issue = bool(
		not terminal
		and not work_order.get("skip_transfer")
		and material_groups
		and not all_materials_issued
		and not has_issue_draft
		and issue_code in {"ready", "partially_ready"}
	)
	# Standard Work Orders dispatch only after posted net issue. Direct-consumption
	# Work Orders have no issue document, but still need full on-site coverage.
	can_dispatch = bool(
		not terminal
		and (
			all_materials_issued
			or (
				work_order.get("skip_transfer")
				and (not material_groups or materials_available_now)
			)
		)
	)

	transfer_base_qty = flt(work_order.get("qty")) * (
		1 if work_order.get("skip_transfer") else net_transfer_coverage_fraction
	)
	receipt_remaining_qty = max(
		transfer_base_qty
		- flt(work_order.get("produced_qty"))
		- flt(work_order.get("process_loss_qty")),
		0,
	)
	if receipt_remaining_qty <= QTY_EPSILON:
		receipt_remaining_qty = 0
	has_receipt_draft = bool(work_order.get("draft_receipt_stock_entry"))
	operations_completed = operation_code in {"completed", "not_applicable"}
	if completed_work_order:
		receipt_code = "received"
	elif terminal:
		receipt_code = "not_ready"
	elif has_receipt_draft:
		receipt_code = "draft_pending"
	elif operations_completed and receipt_remaining_qty > 0:
		receipt_code = "requestable"
	else:
		receipt_code = "not_ready"
	receipt_state = frappe._dict(
		code=receipt_code,
		remaining_qty=receipt_remaining_qty,
		produced_qty=flt(work_order.get("produced_qty")),
		process_loss_qty=flt(work_order.get("process_loss_qty")),
	)
	if has_receipt_draft:
		receipt_state.draft_entry = frappe._dict(name=work_order.get("draft_receipt_stock_entry"))

	can_request_receipt = bool(
		not terminal
		and receipt_remaining_qty > 0
		and receipt_code == "requestable"
	)

	if completed_work_order:
		flow_status = "completed"
	elif terminal:
		flow_status = "blocked"
	elif receipt_code == "draft_pending":
		flow_status = "awaiting_receipt_submission"
	elif can_request_receipt:
		flow_status = "awaiting_receipt_request"
	elif operation_code == "in_progress":
		flow_status = "in_production"
	elif issue_code == "draft_pending":
		flow_status = "awaiting_issue_submission"
	elif can_dispatch:
		flow_status = (
			"materials_ready_waiting_dispatch"
			if work_order.get("skip_transfer")
			else "issued_waiting_dispatch"
		)
	elif issue_code == "ready":
		flow_status = "ready_for_issue"
	elif issue_code == "partially_ready":
		flow_status = "partially_ready_for_issue"
	else:
		flow_status = work_order.get("readiness_status") or "not_ready"

	return {
		"operation_state": operation_state,
		"receipt_state": receipt_state,
		"issue_state": issue_state,
		"flow_status": flow_status,
		"can_request_receipt": can_request_receipt,
		"can_request_issue": can_request_issue,
		"can_dispatch": can_dispatch,
		"net_issued_qty": net_issued_qty,
		"remaining_issue_qty": remaining_issue_qty,
		"production_remaining_qty": receipt_remaining_qty,
	}


def _apply_work_order_flow_state(work_order):
	work_order.update(derive_work_order_flow_state(work_order))
	return work_order


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
	loaded_commitments = defaultdict(float)
	loaded_plan_reservations = defaultdict(float)
	for plan in result:
		for key, qty in (plan.get("plan_reservations") or {}).items():
			loaded_plan_reservations[key] += max(flt(qty), 0)
		for work_order in plan.work_orders_by_name.values():
			if work_order.get("status") in TERMINAL_WORK_ORDER_STATUSES:
				work_order.required_items = []
				work_order.readiness_status = _work_order_readiness_status(work_order)
				continue
			for item in work_order.get("required_items") or []:
				key = (
					item.get("item_code"),
					_work_order_item_issue_warehouse(work_order, item),
				)
				remaining_qty = _remaining_work_order_item_qty(work_order, item)
				loaded_commitments[key] += remaining_qty
				reserved_stock[key] += min(
					_source_reserved_qty(item),
					remaining_qty,
				)
	remaining_stock = {
		key: max(
			_loaded_work_order_stock_pool(
				snapshot,
				loaded_commitments.get(key, 0),
				loaded_plan_reservations.get(key, 0),
			)
			- reserved_stock.get(key, 0),
			0,
		)
		for key, snapshot in (stock_snapshots or {}).items()
	}
	remaining_supply = {}
	priority_allocations = defaultdict(list)

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
			item.issue_warehouse = _work_order_item_issue_warehouse(work_order, item)
			key = (item.get("item_code"), item.issue_warehouse)
			snapshot = frappe._dict((stock_snapshots or {}).get(key) or {})
			item.original_required_qty = flt(item.get("required_qty"))
			item.required_qty = remaining_required
			item.net_transferred_qty = max(
				flt(item.get("stock_fact_completed_qty"))
				if item.get("stock_fact_loaded")
				else (
					flt(item.get("consumed_qty"))
					if work_order.get("skip_transfer")
					else flt(item.get("transferred_qty")) - flt(item.get("returned_qty"))
				),
				0,
			)
			item.remaining_issue_qty = remaining_required
			item.actual_qty = flt(snapshot.get("actual_qty"))
			item.committed_qty = flt(snapshot.get("committed_qty"))
			item.supply_type = "manufactured" if item.get("is_manufactured") else "purchased"
			item.child_work_order = (
				_child_work_order_for_item(plan, work_order, item.get("item_code"))
				if item.supply_type == "manufactured"
				else None
			)
			item.completed_child_work_orders = (
				_completed_child_work_orders_for_item(
					plan, work_order, item.get("item_code")
				)
				if item.supply_type == "manufactured"
				else []
			)
			item.supply_work_orders = list(item.get("supply_work_orders") or [])
			item.replenishment_in_progress = bool(item.get("replenishment_in_progress"))
			item.replenishment_required = False
			item.open_purchase_order_qty = 0
			item.open_material_request_qty = 0
			item.supply_documents = []
			item.blocked = bool(
				not item.get("issue_warehouse") or snapshot.get("can_calculate") is False
			)
			if item.blocked:
				item.available_qty = 0
				item.current_gap_qty = remaining_required
				item.shortage_qty = 0
				item.status = "cannot_calculate"
				if item.current_gap_qty > QTY_EPSILON:
					item.allocation_conflict = build_allocation_conflict(
						current_gap_qty=item.current_gap_qty,
						gap_without_priority_qty=item.current_gap_qty,
					)
				allocated_items.append(item)
				continue
			available = remaining_stock.setdefault(
				key,
				max(
					_loaded_work_order_stock_pool(
						snapshot,
						loaded_commitments.get(key, 0),
						loaded_plan_reservations.get(key, 0),
					)
					- reserved_stock.get(key, 0),
					0,
				),
			)
			reserved = min(_source_reserved_qty(item), remaining_required)
			allocated_free = min(max(remaining_required - reserved, 0), available)
			allocated = reserved + allocated_free
			remaining_stock[key] = max(available - allocated_free, 0)
			item.available_qty = allocated
			item.current_gap_qty = max(remaining_required - allocated, 0)
			# Arithmetic residue must not create a purchase or replenishment action.
			if item.current_gap_qty <= QTY_EPSILON:
				item.current_gap_qty = 0
			if item.current_gap_qty > QTY_EPSILON:
				higher_priority_allocations = [
					source
					for source in priority_allocations.get(key, [])
					if source.get("work_order") != work_order.get("name")
				]
				prior_allocated_qty = sum(
					flt(source.get("allocated_qty"))
					for source in higher_priority_allocations
				)
				gap_without_priority_qty = max(
					remaining_required
					- reserved
					- available
					- prior_allocated_qty,
					0,
				)
				item.allocation_conflict = build_allocation_conflict(
					current_gap_qty=item.current_gap_qty,
					gap_without_priority_qty=gap_without_priority_qty,
					other_hard_reserved_qty=max(
						reserved_stock.get(key, 0) - reserved,
						0,
					),
					prior_allocations=higher_priority_allocations,
				)
			if allocated_free > QTY_EPSILON:
				priority_allocations[key].append(
					{
						"work_order": work_order.get("name"),
						"sales_order": work_order.get("sales_order"),
						"delivery_date": work_order.get("order_delivery_date"),
						"allocated_qty": allocated_free,
					}
				)
			if item.supply_type == "manufactured":
				item.shortage_qty = 0
				item.status = "waiting_subassembly" if item.current_gap_qty > 0 else "ready_now"
				item.replenishment_required = bool(
					item.current_gap_qty > 0
					and item.completed_child_work_orders
					and not item.replenishment_in_progress
				)
			else:
				item.replenishment_required = False
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
				item.shortage_qty = uncovered if uncovered > QTY_EPSILON else 0
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
		for work_order in plan.work_orders_by_name.values():
			_apply_work_order_flow_state(work_order)
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


def _serialize_readiness_plan(plan, sales_order_item=None):
	work_order_names = [
		name
		for name in plan.get("execution_order") or []
		if not sales_order_item
		or plan.work_orders_by_name[name].get("sales_order_item") == sales_order_item
	]
	projected_work_orders = {
		name: plan.work_orders_by_name[name]
		for name in work_order_names
	}
	material_priority_dates = [
		str(work_order.get("order_delivery_date"))
		for work_order in projected_work_orders.values()
		if work_order.get("order_delivery_date")
	]
	projected_summary = (
		summarize_plan(frappe._dict(work_orders_by_name=projected_work_orders))
		if sales_order_item
		else plan.get("summary")
	)
	return {
		"name": plan.get("name"),
		"company": plan.get("company"),
		"planned_date": plan.get("planned_date"),
		"material_priority_date": min(material_priority_dates) if material_priority_dates else None,
		"posting_date": plan.get("posting_date"),
		"status": plan.get("status"),
		"summary": projected_summary,
		"work_orders": [
			plan.work_orders_by_name[name]
			for name in work_order_names
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
	work_orders = frappe.get_list(
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
			"from_wip_warehouse",
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
		limit=0,
	)
	if not work_orders:
		return {}

	plan_names = sorted({row.get("production_plan") for row in work_orders if row.get("production_plan")})
	work_order_names = [row.get("name") for row in work_orders]
	required_items = frappe.get_all(
		"Work Order Item",
		filters={"parent": ["in", work_order_names]},
		fields=[
			"name",
			"parent",
			"idx",
			"item_code",
			"item_name",
			"stock_uom",
			"source_warehouse",
			"required_qty",
			"transferred_qty",
			"returned_qty",
			"consumed_qty",
			"include_item_in_manufacturing",
			"stock_reserved_qty",
		],
	)
	attach_work_order_item_issue_warehouses(work_orders, required_items)
	stock_reservation_entries = frappe.get_all(
		"Stock Reservation Entry",
		filters={
			"docstatus": 1,
			"voucher_type": "Work Order",
			"voucher_no": ["in", work_order_names],
		},
		fields=[
			"voucher_no",
			"voucher_detail_no",
			"item_code",
			"warehouse",
			"reserved_qty",
			"delivered_qty",
			"transferred_qty",
			"consumed_qty",
		],
		limit=0,
	)
	attach_work_order_source_reservations(required_items, stock_reservation_entries)
	attach_work_order_stock_facts(work_orders, required_items)
	job_cards = frappe.get_all(
		"Job Card",
		filters={
			"work_order": ["in", work_order_names],
			"docstatus": ["<", 2],
			"is_corrective_job_card": 0,
		},
		fields=[
			"name",
			"work_order",
			"status",
			"docstatus",
			"creation",
		],
		limit=0,
	)
	work_order_operations = frappe.get_all(
		"Work Order Operation",
		filters={"parent": ["in", work_order_names]},
		fields=["name", "parent", "idx"],
		limit=0,
	)
	draft_stock_entries = frappe.get_all(
		"Stock Entry",
		filters={
			"work_order": ["in", work_order_names],
			"docstatus": 0,
			"is_return": 0,
			"purpose": ["in", [MANUFACTURE_PURPOSE, MATERIAL_ISSUE_PURPOSE]],
		},
		fields=["name", "work_order", "purpose", "docstatus", "creation"],
		limit=0,
	)
	attach_work_order_execution_facts(
		work_orders,
		job_cards,
		draft_stock_entries,
		work_order_operations,
	)
	attach_replenishment_work_orders(
		required_items,
		_load_active_replenishment_work_orders(work_order_names),
	)
	plans = frappe.get_list(
		"Production Plan",
		filters={"name": ["in", plan_names]},
		fields=["name", "company", "posting_date", "creation", "status"],
		limit=0,
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
			"qty",
			"required_qty",
			"wo_produced_qty",
			"fg_warehouse",
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
		frappe.get_list(
			"Sales Order",
			filters={"name": ["in", linked_order_names]},
			fields=["name", "creation"],
			limit=0,
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
		fields=["name", "item_name", "is_purchase_item"],
	)
	item_by_name = {row.get("name"): row for row in item_rows}
	attach_work_order_item_names(work_orders, item_by_name)
	for item in required_items:
		master_item = item_by_name.get(item.get("item_code")) or {}
		item.item_name = resolve_item_display_name(
			item.get("item_code"),
			master_item.get("item_name"),
			item.get("item_name"),
		)
		item.is_purchase_item = master_item.get("is_purchase_item")

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
		for graph_work_order in graph.work_orders_by_name.values():
			parent_item = item_by_name.get(graph_work_order.get("parent_item_code")) or {}
			graph_work_order.parent_item_name = parent_item.get("item_name")
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
				key = (
					item.get("item_code"),
					_work_order_item_issue_warehouse(work_order, item),
				)
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
			result[order_item_name].append(
				_serialize_readiness_plan(plan, sales_order_item=order_item_name)
			)
	return dict(result)
