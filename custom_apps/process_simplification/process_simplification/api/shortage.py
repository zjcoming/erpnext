from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import add_days, nowdate, parse_json

from erpnext.manufacturing.doctype.bom.bom import get_bom_items_as_dict
from erpnext.stock.utils import get_stock_balance

from process_simplification.api.setup import get_company_defaults, get_default_bom
from process_simplification.api.utils import normalize_qty, throw_chinese
from process_simplification.api.workbench import get_order_workbench


def _parse(value):
	if isinstance(value, str):
		return parse_json(value)
	return value


def _selected_rows(selected_rows):
	selected_rows = _parse(selected_rows) or []
	if not selected_rows:
		throw_chinese("请至少选择一条订单明细。")
	return [frappe._dict(row) for row in selected_rows]


def _workbench_row(sales_order: str, sales_order_item: str):
	for row in get_order_workbench(sales_order)["rows"]:
		if row["sales_order_item"] == sales_order_item:
			return frappe._dict(row)
	throw_chinese("销售订单明细不存在或不属于该销售订单。")


def _mr_outstanding(item_code: str) -> float:
	mr = frappe.qb.DocType("Material Request")
	mri = frappe.qb.DocType("Material Request Item")
	result = (
		frappe.qb.from_(mri)
		.join(mr)
		.on(mri.parent == mr.name)
		.select(Sum(mri.stock_qty - mri.ordered_qty))
		.where(
			(mri.item_code == item_code)
			& (mr.docstatus == 1)
			& (mr.material_request_type == "Purchase")
			& (mr.status.notin(["Stopped", "Cancelled"]))
		)
	).run()
	return normalize_qty(result[0][0] if result else 0)


def _po_outstanding(item_code: str) -> float:
	po = frappe.qb.DocType("Purchase Order")
	poi = frappe.qb.DocType("Purchase Order Item")
	result = (
		frappe.qb.from_(poi)
		.join(po)
		.on(poi.parent == po.name)
		.select(Sum(poi.stock_qty - poi.received_qty))
		.where(
			(poi.item_code == item_code)
			& (po.docstatus == 1)
			& (po.status.notin(["Closed", "Cancelled"]))
		)
	).run()
	return normalize_qty(result[0][0] if result else 0)


def _source_note(sources):
	return "; ".join(
		"{sales_order}/{sales_order_item}/{finished_item}: {qty}".format(**source)
		for source in sources
	)


@frappe.whitelist()
def check_shortage(selected_rows, company: str | None = None):
	frappe.has_permission("Material Request", "read", throw=True)
	rows = _selected_rows(selected_rows)
	defaults = get_company_defaults(company)
	company = company or defaults.company
	if not company:
		throw_chinese("默认公司缺失，请先设置公司。")

	materials = {}
	for selected in rows:
		workbench_row = _workbench_row(selected.sales_order, selected.sales_order_item)
		if workbench_row.get("unsupported"):
			continue

		demand_qty = normalize_qty(selected.get("qty")) or normalize_qty(workbench_row.uncovered_qty)
		if demand_qty <= 0:
			demand_qty = normalize_qty(workbench_row.active_work_order_qty)
		if demand_qty <= 0:
			continue

		bom_no = get_default_bom(workbench_row.item_code)
		if not bom_no:
			continue

		bom_items = get_bom_items_as_dict(bom_no, company, qty=demand_qty, fetch_exploded=1)
		for item_code, bom_item in bom_items.items():
			warehouse = bom_item.get("source_warehouse") or bom_item.get("default_warehouse") or defaults.source_warehouse
			key = (item_code, warehouse)
			if key not in materials:
				materials[key] = {
					"item_code": item_code,
					"item_name": bom_item.get("item_name"),
					"stock_uom": bom_item.get("stock_uom"),
					"warehouse": warehouse,
					"required_qty": 0,
					"available_qty": 0,
					"open_material_request_qty": 0,
					"open_purchase_order_qty": 0,
					"shortage_qty": 0,
					"sources": [],
				}
			materials[key]["required_qty"] += normalize_qty(bom_item.get("qty"))
			materials[key]["sources"].append(
				{
					"sales_order": selected.sales_order,
					"sales_order_item": selected.sales_order_item,
					"finished_item": workbench_row.item_code,
					"qty": demand_qty,
				}
			)

	for material in materials.values():
		material["available_qty"] = get_stock_balance(material["item_code"], material["warehouse"]) if material["warehouse"] else 0
		material["open_material_request_qty"] = _mr_outstanding(material["item_code"])
		material["open_purchase_order_qty"] = _po_outstanding(material["item_code"])
		material["shortage_qty"] = max(
			normalize_qty(material["required_qty"])
			- normalize_qty(material["available_qty"])
			- normalize_qty(material["open_material_request_qty"])
			- normalize_qty(material["open_purchase_order_qty"]),
			0,
		)

	shortages = [material for material in materials.values() if material["shortage_qty"] > 0]
	if not shortages:
		return {"shortages": [], "message": "当前选择的订单没有需要采购的缺料。"}
	return {"shortages": shortages}


@frappe.whitelist()
def create_material_request(shortage_rows, company: str | None = None, schedule_date: str | None = None):
	frappe.has_permission("Material Request", "create", throw=True)
	shortage_rows = _parse(shortage_rows) or []
	if not shortage_rows:
		throw_chinese("请至少选择一条缺料记录。")

	defaults = get_company_defaults(company)
	company = company or defaults.company
	if not company:
		throw_chinese("默认公司缺失，请先设置公司。")

	mr = frappe.new_doc("Material Request")
	mr.material_request_type = "Purchase"
	mr.company = company
	mr.transaction_date = nowdate()
	mr.schedule_date = schedule_date or add_days(nowdate(), 1)

	for index, row in enumerate(shortage_rows, start=1):
		row = frappe._dict(row)
		qty = normalize_qty(row.get("purchase_qty") or row.get("shortage_qty"))
		shortage_qty = normalize_qty(row.get("shortage_qty"))
		if qty <= 0:
			throw_chinese("第 {0} 行采购数量必须大于 0。".format(index))
		if qty > shortage_qty and not row.get("allow_over_purchase"):
			throw_chinese("第 {0} 行采购数量不能超过采购缺口。".format(index))

		mr.append(
			"items",
			{
				"item_code": row.item_code,
				"qty": qty,
				"schedule_date": row.get("schedule_date") or mr.schedule_date,
				"warehouse": row.get("warehouse") or defaults.source_warehouse,
				"description": "流程简化缺料来源：{0}".format(_source_note(row.get("sources") or [])),
			},
		)

	mr.insert()
	mr.submit()
	return {"material_request": mr.name, "docstatus": mr.docstatus}
