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
	# Production and production-plan reservations are this flow's OWN production
	# demand. The workbench already counts that demand separately, so deducting
	# the matching reservation here would double-count it and keep a well-stocked
	# material perpetually "short". Only sales and subcontract commitments, which
	# are consumed outside this production demand, reduce availability.
	committed_qty = sum(
		max(normalize_qty(bin_row.get(field)), 0)
		for field in (
			"reserved_qty",
			"reserved_qty_for_sub_contract",
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


def _mr_documents(item_code: str, warehouse: str | None, company: str, need_by_date: str | None):
	"""Outstanding purchase Material Request lines for this item/warehouse as
	document rows, so the workbench can show which requests exist and how far
	along they are.

	All outstanding requests are returned regardless of schedule date; each is
	tagged ``is_late`` when it is due after ``need_by_date`` so a request that
	will arrive too late is still visible, just flagged. Quantity aggregation
	(``_mr_outstanding``) still honours the delivery deadline."""
	if not warehouse:
		return []
	mr = frappe.qb.DocType("Material Request")
	mri = frappe.qb.DocType("Material Request Item")
	query = (
		frappe.qb.from_(mri)
		.join(mr)
		.on(mri.parent == mr.name)
		.select(mr.name, mr.status, mri.stock_qty, mri.ordered_qty, mri.schedule_date)
		.where(
			(mri.item_code == item_code)
			& (mri.warehouse == warehouse)
			& (mr.company == company)
			& (mr.docstatus == 1)
			& (mr.material_request_type == "Purchase")
			& (mr.status.notin(["Stopped", "Cancelled"]))
		)
	)

	documents = []
	for row in query.run(as_dict=True):
		outstanding = max(normalize_qty(row.stock_qty) - normalize_qty(row.ordered_qty), 0)
		if outstanding <= 0:
			continue
		documents.append(
			{
				"doctype": "Material Request",
				"name": row.name,
				"status": row.status,
				"outstanding_qty": outstanding,
				"schedule_date": str(row.schedule_date) if row.schedule_date else None,
				"is_late": bool(need_by_date and row.schedule_date and str(row.schedule_date) > str(need_by_date)),
			}
		)
	return documents


def _po_documents(item_code: str, warehouse: str | None, company: str, need_by_date: str | None):
	"""Outstanding Purchase Order lines for this item/warehouse as document rows.

	All outstanding orders are returned regardless of schedule date; each is
	tagged ``is_late`` when due after ``need_by_date``. Quantity aggregation
	(``_po_outstanding``) still honours the delivery deadline."""
	if not warehouse:
		return []
	po = frappe.qb.DocType("Purchase Order")
	poi = frappe.qb.DocType("Purchase Order Item")
	query = (
		frappe.qb.from_(poi)
		.join(po)
		.on(poi.parent == po.name)
		.select(po.name, po.status, poi.stock_qty, poi.received_qty, poi.conversion_factor, poi.schedule_date)
		.where(
			(poi.item_code == item_code)
			& (poi.warehouse == warehouse)
			& (po.company == company)
			& (po.docstatus == 1)
			& (po.status.notin(["Closed", "Cancelled"]))
		)
	)

	documents = []
	for row in query.run(as_dict=True):
		outstanding = max(
			normalize_qty(row.stock_qty)
			- normalize_qty(row.received_qty) * normalize_qty(row.conversion_factor or 1),
			0,
		)
		if outstanding <= 0:
			continue
		documents.append(
			{
				"doctype": "Purchase Order",
				"name": row.name,
				"status": row.status,
				"outstanding_qty": outstanding,
				"schedule_date": str(row.schedule_date) if row.schedule_date else None,
				"is_late": bool(need_by_date and row.schedule_date and str(row.schedule_date) > str(need_by_date)),
			}
		)
	return documents


def _mr_outstanding(item_code: str, warehouse: str | None, company: str, need_by_date: str | None) -> float:
	# On-time outstanding only: a request due after the deadline does not count
	# toward covering this demand's shortage.
	return sum(
		doc["outstanding_qty"]
		for doc in _mr_documents(item_code, warehouse, company, need_by_date)
		if not doc["is_late"]
	)


def _po_outstanding(item_code: str, warehouse: str | None, company: str, need_by_date: str | None) -> float:
	return sum(
		doc["outstanding_qty"]
		for doc in _po_documents(item_code, warehouse, company, need_by_date)
		if not doc["is_late"]
	)


def _intransit_purchase_for_soi(
	item_code: str, warehouse: str | None, company: str, sales_order_item: str | None, need_by_date: str | None
) -> float:
	"""Outstanding purchase attributed to a specific Sales Order Item that has
	not yet been received.

	Received purchase is already reflected in warehouse stock, so only the
	not-yet-received balance is counted here. Purchase Orders are matched by
	their native ``sales_order_item`` (carried over from the Material Request);
	Material Requests not yet converted to a Purchase Order are included too."""
	if not warehouse or not sales_order_item:
		return 0

	total = 0.0
	po = frappe.qb.DocType("Purchase Order")
	poi = frappe.qb.DocType("Purchase Order Item")
	po_query = (
		frappe.qb.from_(poi)
		.join(po)
		.on(poi.parent == po.name)
		.select(poi.stock_qty, poi.received_qty, poi.conversion_factor)
		.where(
			(poi.item_code == item_code)
			& (poi.warehouse == warehouse)
			& (poi.sales_order_item == sales_order_item)
			& (po.company == company)
			& (po.docstatus == 1)
			& (po.status.notin(["Closed", "Cancelled"]))
		)
	)
	if need_by_date:
		po_query = po_query.where(poi.schedule_date <= need_by_date)
	ordered_from_po = 0.0
	for row in po_query.run(as_dict=True):
		outstanding = max(
			normalize_qty(row.stock_qty) - normalize_qty(row.received_qty) * normalize_qty(row.conversion_factor or 1),
			0,
		)
		total += outstanding
		ordered_from_po += normalize_qty(row.stock_qty)

	# Material Requests attributed to this SOI that are not yet converted to a PO
	# (avoid double counting the part already turned into a Purchase Order).
	mr = frappe.qb.DocType("Material Request")
	mri = frappe.qb.DocType("Material Request Item")
	mr_query = (
		frappe.qb.from_(mri)
		.join(mr)
		.on(mri.parent == mr.name)
		.select(mri.stock_qty, mri.ordered_qty)
		.where(
			(mri.item_code == item_code)
			& (mri.warehouse == warehouse)
			& (mri.sales_order_item == sales_order_item)
			& (mr.company == company)
			& (mr.docstatus == 1)
			& (mr.material_request_type == "Purchase")
			& (mr.status.notin(["Stopped", "Cancelled"]))
		)
	)
	if need_by_date:
		mr_query = mr_query.where(mri.schedule_date <= need_by_date)
	for row in mr_query.run(as_dict=True):
		total += max(normalize_qty(row.stock_qty) - normalize_qty(row.ordered_qty), 0)

	return total


def _soi_supply_documents(
	item_code: str, warehouse: str | None, company: str, sales_order_item: str | None, need_by_date: str | None
):
	"""Purchase documents attributed to this Sales Order Item, for display."""
	if not warehouse or not sales_order_item:
		return []
	docs = []
	mr = frappe.qb.DocType("Material Request")
	mri = frappe.qb.DocType("Material Request Item")
	for row in (
		frappe.qb.from_(mri)
		.join(mr)
		.on(mri.parent == mr.name)
		.select(mr.name, mr.status, mri.stock_qty, mri.ordered_qty, mri.schedule_date)
		.where(
			(mri.item_code == item_code)
			& (mri.warehouse == warehouse)
			& (mri.sales_order_item == sales_order_item)
			& (mr.company == company)
			& (mr.docstatus == 1)
			& (mr.material_request_type == "Purchase")
			& (mr.status.notin(["Stopped", "Cancelled"]))
		)
		.run(as_dict=True)
	):
		outstanding = max(normalize_qty(row.stock_qty) - normalize_qty(row.ordered_qty), 0)
		if outstanding <= 0:
			continue
		docs.append(
			{
				"doctype": "Material Request",
				"name": row.name,
				"status": row.status,
				"outstanding_qty": outstanding,
				"schedule_date": str(row.schedule_date) if row.schedule_date else None,
				"is_late": bool(need_by_date and row.schedule_date and str(row.schedule_date) > str(need_by_date)),
			}
		)
	po = frappe.qb.DocType("Purchase Order")
	poi = frappe.qb.DocType("Purchase Order Item")
	for row in (
		frappe.qb.from_(poi)
		.join(po)
		.on(poi.parent == po.name)
		.select(po.name, po.status, poi.stock_qty, poi.received_qty, poi.conversion_factor, poi.schedule_date)
		.where(
			(poi.item_code == item_code)
			& (poi.warehouse == warehouse)
			& (poi.sales_order_item == sales_order_item)
			& (po.company == company)
			& (po.docstatus == 1)
			& (po.status.notin(["Closed", "Cancelled"]))
		)
		.run(as_dict=True)
	):
		outstanding = max(
			normalize_qty(row.stock_qty) - normalize_qty(row.received_qty) * normalize_qty(row.conversion_factor or 1),
			0,
		)
		if outstanding <= 0:
			continue
		docs.append(
			{
				"doctype": "Purchase Order",
				"name": row.name,
				"status": row.status,
				"outstanding_qty": outstanding,
				"schedule_date": str(row.schedule_date) if row.schedule_date else None,
				"is_late": bool(need_by_date and row.schedule_date and str(row.schedule_date) > str(need_by_date)),
			}
		)
	return sorted(docs, key=lambda d: (d.get("schedule_date") or "9999-12-31", d["doctype"], d["name"]))


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
	"""Explain raw-material coverage per order line (Sales Order Item).

	Each demand's BOM is exploded into materials attributed to that demand's
	Sales Order Item. Warehouse stock for a shared ``(item, warehouse)`` is
	allocated across order lines in delivery-date priority, so the earliest-due
	order line consumes on-hand stock first. In-transit (not-yet-received)
	purchase counts toward an order line only when the purchase is attributed to
	that Sales Order Item. A material is ``ready_now`` when its allocated stock
	alone meets the requirement; receipt of an attributed purchase raises
	warehouse stock and turns the line ready on the next recompute.

	``prior_demands`` are other in-flight demands (e.g. earlier-due orders not in
	this call) that must consume shared stock before the reported demands.
	"""
	defaults = defaults or get_company_defaults(company)
	prior_consumed = _prior_required_by_key(prior_demands, company, defaults)

	rows = _expand_material_rows(demands, company, defaults)
	_allocate_stock_by_delivery_priority(rows, prior_consumed)

	for row in rows:
		if row["blocked"]:
			continue
		soi = row["sales_order_item"]
		intransit = _intransit_purchase_for_soi(
			row["item_code"], row["warehouse"], company, soi, need_by_date
		)
		row["intransit_qty"] = intransit
		# Gap after this line's allocated stock; in-transit attributed purchase
		# closes the gap for "awaiting" but does not make it ready (not on hand).
		gap_after_stock = max(normalize_qty(row["required_qty"]) - row["allocated_qty"], 0)
		row["current_gap_qty"] = gap_after_stock
		row["shortage_qty"] = max(gap_after_stock - intransit, 0)
		if gap_after_stock <= 0:
			row["status"] = "ready_now"
		elif intransit >= gap_after_stock:
			row["status"] = "awaiting_purchase_receipt"
		else:
			row["status"] = "new_purchase_required"
		row["supply_documents"] = _soi_supply_documents(
			row["item_code"], row["warehouse"], company, soi, need_by_date
		)

	material_rows = sorted(
		rows, key=lambda r: (r["sales_order_item"] or "", r["warehouse"] or "", r["item_code"])
	)
	return frappe._dict(
		{
			"materials": material_rows,
			"shortages": _merge_shortages_for_purchase(material_rows),
		}
	)


def _expand_material_rows(demands, company: str, defaults):
	"""One row per (Sales Order Item, item, warehouse) with the BOM requirement."""
	rows = []
	for demand in demands or []:
		demand = frappe._dict(demand)
		qty = normalize_qty(demand.get("qty"))
		source = frappe._dict(demand.get("source") or {})
		if not demand.get("bom_no") or qty <= 0:
			continue
		try:
			bom_items = get_bom_items_as_dict(demand.bom_no, company, qty=qty, fetch_exploded=1)
		except Exception as exc:
			raise MaterialCoverageBomExpansionError(demand.bom_no) from exc
		resolved_source = resolve_production_source_warehouse(
			company, defaults=defaults, sales_order_item_warehouse=source.get("sales_order_item_warehouse")
		)
		for bom_item in bom_items.values():
			contribution = normalize_qty(bom_item.get("qty"))
			rows.append(
				{
					"item_code": bom_item.get("item_code"),
					"item_name": bom_item.get("item_name"),
					"stock_uom": bom_item.get("stock_uom"),
					"warehouse": resolved_source.warehouse,
					"row": source.get("row"),
					"sales_order": source.get("sales_order"),
					"sales_order_item": source.get("sales_order_item"),
					"demand_key": source.get("demand_key") or source.get("sales_order_item"),
					"customer_name": source.get("customer_name"),
					"finished_item": source.get("finished_item"),
					"finished_item_name": source.get("finished_item_name"),
					"delivery_date": source.get("delivery_date"),
					"required_qty": contribution,
					"bom_qty_per_unit": contribution / qty if qty else 0,
					"actual_qty": 0,
					"allocated_qty": 0,
					"intransit_qty": 0,
					"current_gap_qty": 0,
					"shortage_qty": 0,
					"status": "cannot_calculate",
					"blocked": not resolved_source.can_use,
				}
			)
	return rows


def _allocate_stock_by_delivery_priority(rows, prior_consumed):
	"""Allocate on-hand stock per (item, warehouse) across order lines, earliest
	delivery date first. ``prior_consumed`` (earlier demands outside this call)
	is deducted from the pool before allocation."""
	pools = {}
	for row in rows:
		if row["blocked"]:
			continue
		key = (row["item_code"], row["warehouse"])
		if key not in pools:
			snapshot = get_material_stock_snapshot(row["item_code"], row["warehouse"])
			if not snapshot.get("can_calculate"):
				row["blocked"] = True
				continue
			available = max(
				normalize_qty(snapshot.get("available_qty")) - normalize_qty(prior_consumed.get(key, 0)), 0
			)
			pools[key] = {"available": available, "actual": normalize_qty(snapshot.get("actual_qty"))}
		row["actual_qty"] = pools[key]["actual"]

	ordered = sorted(
		[r for r in rows if not r["blocked"]],
		key=lambda r: (
			r.get("delivery_date") or "9999-12-31",
			str(r.get("sales_order") or ""),
			str(r.get("sales_order_item") or ""),
		),
	)
	for row in ordered:
		key = (row["item_code"], row["warehouse"])
		pool = pools.get(key)
		if not pool:
			continue
		take = min(pool["available"], normalize_qty(row["required_qty"]))
		row["allocated_qty"] = take
		pool["available"] = max(pool["available"] - take, 0)


def _merge_shortages_for_purchase(material_rows):
	"""Merge order-line shortages back to (item, warehouse) for a consolidated
	purchase, carrying each contributing Sales Order Item as a source so the
	generated Material Request lines stay attributable."""
	merged = {}
	for row in material_rows:
		if row["status"] != "new_purchase_required" or row["shortage_qty"] <= 0:
			continue
		key = (row["item_code"], row["warehouse"])
		if key not in merged:
			merged[key] = {
				"item_code": row["item_code"],
				"item_name": row.get("item_name"),
				"stock_uom": row.get("stock_uom"),
				"warehouse": row["warehouse"],
				"required_qty": 0,
				"shortage_qty": 0,
				"sources": [],
			}
		merged[key]["required_qty"] += normalize_qty(row["required_qty"])
		merged[key]["shortage_qty"] += normalize_qty(row["shortage_qty"])
		merged[key]["sources"].append(
			{
				"sales_order": row.get("sales_order"),
				"sales_order_item": row.get("sales_order_item"),
				"customer_name": row.get("customer_name"),
				"finished_item": row.get("finished_item"),
				"finished_item_name": row.get("finished_item_name"),
				"delivery_date": row.get("delivery_date"),
				"required_qty": normalize_qty(row["shortage_qty"]),
			}
		)
	return sorted(merged.values(), key=lambda m: (m["warehouse"] or "", m["item_code"]))


def is_order_item_ready(coverage, sales_order_item: str) -> bool:
	"""True when every material for the order line has its requirement met by
	allocated on-hand stock (all raw materials present, ready to produce)."""
	rows = [m for m in (coverage or {}).get("materials", []) if m.get("sales_order_item") == sales_order_item]
	if not rows:
		return False
	return all(not r.get("blocked") and r.get("status") == "ready_now" for r in rows)


def calculate_material_shortages(demands, company: str, defaults=None, need_by_date: str | None = None, prior_demands=None):
	"""Return only material rows requiring a new purchase request."""
	return calculate_material_coverage(demands, company, need_by_date, defaults, prior_demands)["shortages"]


def get_all_material_demands(company: str):
	"""All open production demands for the company as material-coverage rows.

	Thin wrapper over ``production.get_all_material_demands`` (imported lazily
	because ``production`` imports this module at load time)."""
	from process_simplification.api.production import get_all_material_demands as _all

	return _all(company)


@frappe.whitelist()
def check_all_shortages(company: str | None = None):
	"""Aggregate raw-material shortage across every open order.

	Pulls all open production demands, so ``calculate_material_coverage`` merges
	each raw material by (item, warehouse) across orders and returns a single
	consolidated shortage line per material for one combined purchase."""
	frappe.has_permission("Material Request", "read", throw=True)
	defaults = get_company_defaults(company)
	company = company or defaults.company
	if not company:
		throw_chinese("默认公司缺失，请先设置公司。")

	demands = get_all_material_demands(company)
	shortages = calculate_material_shortages(demands, company, defaults)
	if not shortages:
		return {"shortages": [], "message": "当前所有订单没有需要采购的缺料。"}
	return {"shortages": shortages}


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

		warehouse = row.get("warehouse") or defaults.source_warehouse
		schedule = row.get("schedule_date") or mr.schedule_date
		note = "流程简化缺料来源：{0}".format(_source_note(row.get("sources") or []))
		# Split the purchase across the originating Sales Order Items so each
		# resulting line is attributable (native sales_order/sales_order_item,
		# which ERPNext carries onto the Purchase Order when the MR is converted).
		for line in _attributed_lines(qty, row.get("sources") or []):
			mr.append(
				"items",
				{
					"item_code": row.item_code,
					"qty": line["qty"],
					"schedule_date": schedule,
					"warehouse": warehouse,
					"sales_order": line.get("sales_order"),
					"sales_order_item": line.get("sales_order_item"),
					"description": note,
				},
			)

	mr.insert()
	mr.submit()
	return {"material_request": mr.name, "docstatus": mr.docstatus}


def _attributed_lines(qty: float, sources) -> list:
	"""Split a purchase quantity across its source Sales Order Items in
	proportion to each source's raw-material requirement.

	Sources without a Sales Order Item, or an empty list, yield one
	unattributed line for the whole quantity. The rounding remainder is added
	to the last attributed line so the split total equals ``qty`` exactly."""
	attributed = [frappe._dict(s) for s in sources if frappe._dict(s).get("sales_order_item")]
	if not attributed:
		return [{"qty": qty, "sales_order": None, "sales_order_item": None}]

	total = sum(normalize_qty(s.get("required_qty")) for s in attributed)
	lines = []
	if total <= 0:
		# No usable weights: put everything on the first source.
		first = attributed[0]
		return [{"qty": qty, "sales_order": first.get("sales_order"), "sales_order_item": first.get("sales_order_item")}]

	allocated = 0.0
	for idx, source in enumerate(attributed):
		if idx == len(attributed) - 1:
			line_qty = normalize_qty(qty - allocated)
		else:
			line_qty = normalize_qty(qty * normalize_qty(source.get("required_qty")) / total)
			allocated += line_qty
		lines.append(
			{
				"qty": line_qty,
				"sales_order": source.get("sales_order"),
				"sales_order_item": source.get("sales_order_item"),
			}
		)
	return lines
