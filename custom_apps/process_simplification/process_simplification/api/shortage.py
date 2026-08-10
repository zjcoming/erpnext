from __future__ import annotations

import frappe
from frappe.utils import add_days, nowdate, parse_json

from erpnext.manufacturing.doctype.bom.bom import get_bom_items_as_dict

from process_simplification.api.setup import (
	get_company_defaults,
	get_default_bom,
	resolve_production_source_warehouse,
)
from process_simplification.api.utils import normalize_qty, throw_chinese
from process_simplification.api.workbench import get_order_workbench


class MaterialCoverageBomExpansionError(Exception):
	"""The requested BOM could not be expanded for material coverage."""


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


def get_material_stock_snapshot(item_code: str, warehouse: str | None) -> frappe._dict:
	"""Return stock usable for this material in one source warehouse only."""
	if not warehouse:
		return frappe._dict(
			{
				"can_calculate": False,
				"actual_qty": 0,
				"committed_qty": 0,
				"available_qty": 0,
			}
		)

	bin_row = frappe._dict(
		frappe.db.get_value(
			"Bin",
			{"item_code": item_code, "warehouse": warehouse},
			[
				"actual_qty",
				"reserved_qty",
				"reserved_qty_for_production",
				"reserved_qty_for_sub_contract",
				"reserved_qty_for_production_plan",
			],
			as_dict=True,
		)
		or {}
	)
	actual_qty = normalize_qty(bin_row.get("actual_qty"))
	committed_qty = sum(
		max(normalize_qty(bin_row.get(field)), 0)
		for field in (
			"reserved_qty",
			"reserved_qty_for_production",
			"reserved_qty_for_sub_contract",
			"reserved_qty_for_production_plan",
		)
	)
	return frappe._dict(
		{
			"can_calculate": True,
			"actual_qty": actual_qty,
			"committed_qty": committed_qty,
			"available_qty": max(actual_qty - committed_qty, 0),
		}
	)


def _mr_outstanding(item_code: str, warehouse: str | None, company: str, need_by_date: str | None) -> float:
	if not warehouse:
		return 0
	mr = frappe.qb.DocType("Material Request")
	mri = frappe.qb.DocType("Material Request Item")
	query = (
		frappe.qb.from_(mri)
		.join(mr)
		.on(mri.parent == mr.name)
		.select(mri.stock_qty, mri.ordered_qty)
		.where(
			(mri.item_code == item_code)
			& (mri.warehouse == warehouse)
			& (mr.company == company)
			& (mr.docstatus == 1)
			& (mr.material_request_type == "Purchase")
			& (mr.status.notin(["Stopped", "Cancelled"]))
		)
	)
	if need_by_date:
		query = query.where(mri.schedule_date <= need_by_date)
	return sum(
		max(normalize_qty(row[0]) - normalize_qty(row[1]), 0)
		for row in query.run()
	)


def _po_outstanding(item_code: str, warehouse: str | None, company: str, need_by_date: str | None) -> float:
	if not warehouse:
		return 0
	po = frappe.qb.DocType("Purchase Order")
	poi = frappe.qb.DocType("Purchase Order Item")
	query = (
		frappe.qb.from_(poi)
		.join(po)
		.on(poi.parent == po.name)
		.select(poi.stock_qty, poi.received_qty, poi.conversion_factor)
		.where(
			(poi.item_code == item_code)
			& (poi.warehouse == warehouse)
			& (po.company == company)
			& (po.docstatus == 1)
			& (po.status.notin(["Closed", "Cancelled"]))
		)
	)
	if need_by_date:
		query = query.where(poi.schedule_date <= need_by_date)
	return sum(
		max(normalize_qty(row[0]) - normalize_qty(row[1]) * normalize_qty(row[2] or 1), 0)
		for row in query.run()
	)


def _source_note(sources):
	return "; ".join(
		"{0}/{1}/{2}: {3}".format(
			source.get("sales_order") or "",
			source.get("sales_order_item") or source.get("row") or "",
			source.get("finished_item") or "",
			source.get("required_qty") or source.get("qty") or 0,
		)
		for source in sources
	)


def _prior_required_by_key(demands, company: str, defaults) -> dict:
	"""Sum raw-material demand per (item, warehouse) for demands that draw on the
	same shared stock but are not part of the reported set.

	Used so a partial (per-order) coverage check reduces free stock by other
	in-flight demands first, instead of letting every order claim the same stock.
	"""
	consumed: dict = {}
	for demand in demands or []:
		demand = frappe._dict(demand)
		qty = normalize_qty(demand.get("qty"))
		if not demand.get("bom_no") or qty <= 0:
			continue
		try:
			bom_items = get_bom_items_as_dict(demand.bom_no, company, qty=qty, fetch_exploded=1)
		except Exception as exc:
			raise MaterialCoverageBomExpansionError(demand.bom_no) from exc
		resolved_source = resolve_production_source_warehouse(
			company,
			defaults=defaults,
			sales_order_item_warehouse=(demand.get("source") or {}).get("sales_order_item_warehouse"),
		)
		for bom_item in bom_items.values():
			key = (bom_item.get("item_code"), resolved_source.warehouse)
			consumed[key] = consumed.get(key, 0) + normalize_qty(bom_item.get("qty"))
	return consumed


def calculate_material_coverage(
	demands, company: str, need_by_date: str | None = None, defaults=None, prior_demands=None
) -> frappe._dict:
	"""Explain material availability, approved supply, and new purchase needs.

	``prior_demands`` are other in-flight demands that draw on the same shared
	stock; their raw-material need is consumed from free stock before the
	reported ``demands`` are evaluated, so a per-order check does not let every
	order believe the same scarce stock covers it.
	"""
	defaults = defaults or get_company_defaults(company)
	prior_consumed = _prior_required_by_key(prior_demands, company, defaults)
	materials = {}
	for demand in demands or []:
		demand = frappe._dict(demand)
		qty = normalize_qty(demand.get("qty"))
		if not demand.get("bom_no") or qty <= 0:
			continue

		try:
			bom_items = get_bom_items_as_dict(demand.bom_no, company, qty=qty, fetch_exploded=1)
		except Exception as exc:
			raise MaterialCoverageBomExpansionError(demand.bom_no) from exc
		resolved_source = resolve_production_source_warehouse(
			company,
			defaults=defaults,
			sales_order_item_warehouse=(demand.get("source") or {}).get(
				"sales_order_item_warehouse"
			),
		)
		for bom_item in bom_items.values():
			item_code = bom_item.get("item_code")
			warehouse = resolved_source.warehouse
			key = (item_code, warehouse)
			if key not in materials:
				materials[key] = {
					"item_code": item_code,
					"item_name": bom_item.get("item_name"),
					"stock_uom": bom_item.get("stock_uom"),
					"warehouse": warehouse,
					"required_qty": 0,
					"actual_qty": 0,
					"committed_qty": 0,
					"available_qty": 0,
					"open_material_request_qty": 0,
					"open_purchase_order_qty": 0,
					"current_gap_qty": 0,
					"shortage_qty": 0,
					"status": "cannot_calculate",
					"blocked": False,
					"warehouse_can_use": bool(resolved_source.can_use),
					"sources": [],
				}
			else:
				materials[key]["warehouse_can_use"] = bool(
					materials[key]["warehouse_can_use"] and resolved_source.can_use
				)
			contribution_qty = normalize_qty(bom_item.get("qty"))
			materials[key]["required_qty"] += contribution_qty
			source = dict(demand.get("source") or {})
			source["required_qty"] = contribution_qty
			source["bom_qty_per_unit"] = contribution_qty / qty
			materials[key]["sources"].append(source)

	for material in materials.values():
		warehouse_can_use = material.pop("warehouse_can_use")
		if not warehouse_can_use:
			material["blocked"] = True
			continue
		snapshot = get_material_stock_snapshot(material["item_code"], material["warehouse"])
		material["actual_qty"] = normalize_qty(snapshot.get("actual_qty"))
		material["committed_qty"] = normalize_qty(snapshot.get("committed_qty"))
		prior_qty = prior_consumed.get((material["item_code"], material["warehouse"]), 0)
		material["available_qty"] = max(normalize_qty(snapshot.get("available_qty")) - prior_qty, 0)
		if not snapshot.get("can_calculate"):
			material["blocked"] = True
			continue

		material["open_material_request_qty"] = _mr_outstanding(
			material["item_code"], material["warehouse"], company, need_by_date
		)
		material["open_purchase_order_qty"] = _po_outstanding(
			material["item_code"], material["warehouse"], company, need_by_date
		)
		material["current_gap_qty"] = max(
			normalize_qty(material["required_qty"]) - material["available_qty"], 0
		)
		material["shortage_qty"] = max(
			material["current_gap_qty"]
			- material["open_material_request_qty"]
			- material["open_purchase_order_qty"],
			0,
		)
		if material["current_gap_qty"] == 0:
			material["status"] = "ready_now"
		elif material["open_purchase_order_qty"] >= material["current_gap_qty"]:
			material["status"] = "awaiting_purchase_receipt"
		elif (
			material["open_material_request_qty"] + material["open_purchase_order_qty"]
			>= material["current_gap_qty"]
		):
			material["status"] = "purchase_request_pending"
		else:
			material["status"] = "new_purchase_required"

	material_rows = sorted(materials.values(), key=lambda material: (material["warehouse"] or "", material["item_code"]))
	return frappe._dict(
		{
			"materials": material_rows,
			"shortages": [
				material
				for material in material_rows
				if material["status"] == "new_purchase_required" and material["shortage_qty"] > 0
			],
		}
	)


def calculate_material_shortages(demands, company: str, defaults=None, need_by_date: str | None = None, prior_demands=None):
	"""Return only material rows requiring a new purchase request."""
	return calculate_material_coverage(demands, company, need_by_date, defaults, prior_demands)["shortages"]


@frappe.whitelist()
def check_shortage(selected_rows, company: str | None = None):
	frappe.has_permission("Material Request", "read", throw=True)
	rows = _selected_rows(selected_rows)
	defaults = get_company_defaults(company)
	company = company or defaults.company
	if not company:
		throw_chinese("默认公司缺失，请先设置公司。")

	demands = []
	selected_items = set()
	boundary_delivery_date = None
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

		selected_items.add(selected.sales_order_item)
		delivery_date = workbench_row.get("delivery_date")
		if delivery_date and (boundary_delivery_date is None or delivery_date > boundary_delivery_date):
			boundary_delivery_date = delivery_date

		demands.append(
			{
				"bom_no": bom_no,
				"qty": demand_qty,
				"source": {
					"sales_order": selected.sales_order,
					"sales_order_item": selected.sales_order_item,
					"finished_item": workbench_row.item_code,
					"qty": demand_qty,
					"sales_order_item_warehouse": workbench_row.get("warehouse"),
				},
			}
		)

	prior_demands = _prior_demands_for(
		company,
		target_delivery_date=boundary_delivery_date,
		exclude_sales_order_items=selected_items,
	)
	shortages = calculate_material_shortages(demands, company, defaults, prior_demands=prior_demands)
	if not shortages:
		return {"shortages": [], "message": "当前选择的订单没有需要采购的缺料。"}
	return {"shortages": shortages}


def _prior_demands_for(company, *, target_delivery_date, exclude_sales_order_items):
	"""Delivery-date-prior production demands that consume shared stock first.

	Imported lazily because ``production`` imports this module at load time.
	"""
	from process_simplification.api.production import get_prior_material_demands

	prior = []
	for demand in get_prior_material_demands(company, target_delivery_date=target_delivery_date):
		source = demand.get("source") or {}
		if source.get("sales_order_item") in exclude_sales_order_items:
			continue
		prior.append(demand)
	return prior


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
