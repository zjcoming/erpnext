from __future__ import annotations

import frappe
from frappe import _
from frappe.desk.search import sanitize_searchfield
from frappe.utils import add_days, nowdate, parse_json

from erpnext import get_default_company
from erpnext.stock.utils import get_stock_balance

from process_simplification.api.setup import get_company_defaults, get_default_bom
from process_simplification.api.utils import get_item_uom_details, normalize_qty, throw_chinese


def _as_dict(payload):
	if isinstance(payload, str):
		return frappe._dict(parse_json(payload))
	return frappe._dict(payload or {})


def _normalize_items(items):
	if isinstance(items, str):
		items = parse_json(items)
	if not items:
		throw_chinese("请至少添加一个产品。")

	normalized = []
	for idx, item in enumerate(items, start=1):
		row = frappe._dict(item)
		if not row.get("item_code"):
			throw_chinese("第 {0} 行产品不能为空。".format(idx))
		if normalize_qty(row.get("qty")) <= 0:
			throw_chinese("第 {0} 行数量必须大于 0。".format(idx))
		if normalize_qty(row.get("rate")) < 0:
			throw_chinese("第 {0} 行单价不能小于 0。".format(idx))
		if not row.get("delivery_date"):
			throw_chinese("第 {0} 行交付日期不能为空。".format(idx))
		get_item_uom_details(row.item_code)
		normalized.append(row)
	return normalized


def validate_no_duplicate_finished_goods(items):
	seen = set()
	duplicates = []
	for row in items:
		item_code = row.get("item_code")
		if item_code in seen and item_code not in duplicates:
			duplicates.append(item_code)
		seen.add(item_code)

	if duplicates:
		throw_chinese(
			"同一张快速开单中，同一个成品只能出现一次。请合并或拆分以下产品：{0}".format(
				", ".join(duplicates)
			)
		)


def _order_terms(allow_partial_delivery, remarks):
	partial_text = "允许分批发货：{0}".format("是" if allow_partial_delivery else "否")
	if remarks:
		return "{0}<br>{1}".format(partial_text, frappe.utils.escape_html(remarks))
	return partial_text


def _selling_price_list() -> str | None:
	return frappe.db.get_single_value("Selling Settings", "selling_price_list")


def _item_default(item_code: str, company: str | None):
	if not company:
		return frappe._dict()

	return (
		frappe.db.get_value(
			"Item Default",
			{"parent": item_code, "company": company},
			["default_warehouse", "default_price_list"],
			as_dict=True,
		)
		or frappe._dict()
	)


def _item_price(item_code: str, price_list: str | None):
	filters = {"item_code": item_code, "selling": 1}
	if price_list:
		filters["price_list"] = price_list

	price = frappe.db.get_value(
		"Item Price",
		filters,
		["price_list", "price_list_rate", "currency"],
		as_dict=True,
	)
	if price or not price_list:
		return price

	return frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "selling": 1},
		["price_list", "price_list_rate", "currency"],
		as_dict=True,
	)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def search_quick_order_products(doctype, txt, searchfield, start, page_len, filters):
	searchfield = searchfield or "name"
	sanitize_searchfield(searchfield)
	search_value = "%{0}%".format(txt or "")

	return frappe.db.sql(
		f"""
		select item.name, item.item_name
		from `tabItem` item
		where item.disabled = 0
			and item.is_sales_item = 1
			and item.has_variants = 0
			and exists (
				select 1
				from `tabBOM` bom
				where bom.item = item.name
					and bom.docstatus = 1
					and bom.is_active = 1
			)
			and (
				item.`{searchfield}` like %(txt)s
				or item.item_name like %(txt)s
				or item.name like %(txt)s
			)
		order by
			case when item.name like %(prefix)s then 0 else 1 end,
			item.modified desc
		limit %(start)s, %(page_len)s
		""",
		{
			"txt": search_value,
			"prefix": "{0}%".format(txt or ""),
			"start": start,
			"page_len": page_len,
		},
	)


@frappe.whitelist()
def get_quick_order_context(company: str | None = None):
	defaults = get_company_defaults(company)
	return {
		"company": defaults.company,
		"source_warehouse": defaults.source_warehouse,
		"fg_warehouse": defaults.fg_warehouse,
		"wip_warehouse": defaults.wip_warehouse,
		"selling_price_list": _selling_price_list(),
		"default_delivery_date": add_days(nowdate(), 7),
	}


@frappe.whitelist()
def get_quick_order_item_defaults(item_code: str, company: str | None = None):
	frappe.has_permission("Item", "read", throw=True)
	if not item_code:
		throw_chinese("产品不能为空。")

	item = frappe.get_cached_value(
		"Item",
		item_code,
		["item_code", "item_name", "stock_uom", "is_sales_item", "is_stock_item", "disabled", "has_variants"],
		as_dict=True,
	)
	if not item:
		throw_chinese("产品不存在：{0}".format(item_code))
	if item.disabled:
		throw_chinese("产品已停用：{0}".format(item_code))
	if not item.is_sales_item:
		throw_chinese("产品不是销售物料：{0}".format(item_code))

	company = company or get_default_company()
	defaults = get_company_defaults(company)
	item_default = _item_default(item_code, defaults.company)
	warehouse = item_default.default_warehouse or defaults.fg_warehouse
	price_list = item_default.default_price_list or _selling_price_list()
	price = _item_price(item_code, price_list) or frappe._dict()
	available_qty = get_stock_balance(item_code, warehouse) if item.is_stock_item and warehouse else 0

	return {
		"item_code": item.item_code,
		"item_name": item.item_name,
		"stock_uom": item.stock_uom,
		"warehouse": warehouse,
		"rate": normalize_qty(price.get("price_list_rate")),
		"currency": price.get("currency"),
		"price_list": price.get("price_list") or price_list,
		"available_qty": available_qty,
		"has_bom": bool(get_default_bom(item_code)),
		"has_variants": item.has_variants,
	}


@frappe.whitelist()
def create_quick_sales_order(payload=None, **kwargs):
	frappe.has_permission("Sales Order", "create", throw=True)
	data = _as_dict(payload or kwargs)

	if not data.get("confirmed"):
		throw_chinese("请先二次确认后再提交快速开单。")

	customer = data.get("customer")
	if not customer:
		throw_chinese("客户不能为空。")

	items = _normalize_items(data.get("items"))
	validate_no_duplicate_finished_goods(items)

	company = data.get("company") or get_default_company()
	if not company:
		throw_chinese("默认公司缺失，请先设置公司。")

	defaults = get_company_defaults(company)
	delivery_date = data.get("delivery_date") or max(row.delivery_date for row in items)

	so = frappe.new_doc("Sales Order")
	so.customer = customer
	so.company = company
	so.order_type = "Sales"
	so.transaction_date = data.get("transaction_date") or nowdate()
	so.delivery_date = delivery_date
	so.terms = _order_terms(data.get("allow_partial_delivery"), data.get("remarks"))
	so.group_same_items = 0

	for row in items:
		so.append(
			"items",
			{
				"item_code": row.item_code,
				"qty": normalize_qty(row.qty),
				"rate": normalize_qty(row.get("rate")),
				"delivery_date": row.delivery_date,
				"warehouse": row.get("warehouse") or defaults.fg_warehouse,
				"description": row.get("remarks") or row.get("description"),
			},
		)

	try:
		so.insert()
		so.submit()
	except Exception as exc:
		frappe.db.rollback()
		throw_chinese("创建销售订单失败：{0}".format(frappe.utils.escape_html(str(exc))))

	return {
		"sales_order": so.name,
		"docstatus": so.docstatus,
		"route": ["order-workbench", so.name],
	}
