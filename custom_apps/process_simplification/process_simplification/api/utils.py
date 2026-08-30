from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import frappe
from frappe import _
from frappe.utils import flt


ACTIVE_WORK_ORDER_STATUSES = ("Submitted", "Not Started", "In Process", "Stock Reserved", "Stock Partially Reserved")
TERMINAL_WORK_ORDER_STATUSES = ("Completed", "Stopped", "Closed", "Cancelled")


class SimplifiedFlowError(frappe.ValidationError):
	pass


@dataclass
class Action:
	label: str
	action: str
	enabled: bool = True
	reason: str | None = None


@dataclass
class WorkbenchRow:
	sales_order: str
	sales_order_item: str
	customer: str
	item_code: str
	item_name: str
	warehouse: str | None
	delivery_date: str | None
	order_qty: float
	delivered_qty: float
	pending_qty: float
	reserved_qty: float = 0
	available_to_reserve: float = 0
	finished_stock_coverage_qty: float = 0
	production_required_qty: float = 0
	active_work_order_qty: float = 0
	unplanned_production_qty: float = 0
	overplanned_qty: float = 0
	completed_qty: float = 0
	completed_unreserved_qty: float = 0
	uncovered_qty: float = 0
	material_status: str = "未检查"
	status: str = "待处理"
	unsupported: bool = False
	unsupported_reason: str | None = None
	next_actions: list[Action] = field(default_factory=list)

	def as_dict(self):
		data = asdict(self)
		data["next_actions"] = [asdict(action) for action in self.next_actions]
		return data


def throw_chinese(message: str, title: str = "流程简化"):
	frappe.throw(_(message), SimplifiedFlowError, title=_(title))


def normalize_qty(value: Any, precision: int | None = None) -> float:
	return flt(value or 0, precision)


def resolve_item_display_name(item_code, current_item_name=None, document_item_name=None):
	"""Prefer the current Item name while retaining old document text as a fallback."""
	code = str(item_code or "").strip()
	candidates = [current_item_name, document_item_name]
	for candidate in candidates:
		name = str(candidate or "").strip()
		if name and name != code:
			return name
	return next((str(candidate or "").strip() for candidate in candidates if candidate), code)


def get_current_item_names(item_codes) -> dict[str, str]:
	item_codes = sorted({str(item_code).strip() for item_code in item_codes or [] if item_code})
	if not item_codes:
		return {}
	return {
		row.name: row.item_name
		for row in frappe.get_all(
			"Item",
			filters={"name": ["in", item_codes]},
			fields=["name", "item_name"],
		)
	}


def apply_current_item_names(rows, *, item_code_field="item_code", item_name_field="item_name"):
	"""Refresh item labels in API rows without changing transactional documents."""
	rows = list(rows or [])
	current_names = get_current_item_names(
		[row.get(item_code_field) for row in rows if row.get(item_code_field)]
	)
	for row in rows:
		item_code = row.get(item_code_field)
		if not item_code:
			continue
		row[item_name_field] = resolve_item_display_name(
			item_code,
			current_names.get(item_code),
			row.get(item_name_field),
		)
	return rows


def remaining_qty(total: float, *deductions: float) -> float:
	return max(normalize_qty(total) - sum(normalize_qty(value) for value in deductions), 0)


def ensure_submitted_sales_order(sales_order: str):
	docstatus = frappe.db.get_value("Sales Order", sales_order, "docstatus")
	if docstatus is None:
		throw_chinese("销售订单不存在。")
	if int(docstatus) != 1:
		throw_chinese("只能处理已提交的销售订单。")


def get_sales_order_item(sales_order_item: str):
	row = frappe.db.get_value(
		"Sales Order Item",
		sales_order_item,
		[
			"name",
			"parent",
			"item_code",
			"item_name",
			"warehouse",
			"delivery_date",
			"qty",
			"stock_qty",
			"delivered_qty",
			"conversion_factor",
			"stock_uom",
			"bom_no",
			"docstatus",
		],
		as_dict=True,
	)
	if not row:
		throw_chinese("销售订单明细不存在。")
	return row


def item_stock_qty(row) -> float:
	return normalize_qty(row.stock_qty or row.qty)


def delivered_stock_qty(row) -> float:
	return normalize_qty(row.delivered_qty) * normalize_qty(row.conversion_factor or 1)


def pending_delivery_qty(row) -> float:
	return remaining_qty(item_stock_qty(row), delivered_stock_qty(row))


def get_item_uom_details(item_code: str):
	details = frappe.get_cached_value(
		"Item",
		item_code,
		["stock_uom", "has_serial_no", "has_batch_no", "is_stock_item"],
		as_dict=True,
	)
	if not details:
		throw_chinese("物料不存在：{0}".format(item_code))
	return details


def action(label: str, action_name: str, enabled: bool = True, reason: str | None = None) -> Action:
	return Action(label=label, action=action_name, enabled=enabled, reason=reason)


def row_to_dict(row: WorkbenchRow):
	return row.as_dict()
