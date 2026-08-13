from __future__ import annotations

from copy import deepcopy

import frappe
from frappe.utils import flt, getdate, now_datetime

from process_simplification.api.setup import get_default_bom
from process_simplification.api.shortage import (
	MaterialCoverageBomExpansionError,
	calculate_material_coverage,
)
from process_simplification.api.workbench import (
	_delivery_timing,
	calculate_production_quantities,
	get_fulfillment_overview,
	get_work_orders,
)


STATUS_LABELS = {
	"master_data_blocked": "基础资料异常",
	"unplanned": "待安排",
	"material_shortage": "缺料",
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
		"material_shortage",
		"in_production",
		"partially_completed",
	}:
		return "orange", 80, "临近交期"
	if status_code == "material_shortage":
		return "orange", 75, "原料短缺"
	if status_code == "unplanned":
		return "orange", 70, "生产未安排"
	if status_code == "overplanned":
		return "orange", 65, "生产超计划"
	if status_code in {"in_production", "partially_completed"}:
		return "blue", 50, STATUS_LABELS[status_code]
	if status_code == "awaiting_order_reservation":
		return "blue", 40, "完工待回补"
	return "green", 20, "计划已覆盖"


def production_sort_key(demand):
	# Missing delivery dates are an explicit master-data risk and must not disappear at the end.
	delivery_value = demand.get("delivery_date") or "0000-00-00"
	return (
		delivery_value,
		-flt(demand.get("risk_score") or 0),
		-flt(demand.get("unplanned_production_qty") or 0),
		str(demand.get("creation") or ""),
		str(demand.get("demand_key") or ""),
	)


def material_priority_sort_key(demand):
	return (
		not bool(demand.get("delivery_date")),
		demand.get("delivery_date") or "9999-12-31",
		str(demand.get("creation") or ""),
		str(demand.get("sales_order") or ""),
		str(demand.get("sales_order_item") or demand.get("demand_key") or ""),
	)


def allocate_finished_stock(rows):
	"""Allocate each item/warehouse free-stock snapshot once, in delivery priority order."""
	ordered = [frappe._dict(deepcopy(dict(row))) for row in rows or []]
	ordered.sort(
		key=lambda row: (
			row.get("delivery_date") or "0000-00-00",
			str(row.get("sales_order") or ""),
			str(row.get("sales_order_item") or ""),
		)
	)
	pools = {}
	for row in ordered:
		key = (row.get("item_code"), row.get("warehouse"))
		pools[key] = max(pools.get(key, 0), _positive(row, "available_to_reserve"))

	for row in ordered:
		key = (row.get("item_code"), row.get("warehouse"))
		production = calculate_production_quantities(
			pending_qty=_positive(row, "pending_qty"),
			reserved_qty=_positive(row, "reserved_qty"),
			available_to_reserve=pools.get(key, 0),
			active_work_order_qty=_positive(row, "active_work_order_qty"),
		)
		pools[key] = max(pools.get(key, 0) - production.available_to_reserve, 0)
		row.update(
			{
				"reserved_qty": production.reserved_qty,
				"available_to_reserve": production.available_to_reserve,
				"finished_stock_coverage_qty": production.finished_stock_coverage_qty,
				"production_required_qty": production.production_required_qty,
				"unplanned_production_qty": production.unplanned_production_qty,
				"overplanned_qty": production.overplanned_qty,
				"uncovered_qty": production.unplanned_production_qty,
			}
		)
	return ordered


def _allocated_rows_from_fulfillment(fulfillment):
	rows = []
	for order in (fulfillment or {}).get("orders") or []:
		for source_row in order.get("rows") or []:
			row = frappe._dict(deepcopy(dict(source_row)))
			row.sales_order = row.get("sales_order") or order.get("name")
			row.delivery_date = row.get("delivery_date") or order.get("delivery_date")
			rows.append(row)
	return allocate_finished_stock(rows)


def get_allocated_production_row(sales_order: str, sales_order_item: str):
	"""Return the delivery-priority finished-stock allocation used for safe Work Order creation."""
	for row in _allocated_rows_from_fulfillment(get_fulfillment_overview()):
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
	started = any(
		wo.get("status") not in {"Submitted", "Not Started"} or _positive(wo, "produced_qty") > 0
		for wo in work_orders
	)
	if row.get("unsupported") or (unplanned_production_qty > 0 and not has_bom):
		status_code = "master_data_blocked"
	elif unplanned_production_qty > 0:
		status_code = "unplanned"
	elif overplanned_qty > 0:
		status_code = "overplanned"
	elif completed_unreserved_qty > 0 and production_required_qty <= 0:
		status_code = "awaiting_order_reservation"
	elif _positive(row, "completed_qty") > 0 and production_required_qty > 0:
		status_code = "partially_completed"
	elif active_work_order_qty > 0 and started:
		status_code = "in_production"
	elif active_work_order_qty > 0:
		status_code = "ready_to_start"
	else:
		status_code = "awaiting_order_reservation"

	risk_level, risk_score, risk_label = _risk_for_demand(delivery_timing, status_code)
	actions = []
	if unplanned_production_qty > 0 and status_code != "master_data_blocked":
		_unique_action(actions, "创建生产任务", "create_work_order")
	_unique_action(actions, "检查物料", "check_materials")
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
		has_material_gap = any(_positive(row, "current_gap_qty") > 0 for row in materials)
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
		if has_material_gap and demand["status_code"] not in {
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


def attach_priority_material_coverage(demands, company):
	"""Attach per-demand material coverage after earlier demands consume shared stock."""
	ordered = [frappe._dict(deepcopy(dict(demand))) for demand in demands or []]
	ordered.sort(key=material_priority_sort_key)
	material_demands = [(demand, _material_demands([demand])) for demand in ordered]
	prior_demands = []
	coverage_by_demand = []
	remaining_supply = {}

	for demand, current_demands in material_demands:
		coverage = calculate_material_coverage(
			current_demands,
			company,
			need_by_date=demand.get("delivery_date"),
			prior_demands=prior_demands,
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
						key = (
							material.get("item_code"),
							material.get("warehouse"),
							document.get("doctype"),
							document.get("detail_name") or document.get("name"),
						)
						available_qty = remaining_supply.setdefault(key, _positive(document, "outstanding_qty"))
						allocated_qty = min(gap_qty, available_qty)
						remaining_supply[key] = available_qty - allocated_qty
						gap_qty -= allocated_qty
					document["allocated_qty"] = allocated_qty
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
		prior_demands.extend(current_demands)

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


def _delivery_precedes(other_date, target_date) -> bool:
	"""Delivery-date priority (口径 2): a demand with no date is treated as most
	urgent, so it always precedes; a dated demand precedes a strictly later one."""
	if not other_date:
		return True
	if not target_date:
		return False
	return getdate(other_date) < getdate(target_date)


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
	demands = []
	for demand in get_production_overview().get("demands") or []:
		if demand.get("company") != company:
			continue
		if exclude_sales_order_item and demand.get("sales_order_item") == exclude_sales_order_item:
			continue
		if not _delivery_precedes(demand.get("delivery_date"), target_delivery_date):
			continue
		demands.append(demand)
	return _material_demands(demands)


def get_all_material_demands(company: str):
	"""All open production demands for the company, in the ``{bom_no, qty, source}``
	shape for material coverage. Used to aggregate raw-material need across every
	unfinished order so one Material Request can cover the combined quantity."""
	demands = [
		demand
		for demand in get_production_overview().get("demands") or []
		if demand.get("company") == company
	]
	return _material_demands(demands)


def _other_work_orders():
	rows = frappe.get_all(
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
	)
	return [dict(row) for row in rows if not row.get("sales_order") or not row.get("sales_order_item")]


@frappe.whitelist()
def get_production_overview():
	frappe.has_permission("Sales Order", "read", throw=True)
	frappe.has_permission("Work Order", "read", throw=True)
	checked_at = now_datetime()
	fulfillment = get_fulfillment_overview()
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
		if not _material_demands(company_demands) or not company:
			covered_demands.extend(company_demands)
			continue
		try:
			covered_demands.extend(attach_priority_material_coverage(company_demands, company))
		except MaterialCoverageBomExpansionError:
			for demand in company_demands:
				demand["status_code"] = "master_data_blocked"
				demand["status_label"] = STATUS_LABELS["master_data_blocked"]
				demand["material_summary"]["status_code"] = "blocked"
			covered_demands.extend(company_demands)

	covered_demands.sort(key=production_sort_key)
	return {
		"checked_at": checked_at,
		"summary": {
			"total_demands": len(covered_demands),
			"unplanned_demands": sum(row["unplanned_production_qty"] > 0 for row in covered_demands),
			"overdue_demands": sum(row["delivery_timing"] == "overdue" for row in covered_demands),
			"due_within_7_days": sum(
				row["delivery_timing"] in {"today", "within_7_days"} for row in covered_demands
			),
			"material_shortage_demands": sum(
				row["material_summary"]["shortage_item_count"] > 0 for row in covered_demands
			),
			"in_production_demands": sum(
				row["status_code"] in {"in_production", "partially_completed"} for row in covered_demands
			),
			"awaiting_order_reservation_demands": sum(
				row["status_code"] == "awaiting_order_reservation" for row in covered_demands
			),
		},
		"demands": covered_demands,
		"other_work_orders": _other_work_orders(),
	}


def get_production_demand(sales_order: str, sales_order_item: str):
	for demand in get_production_overview().get("demands") or []:
		if demand.get("sales_order") == sales_order and demand.get("sales_order_item") == sales_order_item:
			return frappe._dict(demand)
	return None
