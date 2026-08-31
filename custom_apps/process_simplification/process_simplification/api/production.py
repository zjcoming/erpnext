from __future__ import annotations

from copy import deepcopy

import frappe
from frappe.utils import flt, getdate, now_datetime

from process_simplification.api.production_readiness import get_production_plan_readiness
from process_simplification.api.setup import get_default_bom
from process_simplification.api.shortage import (
	calculate_material_coverage,
)
from process_simplification.api.workbench import (
	DEFAULT_WORKBENCH_PAGE_SIZE,
	_delivery_timing,
	get_fulfillment_overview,
	get_work_orders,
	order_item_priority_key,
	paginate_workbench_rows,
)
from process_simplification.management_access import (
	CAPABILITY_PRODUCTION_REVIEW,
	user_has_capability,
)


STATUS_LABELS = {
	"master_data_blocked": "基础资料异常",
	"unplanned": "待安排",
	"planning_required": "待创建生产计划",
	"legacy_work_order": "旧工单未纳入计划",
	"material_shortage": "缺料",
	"awaiting_supply": "等待到料",
	"waiting_subassembly": "等待半成品",
	"ready_to_start": "可开工",
	"in_production": "生产中",
	"partially_completed": "部分完工",
	"awaiting_order_reservation": "待回补订单",
	"overplanned": "超计划生产",
}


def _positive(row, fieldname: str) -> float:
	return max(flt(row.get(fieldname) or 0), 0)


def _unique_action(actions, label: str, action_name: str):
	if any(row.get("action") == action_name for row in actions):
		return
	actions.append({"label": label, "action": action_name, "enabled": True, "reason": None})


def _row_delivery_date(row, order):
	return row.get("delivery_date") or order.get("delivery_date")


def _risk_for_demand(delivery_timing: str, status_code: str):
	if delivery_timing == "overdue":
		return "red", 100, "已逾期"
	if delivery_timing == "missing":
		return "red", 95, "缺少交期"
	if status_code == "master_data_blocked":
		return "red", 90, "生产资料阻塞"
	if delivery_timing in {"today", "within_7_days"} and status_code in {
		"unplanned",
		"planning_required",
		"legacy_work_order",
		"material_shortage",
		"awaiting_supply",
		"waiting_subassembly",
		"in_production",
		"partially_completed",
	}:
		return "orange", 80, "临近交期"
	if status_code == "material_shortage":
		return "orange", 75, "原料短缺"
	if status_code == "awaiting_supply":
		return "blue", 55, "采购在途"
	if status_code == "waiting_subassembly":
		return "blue", 50, "等待下级生产"
	if status_code == "unplanned":
		return "orange", 70, "生产未安排"
	if status_code == "planning_required":
		return "orange", 70, "尚未创建生产计划"
	if status_code == "legacy_work_order":
		return "red", 85, "旧工单未纳入计划"
	if status_code == "overplanned":
		return "orange", 65, "生产超计划"
	if status_code in {"in_production", "partially_completed"}:
		return "blue", 50, STATUS_LABELS[status_code]
	if status_code == "awaiting_order_reservation":
		return "blue", 40, "完工待回补"
	return "green", 20, "计划已覆盖"


def production_sort_key(demand):
	return order_item_priority_key(demand)


def material_priority_sort_key(demand):
	return order_item_priority_key(demand)


def _allocated_rows_from_fulfillment(fulfillment):
	rows = []
	for order in (fulfillment or {}).get("orders") or []:
		for source_row in order.get("rows") or []:
			row = frappe._dict(deepcopy(dict(source_row)))
			row.sales_order = row.get("sales_order") or order.get("name")
			row.company = row.get("company") or order.get("company")
			row.delivery_date = row.get("delivery_date") or order.get("delivery_date")
			row.order_creation = row.get("order_creation") or order.get("creation")
			rows.append(row)
	return rows


def get_allocated_production_row(sales_order: str, sales_order_item: str):
	"""Return the delivery-priority finished-stock allocation used for safe Work Order creation."""
	for row in _allocated_rows_from_fulfillment(get_fulfillment_overview(page_size=0)):
		if row.get("sales_order") == sales_order and row.get("sales_order_item") == sales_order_item:
			return row
	return None


def build_production_demand(order, row, work_orders=None, today=None):
	order = frappe._dict(order or {})
	row = frappe._dict(row or {})
	work_orders = [frappe._dict(wo) for wo in work_orders or []]
	production_required_qty = _positive(row, "production_required_qty")
	active_work_order_qty = _positive(row, "active_work_order_qty")
	unplanned_production_qty = _positive(row, "unplanned_production_qty")
	overplanned_qty = _positive(row, "overplanned_qty")
	completed_unreserved_qty = _positive(row, "completed_unreserved_qty")
	if not any(
		(
			production_required_qty,
			active_work_order_qty,
			unplanned_production_qty,
			overplanned_qty,
			completed_unreserved_qty,
		)
	):
		return None

	delivery_date = _row_delivery_date(row, order)
	delivery_timing, days_to_delivery = _delivery_timing(
		getdate(delivery_date) if delivery_date else None,
		getdate(today or now_datetime()),
	)
	has_bom = row.get("material_status") != "不涉及生产"
	if row.get("unsupported") or (unplanned_production_qty > 0 and not has_bom):
		status_code = "master_data_blocked"
	elif unplanned_production_qty > 0:
		status_code = "planning_required"
	elif overplanned_qty > 0:
		status_code = "overplanned"
	elif completed_unreserved_qty > 0 and production_required_qty <= 0:
		status_code = "awaiting_order_reservation"
	elif _positive(row, "completed_qty") > 0 and production_required_qty > 0:
		status_code = "partially_completed"
	elif active_work_order_qty > 0:
		status_code = "legacy_work_order"
	else:
		status_code = "awaiting_order_reservation"

	risk_level, risk_score, risk_label = _risk_for_demand(delivery_timing, status_code)
	actions = []
	if unplanned_production_qty > 0 and status_code != "master_data_blocked":
		_unique_action(actions, "创建生产计划", "create_work_order")
	if completed_unreserved_qty > 0:
		_unique_action(actions, "回补订单", "reserve_completed_stock")
	_unique_action(actions, "查看销售订单", "view_sales_order")

	return {
		"demand_key": row.get("sales_order_item"),
		"sales_order": row.get("sales_order") or order.get("name"),
		"sales_order_item": row.get("sales_order_item"),
		"customer": order.get("customer") or row.get("customer"),
		"customer_name": order.get("customer_name") or row.get("customer"),
		"company": order.get("company"),
		"creation": str(order.get("creation") or ""),
		"order_creation": str(row.get("order_creation") or order.get("creation") or ""),
		"sales_order_item_idx": row.get("sales_order_item_idx"),
		"item_code": row.get("item_code"),
		"item_name": row.get("item_name"),
		"warehouse": row.get("warehouse"),
		"delivery_date": str(delivery_date) if delivery_date else None,
		"delivery_timing": delivery_timing,
		"days_to_delivery": days_to_delivery,
		"pending_qty": _positive(row, "pending_qty"),
		"reserved_qty": _positive(row, "reserved_qty"),
		"available_to_reserve": _positive(row, "available_to_reserve"),
		"finished_stock_coverage_qty": _positive(row, "finished_stock_coverage_qty"),
		"production_required_qty": production_required_qty,
		"active_work_order_qty": active_work_order_qty,
		"unplanned_production_qty": unplanned_production_qty,
		"overplanned_qty": overplanned_qty,
		"completed_qty": _positive(row, "completed_qty"),
		"completed_unreserved_qty": completed_unreserved_qty,
		"status_code": status_code,
		"status_label": STATUS_LABELS[status_code],
		"risk_level": risk_level,
		"risk_score": risk_score,
		"risk_label": risk_label,
		"material_summary": {
			"status_code": "not_checked",
			"material_count": 0,
			"shortage_item_count": 0,
			"blocked_item_count": 0,
			"awaiting_supply_item_count": 0,
		},
		"materials": [],
		"work_orders": [dict(wo) for wo in work_orders],
		"next_actions": actions,
	}


def attach_material_coverage(demands, coverage):
	result = [deepcopy(dict(demand)) for demand in demands or []]
	by_key = {demand.get("demand_key"): demand for demand in result}
	for demand in result:
		demand["materials"] = []

	for material in (coverage or {}).get("materials") or []:
		material = dict(material)
		sources = [dict(source) for source in material.pop("sources", [])]
		is_shared = len({source.get("demand_key") for source in sources if source.get("demand_key")}) > 1
		for source in sources:
			demand = by_key.get(source.get("demand_key") or source.get("sales_order_item"))
			if not demand:
				continue
			demand["materials"].append(
				{
					**deepcopy(material),
					"source_required_qty": flt(source.get("required_qty") or 0),
					"total_required_qty": flt(material.get("required_qty") or 0),
					"is_shared": is_shared,
					"source_count": len(sources),
				}
			)

	for demand in result:
		materials = demand["materials"]
		shortages = [
			row
			for row in materials
			if row.get("status") == "new_purchase_required" and _positive(row, "shortage_qty") > 0
		]
		blocked = [row for row in materials if row.get("blocked")]
		awaiting = [
			row
			for row in materials
			if row.get("status") in {"awaiting_purchase_receipt", "purchase_request_pending"}
		]
		has_material_gap = bool(blocked) or any(_positive(row, "current_gap_qty") > 0 for row in materials)
		demand["material_summary"] = {
			"status_code": "blocked"
			if blocked
			else "shortage"
			if shortages
			else "awaiting_supply"
			if awaiting
			else "ready",
			"material_count": len(materials),
			"shortage_item_count": len(shortages),
			"blocked_item_count": len(blocked),
			"awaiting_supply_item_count": len(awaiting),
		}
		if shortages:
			_unique_action(demand["next_actions"], "处理缺料", "handle_shortage")
		if blocked and demand["status_code"] not in {"in_production", "partially_completed"}:
			demand["status_code"] = "master_data_blocked"
			demand["status_label"] = STATUS_LABELS["master_data_blocked"]
			risk = _risk_for_demand(demand["delivery_timing"], demand["status_code"])
			demand["risk_level"], demand["risk_score"], demand["risk_label"] = risk
		elif has_material_gap and demand["status_code"] not in {
			"master_data_blocked",
			"unplanned",
			"in_production",
			"partially_completed",
		}:
			demand["status_code"] = "material_shortage"
			demand["status_label"] = STATUS_LABELS["material_shortage"]
			risk = _risk_for_demand(demand["delivery_timing"], demand["status_code"])
			demand["risk_level"], demand["risk_score"], demand["risk_label"] = risk
		demand["materials"].sort(key=lambda row: (row.get("warehouse") or "", row.get("item_code") or ""))
	return result


def attach_production_plan_readiness(demands, readiness_by_sales_order_item):
	"""Attach direct Work Order material state without exploding finished-good BOMs."""
	result = [frappe._dict(deepcopy(dict(demand))) for demand in demands or []]
	for demand in result:
		plans = readiness_by_sales_order_item.get(demand.get("sales_order_item")) or []
		if not plans:
			continue
		demand.production_plans = deepcopy(plans)
		demand.work_orders = [
			deepcopy(work_order)
			for plan in plans
			for work_order in plan.get("work_orders") or []
		]
		demand.materials = [
			{
				**deepcopy(dict(item)),
				"work_order": work_order.get("name"),
				"production_item": work_order.get("production_item"),
				"production_item_name": work_order.get("production_item_name"),
				"production_plan": plan.get("name"),
				"planned_date": plan.get("planned_date"),
			}
			for plan in plans
			for work_order in plan.get("work_orders") or []
			for item in work_order.get("required_items") or []
		]

		statuses = {row.get("readiness_status") for row in demand.work_orders}
		purchased_shortages = [
			row
			for row in demand.materials
			if row.get("supply_type") == "purchased"
			and row.get("status") == "new_purchase_required"
			and _positive(row, "shortage_qty") > 0
		]
		awaiting_supply = [
			row
			for row in demand.materials
			if row.get("status") in {"awaiting_purchase_receipt", "purchase_request_pending"}
		]
		blocked = [
			row for row in demand.work_orders if row.get("readiness_status") in {"blocked", "production_task_missing"}
		]
		demand.material_summary = {
			"status_code": "blocked"
			if blocked
			else "shortage"
			if purchased_shortages
			else "awaiting_supply"
			if awaiting_supply
			else "waiting_subassembly"
			if "waiting_subassembly" in statuses and "ready_now" not in statuses
			else "ready",
			"material_count": len(demand.materials),
			"shortage_item_count": len(purchased_shortages),
			"blocked_item_count": len(blocked),
			"awaiting_supply_item_count": len(awaiting_supply),
		}

		if demand.get("status_code") not in {"in_production", "partially_completed"}:
			if "in_progress" in statuses:
				demand.status_code = "in_production"
			elif "ready_now" in statuses:
				demand.status_code = "ready_to_start"
			elif purchased_shortages:
				demand.status_code = "material_shortage"
			elif awaiting_supply:
				demand.status_code = "awaiting_supply"
			elif blocked:
				demand.status_code = "master_data_blocked"
			elif "waiting_subassembly" in statuses:
				demand.status_code = "waiting_subassembly"
		demand.status_label = STATUS_LABELS[demand.status_code]
		risk = _risk_for_demand(demand.delivery_timing, demand.status_code)
		demand.risk_level, demand.risk_score, demand.risk_label = risk
		if purchased_shortages:
			_unique_action(demand.next_actions, "处理缺料", "handle_shortage")
		_unique_action(demand.next_actions, "检查工单物料", "check_materials")
	return result


def attach_priority_material_coverage(demands, company):
	"""Attach per-demand material coverage after earlier demands consume shared stock."""
	ordered = [frappe._dict(deepcopy(dict(demand))) for demand in demands or []]
	ordered.sort(key=material_priority_sort_key)
	material_demands = [(demand, _material_demands([demand])) for demand in ordered]
	prior_consumed = {}
	coverage_by_demand = []
	remaining_supply = {}
	fact_cache = {}

	for demand, current_demands in material_demands:
		coverage = calculate_material_coverage(
			current_demands,
			company,
			need_by_date=demand.get("delivery_date"),
			prior_consumed=prior_consumed,
			fact_cache=fact_cache,
		)
		for material in coverage.get("materials") or []:
			gap_qty = _positive(material, "current_gap_qty")
			allocated_purchase_orders = 0
			allocated_material_requests = 0
			documents = [dict(document) for document in material.get("supply_documents") or []]
			for doctype in ("Purchase Order", "Material Request"):
				for document in documents:
					if document.get("doctype") != doctype:
						continue
					allocated_qty = 0
					if not document.get("is_late"):
						detail_name = document.get("detail_name")
						key = (
							material.get("item_code"),
							material.get("warehouse"),
							document.get("doctype"),
							detail_name or document.get("name"),
						)
						available_qty = remaining_supply.setdefault(key, _positive(document, "outstanding_qty"))
						allocated_qty = min(gap_qty, available_qty)
						remaining_supply[key] = available_qty - allocated_qty
						gap_qty -= allocated_qty
					document["allocated_qty"] = allocated_qty
					document.pop("detail_name", None)
					if doctype == "Purchase Order":
						allocated_purchase_orders += allocated_qty
					else:
						allocated_material_requests += allocated_qty
			material["supply_documents"] = documents
			material["open_purchase_order_qty"] = allocated_purchase_orders
			material["open_material_request_qty"] = allocated_material_requests
			material["shortage_qty"] = gap_qty
			if material.get("blocked"):
				continue
			if material["current_gap_qty"] == 0:
				material["status"] = "ready_now"
			elif allocated_purchase_orders >= material["current_gap_qty"]:
				material["status"] = "awaiting_purchase_receipt"
			elif allocated_purchase_orders + allocated_material_requests >= material["current_gap_qty"]:
				material["status"] = "purchase_request_pending"
			else:
				material["status"] = "new_purchase_required"
		coverage_by_demand.append((demand, coverage))
		for material in coverage.get("materials") or []:
			key = (material.get("item_code"), material.get("warehouse"))
			prior_consumed[key] = prior_consumed.get(key, 0) + _positive(material, "required_qty")

	totals = {}
	for _demand, coverage in coverage_by_demand:
		for material in coverage.get("materials") or []:
			key = (material.get("item_code"), material.get("warehouse"))
			total = totals.setdefault(key, {"required_qty": 0, "sources": set()})
			total["required_qty"] += _positive(material, "required_qty")
			for source in material.get("sources") or []:
				source_key = source.get("demand_key") or source.get("sales_order_item")
				if source_key:
					total["sources"].add(source_key)

	result = []
	for demand, coverage in coverage_by_demand:
		attached_demand = attach_material_coverage([demand], coverage)[0]
		for material in attached_demand["materials"]:
			total = totals[(material.get("item_code"), material.get("warehouse"))]
			material["total_required_qty"] = total["required_qty"]
			material["source_count"] = len(total["sources"])
			material["is_shared"] = material["source_count"] > 1
		result.append(attached_demand)
	return result


def _material_demands(demands):
	rows = []
	for demand in demands:
		qty = _positive(demand, "production_required_qty")
		bom_no = get_default_bom(demand.get("item_code")) if qty > 0 else None
		if not bom_no:
			continue
		rows.append(
			{
				"bom_no": bom_no,
				"qty": qty,
				"source": {
					"demand_key": demand.get("demand_key"),
					"sales_order": demand.get("sales_order"),
					"sales_order_item": demand.get("sales_order_item"),
					"finished_item": demand.get("item_code"),
					"delivery_date": demand.get("delivery_date"),
					"sales_order_item_warehouse": demand.get("warehouse"),
				},
			}
		)
	return rows


def get_prior_material_demands(
	company: str,
	*,
	target_delivery_date=None,
	exclude_sales_order_item: str | None = None,
):
	"""Production demands that must consume shared raw material before a target,
	in delivery-date priority order.

	Returned in the ``{bom_no, qty, source}`` shape expected by
	``calculate_material_coverage(prior_demands=...)``. Used so a per-order
	shortage check nets shared stock already claimed by earlier-due orders
	instead of letting every order claim the same scarce stock.
	"""
	# A quick-order target has not been inserted yet, so every existing demand
	# with the same delivery date precedes it by creation time.
	target_priority = order_item_priority_key(
		{
			"delivery_date": target_delivery_date,
			"order_creation": "9999-12-31 23:59:59.999999",
			"sales_order": "\uffff",
			"sales_order_item_idx": 2**31 - 1,
			"sales_order_item": "\uffff",
		}
	)
	demands = []
	for demand in get_production_overview(page_size=0).get("demands") or []:
		if demand.get("company") != company:
			continue
		if exclude_sales_order_item and demand.get("sales_order_item") == exclude_sales_order_item:
			continue
		if order_item_priority_key(demand) >= target_priority:
			continue
		demands.append(demand)
	return _material_demands(demands)


def get_prior_finished_stock_allocations(
	company: str,
	*,
	target_delivery_date=None,
):
	"""Finished stock already allocated to open orders ahead of a new quick order.

	The fulfillment workbench owns the delivery-priority allocation. Reusing its
	rows here prevents a later quick-order preview from claiming the same physical
	stock snapshot a second time.
	"""
	target_priority = order_item_priority_key(
		{
			"delivery_date": target_delivery_date,
			"order_creation": "9999-12-31 23:59:59.999999",
			"sales_order": "\uffff",
			"sales_order_item_idx": 2**31 - 1,
			"sales_order_item": "\uffff",
		}
	)
	allocations = {}
	for row in _allocated_rows_from_fulfillment(get_fulfillment_overview(page_size=0)):
		if row.get("company") != company or order_item_priority_key(row) >= target_priority:
			continue
		key = (row.get("item_code"), row.get("warehouse"))
		allocations[key] = allocations.get(key, 0) + _positive(row, "available_to_reserve")
	return allocations


def get_all_material_demands(company: str):
	"""All open production demands for the company, in the ``{bom_no, qty, source}``
	shape for material coverage. Used to aggregate raw-material need across every
	unfinished order so one Material Request can cover the combined quantity."""
	demands = [
		demand
		for demand in get_production_overview(page_size=0).get("demands") or []
		if demand.get("company") == company
	]
	return _material_demands(demands)


def _other_work_orders():
	rows = frappe.get_list(
		"Work Order",
		filters={"docstatus": 1, "status": ["not in", ["Completed", "Stopped", "Closed", "Cancelled"]]},
		fields=[
			"name",
			"production_item",
			"qty",
			"produced_qty",
			"status",
			"sales_order",
			"sales_order_item",
			"planned_start_date",
			"expected_delivery_date",
		],
		order_by="expected_delivery_date asc, creation asc",
		limit=0,
	)
	other_rows = [dict(row) for row in rows if not row.get("sales_order") or not row.get("sales_order_item")]
	item_codes = sorted({row.get("production_item") for row in other_rows if row.get("production_item")})
	item_names = {
		row.get("name"): row.get("item_name")
		for row in frappe.get_all(
			"Item",
			filters={"name": ["in", item_codes]},
			fields=["name", "item_name"],
		)
	} if item_codes else {}
	for row in other_rows:
		row["production_item_name"] = item_names.get(row.get("production_item"))
	return other_rows


def is_production_due_within_7_days(demand):
	return demand.get("delivery_timing") in {"today", "within_7_days"}


def filter_production_demands(demands, filters=None):
	filters = frappe.parse_json(filters) if isinstance(filters, str) else filters
	filters = frappe._dict(filters or {})
	search = str(filters.get("search") or "").strip().lower()

	def matches(demand):
		work_order_values = [
			value
			for work_order in demand.get("work_orders") or []
			for value in [
				work_order.get("name"),
				work_order.get("production_item"),
				work_order.get("production_item_name"),
				*[
					identity
					for item in work_order.get("required_items") or []
					for identity in [item.get("item_code"), item.get("item_name")]
				],
			]
		]
		searchable = " ".join(
			str(value or "")
			for value in [
				demand.get("demand_key"),
				demand.get("sales_order"),
				demand.get("customer"),
				demand.get("customer_name"),
				demand.get("item_code"),
				demand.get("item_name"),
				*work_order_values,
			]
		).lower()
		delivery_window = filters.get("deliveryWindow")
		if delivery_window == "within_7_days":
			delivery_matches = is_production_due_within_7_days(demand)
		else:
			delivery_matches = not delivery_window or demand.get("delivery_timing") == delivery_window
		return (
			(not search or search in searchable)
			and (not filters.get("customer") or demand.get("customer") == filters.get("customer"))
			and delivery_matches
			and (not filters.get("status") or demand.get("status_code") == filters.get("status"))
			and (not filters.get("risk") or demand.get("risk_level") == filters.get("risk"))
			and (
				not filters.get("shortageOnly")
				or flt(demand.get("material_summary", {}).get("shortage_item_count") or 0) > 0
			)
			and (not filters.get("unplannedOnly") or flt(demand.get("unplanned_production_qty") or 0) > 0)
		)

	return [demand for demand in demands or [] if matches(demand)]


def production_overview_summary(demands):
	return {
		"total_demands": len(demands or []),
		"unplanned_demands": sum(row["unplanned_production_qty"] > 0 for row in demands or []),
		"overdue_demands": sum(row["delivery_timing"] == "overdue" for row in demands or []),
		"due_within_7_days": sum(is_production_due_within_7_days(row) for row in demands or []),
		"material_shortage_demands": sum(
			row["material_summary"]["shortage_item_count"] > 0 for row in demands or []
		),
		"in_production_demands": sum(
			row["status_code"] in {"in_production", "partially_completed"} for row in demands or []
		),
		"awaiting_order_reservation_demands": sum(
			row["status_code"] == "awaiting_order_reservation" for row in demands or []
		),
	}


def production_customers(demands):
	customers = {}
	for demand in demands or []:
		if demand.get("customer"):
			customers[demand.get("customer")] = demand.get("customer_name") or demand.get("customer")
	return [{"value": value, "label": label} for value, label in sorted(customers.items(), key=lambda row: row[1])]


def attach_visible_worker_assignment_counts(demands):
	"""Attach permission-filtered assignment history counts to visible Work Orders."""
	if not user_has_capability(CAPABILITY_PRODUCTION_REVIEW):
		return demands
	work_order_names = sorted(
		{
			work_order.get("name")
			for demand in demands or []
			for work_order in demand.get("work_orders") or []
			if work_order.get("name")
		}
	)
	if not work_order_names:
		return demands
	counts = {}
	for row in frappe.get_list(
		"Job Card Worker Assignment",
		filters={"work_order": ["in", work_order_names]},
		fields=["work_order"],
		limit=0,
	):
		counts[row.work_order] = counts.get(row.work_order, 0) + 1
	for demand in demands or []:
		for work_order in demand.get("work_orders") or []:
			work_order["worker_assignment_history_count"] = counts.get(work_order.get("name"), 0)
	return demands


@frappe.whitelist()
def get_production_overview(page=1, page_size=DEFAULT_WORKBENCH_PAGE_SIZE, filters=None):
	frappe.has_permission("Sales Order", "read", throw=True)
	frappe.has_permission("Work Order", "read", throw=True)
	checked_at = now_datetime()
	fulfillment = get_fulfillment_overview(page_size=0)
	orders = fulfillment.get("orders") or []
	order_by_name = {order.get("name"): frappe._dict(order) for order in orders}
	allocated_rows = _allocated_rows_from_fulfillment(fulfillment)

	demands = []
	for row in allocated_rows:
		order = order_by_name.get(row.get("sales_order"), frappe._dict())
		work_orders = get_work_orders(row.get("sales_order"), row.get("sales_order_item"), row.get("item_code"))
		demand = build_production_demand(order, row, work_orders, today=checked_at)
		if demand:
			demands.append(demand)

	by_company = {}
	for demand in demands:
		by_company.setdefault(demand.get("company"), []).append(demand)
	covered_demands = []
	for company, company_demands in by_company.items():
		readiness = (
			get_production_plan_readiness(
				company=company,
				sales_order_items=[row.get("sales_order_item") for row in company_demands],
			)
			if company
			else {}
		)
		planned_demands = [
			row for row in company_demands if readiness.get(row.get("sales_order_item"))
		]
		legacy_demands = [
			row for row in company_demands if not readiness.get(row.get("sales_order_item"))
		]
		covered_demands.extend(attach_production_plan_readiness(planned_demands, readiness))
		# A demand without a Production Plan has no authoritative direct Work Order
		# material requirements. It remains planning-required or legacy-blocked
		# until the plan graph provides executable Work Orders; once it does, the
		# resulting materials use the Sales Order Item delivery priority above.
		covered_demands.extend(legacy_demands)

	covered_demands.sort(key=production_sort_key)
	filtered_demands = filter_production_demands(covered_demands, filters)
	paged_demands, pagination = paginate_workbench_rows(filtered_demands, page=page, page_size=page_size)
	attach_visible_worker_assignment_counts(paged_demands)
	return {
		"checked_at": checked_at,
		"summary": production_overview_summary(filtered_demands),
		"pagination": pagination,
		"customers": production_customers(filtered_demands),
		"demands": paged_demands,
		"other_work_orders": _other_work_orders(),
	}


def get_production_demand(sales_order: str, sales_order_item: str):
	for demand in get_production_overview(page_size=0).get("demands") or []:
		if demand.get("sales_order") == sales_order and demand.get("sales_order_item") == sales_order_item:
			return frappe._dict(demand)
	return None
