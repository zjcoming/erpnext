from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from math import ceil

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime

from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
	get_available_qty_to_reserve,
)

from process_simplification.api.setup import get_default_bom
from process_simplification.api.utils import (
	ACTIVE_WORK_ORDER_STATUSES,
	TERMINAL_WORK_ORDER_STATUSES,
	WorkbenchRow,
	action,
	delivered_stock_qty,
	ensure_submitted_sales_order,
	item_stock_qty,
	pending_delivery_qty,
	remaining_qty,
	row_to_dict,
)


@dataclass(frozen=True)
class ProductionQuantities:
	reserved_qty: float
	available_to_reserve: float
	finished_stock_coverage_qty: float
	production_required_qty: float
	unplanned_production_qty: float
	overplanned_qty: float


def calculate_production_quantities(
	pending_qty: float,
	reserved_qty: float,
	available_to_reserve: float,
	active_work_order_qty: float,
) -> ProductionQuantities:
	pending_qty = max(flt(pending_qty), 0)
	reserved_qty = min(max(flt(reserved_qty), 0), pending_qty)
	available_to_reserve = min(max(flt(available_to_reserve), 0), pending_qty - reserved_qty)
	finished_stock_coverage_qty = reserved_qty + available_to_reserve
	production_required_qty = max(pending_qty - finished_stock_coverage_qty, 0)
	active_work_order_qty = max(flt(active_work_order_qty), 0)
	return ProductionQuantities(
		reserved_qty=reserved_qty,
		available_to_reserve=available_to_reserve,
		finished_stock_coverage_qty=finished_stock_coverage_qty,
		production_required_qty=production_required_qty,
		unplanned_production_qty=max(production_required_qty - active_work_order_qty, 0),
		overplanned_qty=max(active_work_order_qty - production_required_qty, 0),
	)


def _remaining_reserved_qty(sre) -> float:
	return max(
		flt(sre.reserved_qty)
		- flt(sre.delivered_qty)
		- flt(sre.transferred_qty)
		- flt(sre.consumed_qty),
		0,
	)


def get_effective_reserved_qty(sales_order: str, sales_order_item: str) -> float:
	entries = frappe.get_all(
		"Stock Reservation Entry",
		filters={
			"docstatus": 1,
			"voucher_type": "Sales Order",
			"voucher_no": sales_order,
			"voucher_detail_no": sales_order_item,
		},
		fields=["reserved_qty", "delivered_qty", "transferred_qty", "consumed_qty"],
	)
	return sum(_remaining_reserved_qty(entry) for entry in entries)


def get_work_orders(sales_order: str, sales_order_item: str, item_code: str | None = None):
	filters = {"sales_order": sales_order, "sales_order_item": sales_order_item, "docstatus": 1}
	if item_code:
		filters["production_item"] = item_code

	return frappe.get_all(
		"Work Order",
		filters=filters,
		fields=[
			"name",
			"production_item",
			"qty",
			"produced_qty",
			"process_loss_qty",
			"material_transferred_for_manufacturing",
			"status",
			"bom_no",
			"source_warehouse",
			"wip_warehouse",
			"fg_warehouse",
			"planned_start_date",
			"expected_delivery_date",
		],
		order_by="creation asc",
	)


def get_active_work_order_qty(work_orders) -> float:
	total = 0
	for wo in work_orders:
		if wo.status in TERMINAL_WORK_ORDER_STATUSES:
			continue
		total += remaining_qty(wo.qty, wo.produced_qty, wo.process_loss_qty)
	return total


def get_completed_qty(work_orders) -> float:
	return sum(flt(wo.produced_qty) for wo in work_orders)


def get_manufacture_stock_entries(work_orders):
	names = [wo.name for wo in work_orders]
	if not names:
		return []
	return frappe.get_all(
		"Stock Entry",
		filters={"docstatus": 1, "purpose": "Manufacture", "work_order": ["in", names]},
		fields=["name", "work_order", "fg_completed_qty"],
	)


def get_completed_reserved_qty(sales_order_item: str, manufacture_entries) -> float:
	entry_names = [entry.name for entry in manufacture_entries]
	if not entry_names:
		return 0

	reservations = frappe.get_all(
		"Stock Reservation Entry",
		filters={
			"docstatus": 1,
			"voucher_type": "Sales Order",
			"voucher_detail_no": sales_order_item,
			"from_voucher_type": "Stock Entry",
			"from_voucher_no": ["in", entry_names],
		},
		fields=["reserved_qty"],
	)
	return sum(flt(entry.reserved_qty) for entry in reservations)


def get_material_status(item_code: str) -> str:
	return "未检查" if get_default_bom(item_code) else "不涉及生产"


def _status_and_actions(row: WorkbenchRow, has_bom: bool):
	if row.unsupported:
		row.status = "不支持"
		row.next_actions = [action("查看标准订单", "view_sales_order")]
		return

	if row.pending_qty <= 0:
		row.status = "已完成"
		row.next_actions = [action("查看订单", "view_sales_order")]
		return

	if row.reserved_qty > 0:
		row.status = "可发货"
		row.next_actions.append(action("创建发货单", "create_delivery_note"))

	if row.completed_unreserved_qty > 0:
		if row.status != "可发货":
			row.status = "完工待预留"
		row.next_actions.append(action("预留完工成品", "reserve_completed_stock"))

	if row.pending_qty > row.reserved_qty:
		if row.available_to_reserve > 0:
			if row.status == "待处理":
				row.status = "待预留"
			row.next_actions.append(action("预留库存", "reserve_stock"))

	if row.unplanned_production_qty > 0 or row.active_work_order_qty > 0 or row.overplanned_qty > 0:
		if row.unplanned_production_qty > 0 and not has_bom:
			row.status = "生产资料异常"
		elif row.unplanned_production_qty > 0:
			row.status = "待安排生产"
		elif row.active_work_order_qty > 0 and row.status == "待处理":
			row.status = "生产中"
		row.next_actions.append(action("安排生产", "open_production_workbench"))

	if not row.next_actions:
		row.status = row.status if row.status != "待处理" else "待处理"
		row.next_actions = [action("查看订单", "view_sales_order")]


def _duplicate_supported_items(items):
	counts = Counter(row.item_code for row in items)
	return {item_code for item_code, count in counts.items() if count > 1}


@frappe.whitelist()
def get_order_workbench(sales_order: str):
	ensure_submitted_sales_order(sales_order)
	so = frappe.get_doc("Sales Order", sales_order)
	duplicates = _duplicate_supported_items(so.items)
	rows = []

	for item in so.items:
		order_qty = item_stock_qty(item)
		delivered_qty = delivered_stock_qty(item)
		pending_qty = pending_delivery_qty(item)
		reserved_qty = get_effective_reserved_qty(so.name, item.name)
		available_to_reserve = (
			get_available_qty_to_reserve(item.item_code, item.warehouse) if item.warehouse else 0
		)
		work_orders = get_work_orders(so.name, item.name, item.item_code)
		active_work_order_qty = get_active_work_order_qty(work_orders)
		production = calculate_production_quantities(
			pending_qty,
			reserved_qty,
			available_to_reserve,
			active_work_order_qty,
		)
		completed_qty = get_completed_qty(work_orders)
		manufacture_entries = get_manufacture_stock_entries(work_orders)
		completed_reserved_qty = get_completed_reserved_qty(item.name, manufacture_entries)
		completed_unreserved_qty = remaining_qty(completed_qty, completed_reserved_qty)
		has_bom = bool(get_default_bom(item.item_code))

		row = WorkbenchRow(
			sales_order=so.name,
			sales_order_item=item.name,
			customer=so.customer,
			item_code=item.item_code,
			item_name=item.item_name,
			warehouse=item.warehouse,
			delivery_date=str(item.delivery_date) if item.delivery_date else None,
			order_qty=order_qty,
			delivered_qty=delivered_qty,
			pending_qty=pending_qty,
			reserved_qty=production.reserved_qty,
			available_to_reserve=production.available_to_reserve,
			finished_stock_coverage_qty=production.finished_stock_coverage_qty,
			production_required_qty=production.production_required_qty,
			active_work_order_qty=active_work_order_qty,
			unplanned_production_qty=production.unplanned_production_qty,
			overplanned_qty=production.overplanned_qty,
			completed_qty=completed_qty,
			completed_unreserved_qty=completed_unreserved_qty,
			uncovered_qty=production.unplanned_production_qty,
			material_status=get_material_status(item.item_code),
			unsupported=item.item_code in duplicates,
			unsupported_reason="同一销售订单中存在重复产品行，简化流程暂不自动处理。"
			if item.item_code in duplicates
			else None,
		)
		_status_and_actions(row, has_bom)
		rows.append(row_to_dict(row))

	return {
		"sales_order": so.name,
		"customer": so.customer,
		"customer_name": so.customer_name,
		"transaction_date": str(so.transaction_date) if so.transaction_date else None,
		"delivery_date": str(so.delivery_date) if so.delivery_date else None,
		"rows": rows,
	}


def _positive_qty(row, fieldname: str) -> float:
	return max(flt(row.get(fieldname) or 0), 0)


def _has_action(row, action_name: str) -> bool:
	return any(action_row.get("action") == action_name for action_row in row.get("next_actions") or [])


def _delivery_values(rows):
	return [getdate(row.get("delivery_date")) for row in rows if row.get("delivery_date")]


def _delivery_timing(delivery_date, today):
	if not delivery_date:
		return "missing", None

	days_to_delivery = (delivery_date - today).days
	if days_to_delivery < 0:
		return "overdue", days_to_delivery
	if days_to_delivery == 0:
		return "today", days_to_delivery
	if days_to_delivery <= 7:
		return "within_7_days", days_to_delivery
	return "later", days_to_delivery


def _fulfillment_status(direct_ship: bool, needs_production: bool, uncovered_qty: float):
	if direct_ship:
		return "ready_to_ship", _("可发货")
	if needs_production:
		return "needs_production", _("需生产")
	if uncovered_qty > 0:
		return "awaiting_stock", _("待预留")
	return "awaiting_fulfillment", _("待处理")


def _row_uncovered_qty(row) -> float:
	if "unplanned_production_qty" in row:
		return _positive_qty(row, "unplanned_production_qty")
	return remaining_qty(
		_positive_qty(row, "pending_qty"),
		min(_positive_qty(row, "reserved_qty"), _positive_qty(row, "pending_qty")),
		_positive_qty(row, "active_work_order_qty"),
	)


def _row_finished_stock_coverage(row) -> float:
	if "finished_stock_coverage_qty" in row:
		return min(_positive_qty(row, "finished_stock_coverage_qty"), _positive_qty(row, "pending_qty"))
	return min(_positive_qty(row, "reserved_qty"), _positive_qty(row, "pending_qty"))


def _fulfillment_risk(
	delivery_timing: str,
	pending_rows,
	direct_ship: bool,
	pending_qty: float,
	reserved_qty: float,
):
	unsupported = any(row.get("unsupported") for row in pending_rows)
	missing_production_base = any(
		_row_uncovered_qty(row) > 0 and row.get("material_status") == "不涉及生产" for row in pending_rows
	)
	production_uncovered = any(_row_uncovered_qty(row) > 0 for row in pending_rows)
	active_production = any(_positive_qty(row, "active_work_order_qty") > 0 for row in pending_rows)
	partial_stock = 0 < sum(_row_finished_stock_coverage(row) for row in pending_rows) < pending_qty

	# Ordered candidates mirror the approved single-highest-risk hierarchy.
	candidates = (
		(delivery_timing == "overdue", ("red", 100, _("已逾期"))),
		(unsupported, ("orange", 90, _("简化操作不支持"))),
		(missing_production_base, ("orange", 90, _("缺少生产基础资料"))),
		(delivery_timing == "missing", ("orange", 85, _("缺少交期"))),
		(
			delivery_timing in {"today", "within_7_days"} and production_uncovered,
			("orange", 80, _("临期生产未覆盖")),
		),
		(production_uncovered, ("orange", 70, _("生产未覆盖"))),
		(active_production, ("blue", 60, _("生产中"))),
		(partial_stock, ("orange", 40, _("库存部分覆盖"))),
		(direct_ship, ("green", 20, _("可发货"))),
	)
	for matches, risk in candidates:
		if matches:
			return risk
	return "gray", 0, _("待确认")


def build_fulfillment_order(order, rows, today=None) -> dict:
	"""Return a read-only, order-level summary of existing workbench rows."""
	today = getdate(today or now_datetime())
	rows = list(rows or [])
	pending_rows = [row for row in rows if _positive_qty(row, "pending_qty") > 0]
	delivery_dates = _delivery_values(pending_rows)
	delivery_date = min(delivery_dates) if delivery_dates else None
	delivery_timing, days_to_delivery = _delivery_timing(delivery_date, today)

	pending_qty = sum(_positive_qty(row, "pending_qty") for row in pending_rows)
	reserved_qty = sum(
		min(_positive_qty(row, "reserved_qty"), _positive_qty(row, "pending_qty")) for row in pending_rows
	)
	available_to_reserve = sum(_positive_qty(row, "available_to_reserve") for row in pending_rows)
	finished_stock_coverage_qty = sum(_row_finished_stock_coverage(row) for row in pending_rows)
	active_work_order_qty = sum(_positive_qty(row, "active_work_order_qty") for row in pending_rows)
	production_required_qty = sum(
		_positive_qty(row, "production_required_qty")
		if "production_required_qty" in row
		else remaining_qty(_positive_qty(row, "pending_qty"), _row_finished_stock_coverage(row))
		for row in pending_rows
	)
	uncovered_qty = sum(_row_uncovered_qty(row) for row in pending_rows)
	needs_production = any(
		_positive_qty(row, "active_work_order_qty") > 0
		or _positive_qty(row, "production_required_qty") > 0
		or _row_uncovered_qty(row) > 0
		for row in pending_rows
	)
	direct_ship = bool(pending_rows) and finished_stock_coverage_qty == pending_qty and not needs_production
	status_code, status_label = _fulfillment_status(direct_ship, needs_production, uncovered_qty)
	risk_level, risk_score, risk_label = _fulfillment_risk(
		delivery_timing, pending_rows, direct_ship, pending_qty, reserved_qty
	)

	return {
		"name": order.get("name"),
		"company": order.get("company"),
		"customer": order.get("customer"),
		"customer_name": order.get("customer_name"),
		"transaction_date": str(order.get("transaction_date")) if order.get("transaction_date") else None,
		"creation": str(order.get("creation") or ""),
		"delivery_date": str(delivery_date) if delivery_date else None,
		"has_multiple_delivery_dates": len(set(delivery_dates)) > 1,
		"item_count": len(rows),
		"order_qty": sum(flt(row.get("order_qty") or 0) for row in rows),
		"delivered_qty": sum(flt(row.get("delivered_qty") or 0) for row in rows),
		"pending_qty": pending_qty,
		"reserved_qty": reserved_qty,
		"available_to_reserve": available_to_reserve,
		"finished_stock_coverage_qty": finished_stock_coverage_qty,
		"production_required_qty": production_required_qty,
		"active_work_order_qty": active_work_order_qty,
		"unplanned_production_qty": uncovered_qty,
		"completed_qty": sum(flt(row.get("completed_qty") or 0) for row in rows),
		"uncovered_qty": uncovered_qty,
		"delivery_timing": delivery_timing,
		"days_to_delivery": days_to_delivery,
		"status_code": status_code,
		"status_label": status_label,
		"risk_level": risk_level,
		"risk_score": risk_score,
		"risk_label": risk_label,
		"direct_ship": direct_ship,
		"needs_production": needs_production,
		"rows": rows,
	}


def _fulfillment_sort_key(order, fulfillment_order):
	return (
		fulfillment_order["delivery_date"] is None,
		fulfillment_order["delivery_date"] or "9999-12-31",
		-fulfillment_order["risk_score"],
		str(order.get("creation") or ""),
		fulfillment_order["name"],
	)


DEFAULT_WORKBENCH_PAGE_SIZE = 20
MAX_WORKBENCH_PAGE_SIZE = 100


def normalize_workbench_pagination(page=1, page_size=DEFAULT_WORKBENCH_PAGE_SIZE):
	page = max(cint(page) or 1, 1)
	page_size = cint(page_size)
	if page_size <= 0:
		return page, 0
	return page, min(page_size or DEFAULT_WORKBENCH_PAGE_SIZE, MAX_WORKBENCH_PAGE_SIZE)


def paginate_workbench_rows(rows, page=1, page_size=DEFAULT_WORKBENCH_PAGE_SIZE):
	page, page_size = normalize_workbench_pagination(page, page_size)
	total_count = len(rows or [])
	if page_size <= 0:
		return list(rows or []), {
			"page": 1,
			"page_size": total_count,
			"total_count": total_count,
			"total_pages": 1 if total_count else 0,
			"has_next": False,
			"has_prev": False,
		}

	total_pages = ceil(total_count / page_size) if total_count else 0
	start = (page - 1) * page_size
	end = start + page_size
	return list(rows or [])[start:end], {
		"page": page,
		"page_size": page_size,
		"total_count": total_count,
		"total_pages": total_pages,
		"has_next": bool(total_pages and page < total_pages),
		"has_prev": page > 1 and total_count > 0,
	}


def _filter_value(filters, key):
	if not filters:
		return None
	return filters.get(key)


def is_fulfillment_due_within_7_days(order):
	return order.get("delivery_timing") in {"today", "within_7_days"}


def filter_fulfillment_orders(orders, filters=None):
	filters = frappe.parse_json(filters) if isinstance(filters, str) else filters
	filters = frappe._dict(filters or {})
	search = str(_filter_value(filters, "search") or "").strip().lower()

	def matches(order):
		searchable = " ".join(
			str(value or "")
			for value in [
				order.get("name"),
				order.get("customer"),
				order.get("customer_name"),
				*[
					item
					for row in order.get("rows") or []
					for item in (row.get("item_code"), row.get("item_name"))
				],
			]
		).lower()
		delivery_window = _filter_value(filters, "deliveryWindow")
		if delivery_window == "within_7_days":
			delivery_matches = is_fulfillment_due_within_7_days(order)
		else:
			delivery_matches = not delivery_window or order.get("delivery_timing") == delivery_window
		return (
			(not search or search in searchable)
			and (not _filter_value(filters, "customer") or order.get("customer") == _filter_value(filters, "customer"))
			and delivery_matches
			and (not _filter_value(filters, "status") or order.get("status_code") == _filter_value(filters, "status"))
			and (not _filter_value(filters, "riskOnly") or order.get("risk_level") in {"red", "orange"})
		)

	return [order for order in orders or [] if matches(order)]


def fulfillment_summary(orders):
	return {
		"total_orders": len(orders or []),
		"overdue_orders": sum(order["delivery_timing"] == "overdue" for order in orders or []),
		"due_within_7_days": sum(is_fulfillment_due_within_7_days(order) for order in orders or []),
		"needs_production_orders": sum(order["needs_production"] for order in orders or []),
		"direct_ship_orders": sum(order["direct_ship"] for order in orders or []),
	}


def fulfillment_customers(orders):
	customers = {}
	for order in orders or []:
		if order.get("customer"):
			customers[order.get("customer")] = order.get("customer_name") or order.get("customer")
	return [{"value": value, "label": label} for value, label in sorted(customers.items(), key=lambda row: row[1])]


@frappe.whitelist()
def get_fulfillment_overview(page=1, page_size=DEFAULT_WORKBENCH_PAGE_SIZE, filters=None):
	"""Return readable unfinished Sales Orders recalculated through the item workbench."""
	frappe.has_permission("Sales Order", "read", throw=True)
	checked_at = now_datetime()
	orders = []
	page_length = 500
	limit_start = 0
	while True:
		db_page = frappe.get_list(
			"Sales Order",
			filters={
				"docstatus": 1,
				"status": ["not in", ["Closed", "Completed"]],
				"per_delivered": ["<", 100],
			},
			fields=["name", "company", "customer", "customer_name", "transaction_date", "delivery_date", "creation"],
			order_by="creation asc, name asc",
			limit_start=limit_start,
			limit_page_length=page_length,
		)
		orders.extend(db_page)
		if len(db_page) < page_length:
			break
		limit_start += page_length

	fulfillment_orders = []
	for order in orders:
		rows = get_order_workbench(order.name).get("rows") or []
		fulfillment_order = build_fulfillment_order(order, rows, today=checked_at)
		if fulfillment_order["pending_qty"] <= 0:
			continue
		fulfillment_orders.append((order, fulfillment_order))

	fulfillment_orders.sort(key=lambda result: _fulfillment_sort_key(*result))
	filtered_results = filter_fulfillment_orders([result for _, result in fulfillment_orders], filters)
	paged_results, pagination = paginate_workbench_rows(filtered_results, page=page, page_size=page_size)
	return {
		"checked_at": checked_at,
		"summary": fulfillment_summary(filtered_results),
		"pagination": pagination,
		"customers": fulfillment_customers(filtered_results),
		"orders": paged_results,
	}


@frappe.whitelist()
def get_work_order_details(sales_order: str, sales_order_item: str):
	work_orders = get_work_orders(sales_order, sales_order_item)
	if not work_orders:
		return {"work_orders": []}

	required_items = defaultdict(list)
	stock_entries = defaultdict(list)
	for row in frappe.get_all(
		"Work Order Item",
		filters={"parent": ["in", [wo.name for wo in work_orders]]},
		fields=[
			"parent",
			"item_code",
			"item_name",
			"source_warehouse",
			"required_qty",
			"transferred_qty",
			"consumed_qty",
			"stock_reserved_qty",
		],
	):
		required_items[row.parent].append(row)

	for row in frappe.get_all(
		"Stock Entry",
		filters={"work_order": ["in", [wo.name for wo in work_orders]], "docstatus": ["<", 2]},
		fields=["name", "work_order", "purpose", "docstatus", "fg_completed_qty", "posting_date"],
		order_by="creation asc",
	):
		stock_entries[row.work_order].append(row)

	return {
		"work_orders": [
			{
				**wo,
				"required_items": required_items.get(wo.name, []),
				"stock_entries": stock_entries.get(wo.name, []),
			}
			for wo in work_orders
		]
	}
