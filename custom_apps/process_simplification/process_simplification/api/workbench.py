from __future__ import annotations

from collections import Counter, defaultdict

import frappe
from frappe import _
from frappe.utils import flt

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


def _status_and_actions(row: WorkbenchRow, item_code: str, warehouse: str | None, has_bom: bool):
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

	if row.uncovered_qty > 0:
		available_qty = get_available_qty_to_reserve(item_code, warehouse) if warehouse else 0
		if available_qty > 0:
			if row.status == "待处理":
				row.status = "待预留"
			row.next_actions.append(action("预留库存", "reserve_stock"))
		if has_bom:
			if row.status == "待处理":
				row.status = "待生产"
			row.next_actions.append(action("创建生产任务", "create_work_order"))

	if row.active_work_order_qty > 0:
		if row.status == "待处理":
			row.status = "生产中"
		row.next_actions.append(action("查看生产任务", "view_work_orders"))

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
		work_orders = get_work_orders(so.name, item.name, item.item_code)
		active_work_order_qty = get_active_work_order_qty(work_orders)
		completed_qty = get_completed_qty(work_orders)
		manufacture_entries = get_manufacture_stock_entries(work_orders)
		completed_reserved_qty = get_completed_reserved_qty(item.name, manufacture_entries)
		completed_unreserved_qty = remaining_qty(completed_qty, completed_reserved_qty)
		uncovered_qty = remaining_qty(pending_qty, reserved_qty, active_work_order_qty)
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
			reserved_qty=reserved_qty,
			active_work_order_qty=active_work_order_qty,
			completed_qty=completed_qty,
			completed_unreserved_qty=completed_unreserved_qty,
			uncovered_qty=uncovered_qty,
			material_status=get_material_status(item.item_code),
			unsupported=item.item_code in duplicates,
			unsupported_reason="同一销售订单中存在重复产品行，简化流程暂不自动处理。"
			if item.item_code in duplicates
			else None,
		)
		_status_and_actions(row, item.item_code, item.warehouse, has_bom)
		rows.append(row_to_dict(row))

	return {
		"sales_order": so.name,
		"customer": so.customer,
		"customer_name": so.customer_name,
		"transaction_date": str(so.transaction_date) if so.transaction_date else None,
		"delivery_date": str(so.delivery_date) if so.delivery_date else None,
		"rows": rows,
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
