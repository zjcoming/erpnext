from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, parse_json

from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
	get_available_qty_to_reserve,
)

from process_simplification.api.production_plan_adapter import (
	create_work_orders_via_production_plan,
)

from process_simplification.api.setup import (
	get_company_defaults,
	get_default_bom,
	resolve_production_source_warehouse,
)
from process_simplification.api.production import get_allocated_production_row
from process_simplification.api.utils import (
	delivered_stock_qty,
	get_item_uom_details,
	get_sales_order_item,
	item_stock_qty,
	normalize_qty,
	pending_delivery_qty,
	remaining_qty,
	throw_chinese,
)
from process_simplification.api.workbench import (
	get_completed_reserved_qty,
	get_effective_reserved_qty,
	get_manufacture_stock_entries,
	get_order_workbench,
	get_work_orders,
)


def _payload(data):
	if isinstance(data, str):
		return frappe._dict(parse_json(data))
	return frappe._dict(data or {})


def _requested_qty(value, default):
	# An omitted/blank optional field means "use the current maximum". An
	# explicit numeric zero is user input and must reach the normal <= 0 guard;
	# treating it as omitted can create the maximum reservation or production
	# quantity after a cleared browser field or a stale/double action.
	if value is None or value == "":
		return default
	return normalize_qty(value)


def _row_from_workbench(sales_order: str, sales_order_item: str):
	workbench = get_order_workbench(sales_order)
	for row in workbench["rows"]:
		if row["sales_order_item"] == sales_order_item:
			if row.get("unsupported"):
				throw_chinese(row.get("unsupported_reason") or "该订单行暂不支持简化操作。")
			return frappe._dict(row)
	throw_chinese("销售订单明细不属于该销售订单。")


def _locked_row_from_workbench(sales_order: str, sales_order_item: str):
	# The first permission-aware snapshot is taken by the caller. Serialize on
	# the shared Sales Order Item, then rebuild the snapshot inside the lock so
	# two tabs/users cannot both act on the same pre-lock availability.
	frappe.db.get_value("Sales Order Item", sales_order_item, "name", for_update=True)
	return _row_from_workbench(sales_order, sales_order_item)


def _new_sre(
	*,
	sales_order: str,
	sales_order_item: str,
	item_code: str,
	warehouse: str,
	qty: float,
	company: str,
	voucher_qty: float,
	from_voucher_type: str | None = None,
	from_voucher_no: str | None = None,
	from_voucher_detail_no: str | None = None,
):
	item_details = get_item_uom_details(item_code)
	sre = frappe.new_doc("Stock Reservation Entry")
	sre.voucher_type = "Sales Order"
	sre.voucher_no = sales_order
	sre.voucher_detail_no = sales_order_item
	sre.item_code = item_code
	sre.warehouse = warehouse
	sre.available_qty = get_available_qty_to_reserve(item_code, warehouse)
	sre.voucher_qty = voucher_qty
	sre.reserved_qty = qty
	sre.company = company
	sre.stock_uom = item_details.stock_uom
	sre.has_serial_no = item_details.has_serial_no
	sre.has_batch_no = item_details.has_batch_no
	sre.reservation_based_on = "Serial and Batch" if item_details.has_serial_no or item_details.has_batch_no else "Qty"
	sre.from_voucher_type = from_voucher_type
	sre.from_voucher_no = from_voucher_no
	sre.from_voucher_detail_no = from_voucher_detail_no
	sre.insert()
	sre.submit()
	return sre


@frappe.whitelist(methods=["POST"])
def reserve_stock(sales_order: str, sales_order_item: str, qty: float | None = None, warehouse: str | None = None):
	frappe.has_permission("Stock Reservation Entry", "create", throw=True)
	row = _row_from_workbench(sales_order, sales_order_item)
	if row.pending_qty - row.reserved_qty <= 0:
		throw_chinese("当前没有可预留库存。")
	row = _locked_row_from_workbench(sales_order, sales_order_item)
	so = frappe.get_doc("Sales Order", sales_order)
	item = get_sales_order_item(sales_order_item)
	warehouse = warehouse or item.warehouse
	if not warehouse:
		throw_chinese("请先为销售订单明细设置成品仓库。")

	available_qty = get_available_qty_to_reserve(item.item_code, warehouse)
	max_qty = min(row.pending_qty - row.reserved_qty, available_qty)
	reserve_qty = _requested_qty(qty, max_qty)
	if reserve_qty <= 0:
		throw_chinese("当前没有可预留库存。")
	if reserve_qty > max_qty:
		throw_chinese("预留数量不能超过当前可预留库存和订单待交数量。")

	sre = _new_sre(
		sales_order=sales_order,
		sales_order_item=sales_order_item,
		item_code=item.item_code,
		warehouse=warehouse,
		qty=reserve_qty,
		company=so.company,
		voucher_qty=item_stock_qty(item),
	)
	return {"stock_reservation_entry": sre.name, "reserved_qty": reserve_qty}


@frappe.whitelist(methods=["POST"])
def create_work_order(sales_order: str, sales_order_item: str, qty: float | None = None):
	for doctype in ("Production Plan", "Work Order"):
		for permission_type in ("create", "submit"):
			frappe.has_permission(doctype, permission_type, throw=True)
	row = _row_from_workbench(sales_order, sales_order_item)
	unplanned_qty = row.get("unplanned_production_qty")
	if unplanned_qty is None:
		unplanned_qty = row.get("uncovered_qty")
	if not unplanned_qty or unplanned_qty <= 0:
		throw_chinese("该订单行已经被库存预留或生产任务覆盖，不能重复创建生产任务。")
	row = _locked_row_from_workbench(sales_order, sales_order_item)
	unplanned_qty = row.get("unplanned_production_qty")
	if unplanned_qty is None:
		unplanned_qty = row.get("uncovered_qty")
	if not unplanned_qty or unplanned_qty <= 0:
		throw_chinese("该订单行已经被库存预留或生产任务覆盖，不能重复创建生产任务。")
	allocated_row = get_allocated_production_row(sales_order, sales_order_item)
	unplanned_qty = flt(allocated_row.get("unplanned_production_qty")) if allocated_row else 0
	if unplanned_qty <= 0:
		throw_chinese("按订单交期分配当前成品库存后，该订单行已无需新增生产任务。")

	item = get_sales_order_item(sales_order_item)
	so = frappe.get_doc("Sales Order", sales_order)
	bom_no = item.get("bom_no") or get_default_bom(item.item_code)
	if not bom_no:
		throw_chinese("产品 {0} 没有已提交、启用的默认 BOM。".format(item.item_code))

	defaults = get_company_defaults(so.company)
	resolved_source = resolve_production_source_warehouse(
		so.company,
		defaults=defaults,
		sales_order_item_warehouse=item.warehouse,
	)
	work_order_qty = _requested_qty(qty, unplanned_qty)
	if work_order_qty <= 0 or work_order_qty > unplanned_qty:
		throw_chinese("本次生产数量不能超过当前尚未覆盖数量。")

	fg_warehouse = item.warehouse or defaults.fg_warehouse
	if not resolved_source.can_use:
		throw_chinese("原料仓不可用，请确认仓库存在、启用、非分组且属于订单公司。")
	if not defaults.wip_warehouse:
		throw_chinese("缺少 WIP 仓，请在 Company 中设置 Default WIP Warehouse。")
	if not fg_warehouse:
		throw_chinese("缺少成品仓，请在 Company 或订单行中设置仓库。")

	# Create the finished-good Work Order plus one Work Order per in-house
	# sub-assembly level via the standard Production Plan engine, while the
	# delivery-priority net quantity above stays authoritative.
	result = create_work_orders_via_production_plan(
		sales_order=sales_order,
		sales_order_item=sales_order_item,
		company=so.company,
		item_code=item.item_code,
		bom_no=bom_no,
		planned_qty=work_order_qty,
		fg_warehouse=fg_warehouse,
		sub_assembly_warehouse=resolved_source.warehouse,
		source_warehouse=resolved_source.warehouse,
		delivery_date=item.delivery_date,
	)
	work_orders = result.get("work_orders") or []
	return {
		"work_order": work_orders[0] if work_orders else None,
		"work_orders": work_orders,
		"sub_assembly_count": result.get("sub_assembly_count", 0),
		"production_plan": result.get("production_plan"),
		"qty": work_order_qty,
	}


def _manufactured_finished_rows(sales_order: str, sales_order_item: str):
	item = get_sales_order_item(sales_order_item)
	work_orders = get_work_orders(sales_order, sales_order_item, item.item_code)
	manufacture_entries = get_manufacture_stock_entries(work_orders)
	if not manufacture_entries:
		return []

	return frappe.get_all(
		"Stock Entry Detail",
		filters={
			"parent": ["in", [entry.name for entry in manufacture_entries]],
			"item_code": item.item_code,
			"is_finished_item": 1,
		},
		fields=["name", "parent", "item_code", "t_warehouse", "transfer_qty"],
		order_by="creation asc",
	)


@frappe.whitelist(methods=["POST"])
def reserve_completed_stock(sales_order: str, sales_order_item: str, qty: float | None = None):
	frappe.has_permission("Stock Reservation Entry", "create", throw=True)
	row = _row_from_workbench(sales_order, sales_order_item)
	if row.completed_unreserved_qty <= 0:
		throw_chinese("当前没有完工待预留数量。")
	row = _locked_row_from_workbench(sales_order, sales_order_item)
	if row.completed_unreserved_qty <= 0:
		throw_chinese("当前没有完工待预留数量。")

	so = frappe.get_doc("Sales Order", sales_order)
	item = get_sales_order_item(sales_order_item)
	qty_to_reserve = _requested_qty(qty, row.completed_unreserved_qty)
	if qty_to_reserve <= 0 or qty_to_reserve > row.completed_unreserved_qty:
		throw_chinese("预留完工成品数量不能超过完工待预留数量。")

	created = []
	for finished_row in _manufactured_finished_rows(sales_order, sales_order_item):
		if qty_to_reserve <= 0:
			break
		if not finished_row.t_warehouse:
			continue
		available_qty = get_available_qty_to_reserve(item.item_code, finished_row.t_warehouse)
		line_qty = min(qty_to_reserve, normalize_qty(finished_row.transfer_qty), available_qty)
		if line_qty <= 0:
			continue
		sre = _new_sre(
			sales_order=sales_order,
			sales_order_item=sales_order_item,
			item_code=item.item_code,
			warehouse=finished_row.t_warehouse,
			qty=line_qty,
			company=so.company,
			voucher_qty=item_stock_qty(item),
			from_voucher_type="Stock Entry",
			from_voucher_no=finished_row.parent,
			from_voucher_detail_no=finished_row.name,
		)
		created.append(sre.name)
		qty_to_reserve -= line_qty

	if qty_to_reserve > 0:
		throw_chinese("完工成品库存不足，无法完成本次预留。")

	return {"stock_reservation_entries": created}


def _get_existing_draft_delivery_note(sales_order: str, sales_order_item: str):
	rows = frappe.get_all(
		"Delivery Note Item",
		filters={
			"against_sales_order": sales_order,
			"so_detail": sales_order_item,
			"docstatus": 0,
		},
		fields=["parent"],
		order_by="creation desc",
		limit=1,
	)
	if not rows:
		return None

	delivery_note = frappe.get_doc("Delivery Note", rows[0].parent)
	if delivery_note.docstatus != 0:
		return None
	delivery_note.check_permission("read")
	return delivery_note


@frappe.whitelist(methods=["POST"])
def create_delivery_note(sales_order: str, sales_order_item: str):
	frappe.has_permission("Delivery Note", "create", throw=True)
	row = _row_from_workbench(sales_order, sales_order_item)
	# Serialize requests for the same order row so concurrent clicks cannot both create a draft.
	row = _locked_row_from_workbench(sales_order, sales_order_item)
	existing_draft = _get_existing_draft_delivery_note(sales_order, sales_order_item)
	if existing_draft:
		return {"delivery_note": existing_draft.name, "docstatus": existing_draft.docstatus, "reused": True}
	if row.reserved_qty <= 0:
		throw_chinese("当前订单行没有有效预留，不能创建发货单。")

	delivery_note = make_delivery_note(
		sales_order,
		kwargs={"for_reserved_stock": True, "filtered_children": [sales_order_item]},
	)
	delivery_note.items = [item for item in delivery_note.items if item.so_detail == sales_order_item]
	if not delivery_note.items:
		throw_chinese("未能根据有效预留生成发货单明细。")

	item_row = get_sales_order_item(sales_order_item)
	max_qty = row.reserved_qty / normalize_qty(item_row.conversion_factor or 1)
	for item in delivery_note.items:
		if normalize_qty(item.qty) > max_qty:
			item.qty = max_qty
	delivery_note.insert()
	return {"delivery_note": delivery_note.name, "docstatus": delivery_note.docstatus, "reused": False}
