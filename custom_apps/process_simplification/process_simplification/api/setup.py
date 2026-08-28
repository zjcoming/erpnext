from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, parse_json

from process_simplification.defaults import get_company_manufacturing_defaults


def _message(status: str, label: str, detail: str, fieldname: str | None = None):
	return {
		"status": status,
		"label": label,
		"detail": detail,
		"fieldname": fieldname,
	}


def get_company_defaults(company: str | None = None):
	return get_company_manufacturing_defaults(company)


def resolve_production_source_warehouse(
	company: str,
	*,
	defaults=None,
	sales_order_item_warehouse: str | None = None,
):
	"""Resolve the exact warehouse used on guided Work Orders and validate it."""
	defaults = defaults or get_company_defaults(company)
	warehouse = defaults.get("source_warehouse") or sales_order_item_warehouse
	if not warehouse:
		return frappe._dict({"warehouse": None, "can_use": False, "reason": "warehouse_missing"})

	warehouse_row = frappe._dict(
		frappe.db.get_value(
			"Warehouse",
			warehouse,
			["company", "is_group", "disabled"],
			as_dict=True,
		)
		or {}
	)
	reason = None
	if not warehouse_row:
		reason = "warehouse_missing"
	elif warehouse_row.get("company") != company:
		reason = "warehouse_company_mismatch"
	elif cint(warehouse_row.get("is_group")):
		reason = "warehouse_is_group"
	elif cint(warehouse_row.get("disabled")):
		reason = "warehouse_disabled"

	return frappe._dict({"warehouse": warehouse, "can_use": not reason, "reason": reason})


def get_default_bom(item_code: str) -> str | None:
	if not item_code:
		return None

	bom = frappe.db.get_value(
		"BOM",
		{"item": item_code, "is_default": 1, "is_active": 1, "docstatus": 1},
		"name",
	)
	if bom:
		return bom

	variant_of = frappe.db.get_value("Item", item_code, "variant_of")
	if variant_of:
		return frappe.db.get_value(
			"BOM",
			{"item": variant_of, "is_default": 1, "is_active": 1, "docstatus": 1},
			"name",
		)
	return None


@frappe.whitelist()
def validate_setup(company: str | None = None, item_codes: list[str] | str | None = None):
	if isinstance(item_codes, str):
		item_codes = parse_json(item_codes)

	defaults = get_company_defaults(company)
	messages = []

	if not frappe.db.get_single_value("Stock Settings", "enable_stock_reservation"):
		messages.append(
			_message(
				"error",
				_("库存预留未启用"),
				_("请先在 Stock Settings 中启用 Enable stock reservation。"),
				"enable_stock_reservation",
			)
		)

	if not defaults.company:
		messages.append(
			_message(
				"error",
				_("默认公司缺失"),
				_("请先在 Global Defaults 或用户默认值中设置默认公司。"),
				"company",
			)
		)

	if not defaults.source_warehouse:
		messages.append(
			_message(
				"warning",
				_("默认原料仓缺失"),
				_("未设置 Stock Settings 默认仓库，创建工单时需要从订单行或物料默认值取得原料仓。"),
				"source_warehouse",
			)
		)

	if defaults.company and not defaults.wip_warehouse:
		messages.append(
			_message(
				"error",
				_("默认 WIP 仓缺失"),
				_("请在 Company 中设置 Default WIP Warehouse。"),
				"wip_warehouse",
			)
		)

	if defaults.company and not defaults.fg_warehouse:
		messages.append(
			_message(
				"warning",
				_("默认成品仓缺失"),
				_("Company 未设置 Default FG Warehouse，系统将优先使用销售订单行仓库。"),
				"fg_warehouse",
			)
		)

	missing_boms = []
	for item_code in item_codes or []:
		if item_code and not get_default_bom(item_code):
			missing_boms.append(item_code)

	if missing_boms:
		messages.append(
			_message(
				"error",
				_("默认 BOM 缺失"),
				_("以下产品没有已提交、启用的默认 BOM：{0}").format(", ".join(missing_boms)),
				"bom_no",
			)
		)

	return {
		"ok": not any(message["status"] == "error" for message in messages),
		"defaults": defaults,
		"messages": messages,
	}
