from __future__ import annotations

from copy import deepcopy

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
				"production_committed_qty": 0,
				"available_qty": 0,
				"free_qty": 0,
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
			"reserved_qty_for_sub_contract",
		)
	)
	production_committed_qty = sum(
		max(normalize_qty(bin_row.get(field)), 0)
		for field in (
			"reserved_qty_for_production",
			"reserved_qty_for_production_plan",
		)
	)
	# Keep the base stock figure for display, but expose genuinely free stock
	# separately. Production readiness adds each loaded Work Order's own
	# stock_reserved_qty back to that Work Order instead of sharing it globally.
	available_qty = max(actual_qty - committed_qty, 0)
	return frappe._dict(
		{
			"can_calculate": True,
			"actual_qty": actual_qty,
			"committed_qty": committed_qty,
			"production_committed_qty": production_committed_qty,
			"available_qty": available_qty,
			"free_qty": max(available_qty - production_committed_qty, 0),
		}
	)


def _snapshot_free_qty(snapshot) -> float:
	snapshot = frappe._dict(snapshot or {})
	if snapshot.get("free_qty") is not None:
		return max(normalize_qty(snapshot.get("free_qty")), 0)
	return max(normalize_qty(snapshot.get("available_qty")), 0)


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
		.select(mr.name, mri.name.as_("detail_name"), mr.status, mri.stock_qty, mri.ordered_qty, mri.schedule_date)
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
				"detail_name": row.detail_name,
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
		.select(po.name, poi.name.as_("detail_name"), po.status, poi.stock_qty, poi.received_qty, poi.conversion_factor, poi.schedule_date)
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
				"detail_name": row.detail_name,
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


def _coverage_defaults(company: str, defaults, fact_cache):
	if defaults:
		return defaults
	if fact_cache is None:
		return get_company_defaults(company)
	defaults_by_company = fact_cache.setdefault("company_defaults", {})
	if company not in defaults_by_company:
		defaults_by_company[company] = get_company_defaults(company)
	return defaults_by_company[company]


def _coverage_stock_snapshot(item_code: str, warehouse: str | None, company: str, fact_cache):
	if fact_cache is None:
		return get_material_stock_snapshot(item_code, warehouse)
	stock_snapshots = fact_cache.setdefault("stock_snapshots", {})
	key = (company, item_code, warehouse)
	if key not in stock_snapshots:
		stock_snapshots[key] = get_material_stock_snapshot(item_code, warehouse)
	return stock_snapshots[key]


def _coverage_supply_documents(
	item_code: str,
	warehouse: str | None,
	company: str,
	need_by_date: str | None,
	fact_cache,
):
	if fact_cache is None:
		return (
			_mr_documents(item_code, warehouse, company, need_by_date),
			_po_documents(item_code, warehouse, company, need_by_date),
		)

	supply_documents = fact_cache.setdefault("supply_documents", {})
	key = (company, item_code, warehouse)
	if key not in supply_documents:
		supply_documents[key] = {
			"material_requests": [
				dict(row) for row in _mr_documents(item_code, warehouse, company, None)
			],
			"purchase_orders": [dict(row) for row in _po_documents(item_code, warehouse, company, None)],
		}

	def for_deadline(rows):
		documents = deepcopy(rows)
		for document in documents:
			schedule_date = document.get("schedule_date")
			document["is_late"] = bool(
				need_by_date and schedule_date and str(schedule_date) > str(need_by_date)
			)
		return documents

	cached = supply_documents[key]
	return for_deadline(cached["material_requests"]), for_deadline(cached["purchase_orders"])


def calculate_material_coverage(
	demands,
	company: str,
	need_by_date: str | None = None,
	defaults=None,
	prior_demands=None,
	*,
	prior_consumed=None,
	fact_cache=None,
) -> frappe._dict:
	"""Explain material availability, approved supply, and new purchase needs.

	``prior_demands`` are other in-flight demands that draw on the same shared
	stock; their raw-material need is consumed from free stock before the
	reported ``demands`` are evaluated, so a per-order check does not let every
	order believe the same scarce stock covers it. Sequential callers can pass
	pre-aggregated ``prior_consumed`` and a shared ``fact_cache`` to avoid
	re-expanding earlier BOMs or rereading identical stock and supply facts.
	"""
	defaults = _coverage_defaults(company, defaults, fact_cache)
	if prior_consumed is None:
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
					"supply_documents": [],
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
		snapshot = _coverage_stock_snapshot(
			material["item_code"], material["warehouse"], company, fact_cache
		)
		material["actual_qty"] = normalize_qty(snapshot.get("actual_qty"))
		material["committed_qty"] = normalize_qty(snapshot.get("committed_qty"))
		prior_qty = prior_consumed.get((material["item_code"], material["warehouse"]), 0)
		material["available_qty"] = max(_snapshot_free_qty(snapshot) - prior_qty, 0)
		if not snapshot.get("can_calculate"):
			material["blocked"] = True
			continue

		mr_docs, po_docs = _coverage_supply_documents(
			material["item_code"], material["warehouse"], company, need_by_date, fact_cache
		)
		# Summary quantities count only on-time supply (keeps the shortage math
		# tied to the delivery deadline); the document list shows all, flagging
		# late ones.
		material["open_material_request_qty"] = sum(d["outstanding_qty"] for d in mr_docs if not d["is_late"])
		material["open_purchase_order_qty"] = sum(d["outstanding_qty"] for d in po_docs if not d["is_late"])
		material["supply_documents"] = sorted(
			mr_docs + po_docs,
			key=lambda doc: (doc.get("schedule_date") or "9999-12-31", doc["doctype"], doc["name"]),
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


def _direct_bom_items(bom_no: str, company: str, fact_cache) -> list:
	cache = fact_cache.setdefault("direct_bom_items", {})
	key = (company, bom_no)
	if key not in cache:
		try:
			cache[key] = [
				dict(row)
				for row in get_bom_items_as_dict(
					bom_no,
					company,
					qty=1,
					fetch_exploded=0,
				).values()
			]
		except Exception as exc:
			raise MaterialCoverageBomExpansionError(bom_no) from exc
	return [frappe._dict(deepcopy(row)) for row in cache[key]]


def _manufacturing_bom_for_item(item, fact_cache) -> str | None:
	if item.get("bom_no"):
		return item.get("bom_no")
	item_code = item.get("item_code")
	default_boms = fact_cache.setdefault("default_boms", {})
	if item_code not in default_boms:
		default_boms[item_code] = get_default_bom(item_code)
	return default_boms[item_code]


def _allocate_multilevel_supply(
	row,
	company: str,
	need_by_date: str | None,
	fact_cache,
	remaining_supply,
):
	mr_docs, po_docs = _coverage_supply_documents(
		row.item_code,
		row.warehouse,
		company,
		need_by_date,
		fact_cache,
	)
	uncovered = normalize_qty(row.current_gap_qty)
	allocated_purchase_orders = 0
	allocated_material_requests = 0
	documents = []
	for doctype, source_documents in (
		("Purchase Order", po_docs),
		("Material Request", mr_docs),
	):
		for source_document in sorted(
			source_documents,
			key=lambda document: (
				document.get("schedule_date") or "9999-12-31",
				document.get("name") or "",
				document.get("detail_name") or "",
			),
		):
			document = frappe._dict(deepcopy(dict(source_document)))
			document_key = (
				row.item_code,
				row.warehouse,
				doctype,
				document.get("detail_name") or document.get("name"),
			)
			available_supply = remaining_supply.setdefault(
				document_key, normalize_qty(document.get("outstanding_qty"))
			)
			allocated = 0
			if not document.get("is_late"):
				allocated = min(uncovered, available_supply)
				remaining_supply[document_key] = max(available_supply - allocated, 0)
				uncovered = max(uncovered - allocated, 0)
			document.allocated_qty = allocated
			documents.append(document)
			if doctype == "Purchase Order":
				allocated_purchase_orders += allocated
			else:
				allocated_material_requests += allocated

	row.open_purchase_order_qty = allocated_purchase_orders
	row.open_material_request_qty = allocated_material_requests
	row.supply_documents = documents
	row.shortage_qty = uncovered
	if row.current_gap_qty == 0:
		row.status = "ready_now"
	elif allocated_purchase_orders >= row.current_gap_qty:
		row.status = "awaiting_purchase_receipt"
	elif allocated_purchase_orders + allocated_material_requests >= row.current_gap_qty:
		row.status = "purchase_request_pending"
	else:
		row.status = "new_purchase_required"


def _aggregate_multilevel_purchased_rows(rows) -> list:
	materials = {}
	for source_row in rows:
		key = (source_row.get("item_code"), source_row.get("warehouse"))
		material = materials.setdefault(
			key,
			{
				"item_code": source_row.get("item_code"),
				"item_name": source_row.get("item_name"),
				"stock_uom": source_row.get("stock_uom"),
				"warehouse": source_row.get("warehouse"),
				"supply_type": "purchased",
				"required_qty": 0,
				"actual_qty": 0,
				"committed_qty": 0,
				"available_qty": 0,
				"open_material_request_qty": 0,
				"open_purchase_order_qty": 0,
				"current_gap_qty": 0,
				"shortage_qty": 0,
				"blocked": False,
				"sources": [],
				"supply_documents": [],
				"_documents": {},
			},
		)
		material["item_name"] = material.get("item_name") or source_row.get("item_name")
		material["stock_uom"] = material.get("stock_uom") or source_row.get("stock_uom")
		for field in (
			"required_qty",
			"available_qty",
			"open_material_request_qty",
			"open_purchase_order_qty",
			"current_gap_qty",
			"shortage_qty",
		):
			material[field] += normalize_qty(source_row.get(field))
		material["actual_qty"] = max(
			normalize_qty(material.get("actual_qty")), normalize_qty(source_row.get("actual_qty"))
		)
		material["committed_qty"] = max(
			normalize_qty(material.get("committed_qty")),
			normalize_qty(source_row.get("committed_qty")),
		)
		material["blocked"] = bool(material.get("blocked") or source_row.get("blocked"))
		material["sources"].extend(deepcopy(source_row.get("sources") or []))
		for source_document in source_row.get("supply_documents") or []:
			document_key = (
				source_document.get("doctype"),
				source_document.get("detail_name") or source_document.get("name"),
			)
			if document_key not in material["_documents"]:
				material["_documents"][document_key] = {
					**dict(source_document),
					"allocated_qty": 0,
				}
			document = material["_documents"][document_key]
			document["allocated_qty"] += normalize_qty(source_document.get("allocated_qty"))
			document["outstanding_qty"] = max(
				normalize_qty(document.get("outstanding_qty")),
				normalize_qty(source_document.get("outstanding_qty")),
			)

	result = []
	for material in materials.values():
		material["supply_documents"] = list(material.pop("_documents").values())
		if material.get("blocked"):
			material["status"] = "cannot_calculate"
		elif material["current_gap_qty"] == 0:
			material["status"] = "ready_now"
		elif material["shortage_qty"] > 0:
			material["status"] = "new_purchase_required"
		elif material["open_purchase_order_qty"] >= material["current_gap_qty"]:
			material["status"] = "awaiting_purchase_receipt"
		else:
			material["status"] = "purchase_request_pending"
		result.append(frappe._dict(material))
	return sorted(result, key=lambda material: (material.get("warehouse") or "", material.item_code))


def calculate_multilevel_material_coverage(
	demands,
	company: str,
	need_by_date: str | None = None,
	defaults=None,
	prior_demands=None,
	*,
	fact_cache=None,
) -> frappe._dict:
	"""Net a multi-level BOM one stock-managed level at a time.

	Manufactured children consume their own stock first. Only the uncovered
	quantity is expanded into the child's direct BOM requirements. Purchased
	leaves are the only rows included in the procurement summary.
	"""
	fact_cache = fact_cache if fact_cache is not None else {}
	defaults = _coverage_defaults(company, defaults, fact_cache)
	remaining_stock = {}
	remaining_supply = {}
	requirements = []
	purchased_rows = []

	def walk_bom(demand, *, capture: bool):
		demand = frappe._dict(demand or {})
		root_qty = normalize_qty(demand.get("qty"))
		if not demand.get("bom_no") or root_qty <= 0:
			return
		resolved_source = resolve_production_source_warehouse(
			company,
			defaults=defaults,
			sales_order_item_warehouse=(demand.get("source") or {}).get(
				"sales_order_item_warehouse"
			),
		)
		root_source = dict(demand.get("source") or {})
		root_source.setdefault("production_qty", root_qty)

		def walk(bom_no, qty, parent_item_code, level, path):
			if bom_no in path:
				raise MaterialCoverageBomExpansionError(bom_no)
			current_path = (*path, bom_no)
			for bom_item in _direct_bom_items(bom_no, company, fact_cache):
				required_qty = normalize_qty(bom_item.get("qty")) * normalize_qty(qty)
				if required_qty <= 0:
					continue
				item_code = bom_item.get("item_code")
				warehouse = resolved_source.warehouse
				key = (item_code, warehouse)
				snapshot = _coverage_stock_snapshot(item_code, warehouse, company, fact_cache)
				available_stock = remaining_stock.setdefault(
					key, _snapshot_free_qty(snapshot)
				)
				allocated_stock = min(required_qty, available_stock)
				remaining_stock[key] = max(available_stock - allocated_stock, 0)
				child_bom = _manufacturing_bom_for_item(bom_item, fact_cache)
				source = deepcopy(root_source)
				source.update(
					{
						"required_qty": required_qty,
						"bom_qty_per_unit": required_qty / root_qty,
						"level": level,
						"parent_item_code": parent_item_code,
					}
				)
				row = frappe._dict(
					{
						"item_code": item_code,
						"item_name": bom_item.get("item_name"),
						"stock_uom": bom_item.get("stock_uom"),
						"warehouse": warehouse,
						"required_qty": required_qty,
						"actual_qty": normalize_qty(snapshot.get("actual_qty")),
						"committed_qty": normalize_qty(snapshot.get("committed_qty")),
						"available_qty": allocated_stock,
						"current_gap_qty": max(required_qty - allocated_stock, 0),
						"open_material_request_qty": 0,
						"open_purchase_order_qty": 0,
						"shortage_qty": 0,
						"supply_documents": [],
						"blocked": not (
							resolved_source.can_use and snapshot.get("can_calculate")
						),
						"supply_type": "manufactured" if child_bom else "purchased",
						"bom_no": child_bom,
						"parent_item_code": parent_item_code,
						"level": level,
						"sources": [source],
					}
				)
				if row.blocked:
					row.status = "cannot_calculate"
					row.production_required_qty = 0
				elif child_bom:
					row.production_required_qty = row.current_gap_qty
					row.status = "production_required" if row.current_gap_qty > 0 else "ready_now"
				else:
					row.production_required_qty = 0
					_allocate_multilevel_supply(
						row,
						company,
						need_by_date,
						fact_cache,
						remaining_supply,
					)
				if capture:
					requirements.append(row)
					if row.supply_type == "purchased":
						purchased_rows.append(row)
				if child_bom and not row.blocked and row.production_required_qty > 0:
					walk(child_bom, row.production_required_qty, item_code, level + 1, current_path)

		walk(demand.get("bom_no"), root_qty, root_source.get("finished_item"), 1, ())

	for prior_demand in prior_demands or []:
		walk_bom(prior_demand, capture=False)
	for demand in demands or []:
		walk_bom(demand, capture=True)

	materials = _aggregate_multilevel_purchased_rows(purchased_rows)
	return frappe._dict(
		{
			"requirements": requirements,
			"materials": materials,
			"shortages": [
				material
				for material in materials
				if material.status == "new_purchase_required" and material.shortage_qty > 0
			],
		}
	)


def calculate_material_shortages(demands, company: str, defaults=None, need_by_date: str | None = None, prior_demands=None):
	"""Return only material rows requiring a new purchase request."""
	return calculate_material_coverage(demands, company, need_by_date, defaults, prior_demands)["shortages"]


def calculate_plan_purchase_shortages(readiness_by_sales_order_item, selected_sales_order_items=None):
	"""Aggregate only leaf purchased items from Production Plan Work Orders."""
	selected_sales_order_items = set(selected_sales_order_items or [])
	materials = {}
	seen_work_orders = set()
	for mapped_sales_order_item, plans in (readiness_by_sales_order_item or {}).items():
		if selected_sales_order_items and mapped_sales_order_item not in selected_sales_order_items:
			continue
		for plan in plans or []:
			for work_order in plan.get("work_orders") or []:
				work_order_sales_order_item = (
					work_order.get("sales_order_item") or mapped_sales_order_item
				)
				if (
					selected_sales_order_items
					and work_order_sales_order_item not in selected_sales_order_items
				):
					continue
				work_order_key = (plan.get("name"), work_order.get("name"))
				if work_order_key in seen_work_orders:
					continue
				seen_work_orders.add(work_order_key)
				for item in work_order.get("required_items") or []:
					if (
						item.get("supply_type") != "purchased"
						or item.get("status") != "new_purchase_required"
						or normalize_qty(item.get("shortage_qty")) <= 0
					):
						continue
					key = (item.get("item_code"), item.get("source_warehouse"))
					material = materials.setdefault(
						key,
						{
							"company": plan.get("company"),
							"item_code": item.get("item_code"),
							"item_name": item.get("item_name"),
							"stock_uom": item.get("stock_uom"),
							"warehouse": item.get("source_warehouse"),
							"required_qty": 0,
							"actual_qty": 0,
							"committed_qty": 0,
							"available_qty": 0,
							"open_material_request_qty": 0,
							"open_purchase_order_qty": 0,
							"current_gap_qty": 0,
							"shortage_qty": 0,
							"status": "new_purchase_required",
							"blocked": False,
							"sources": [],
							"supply_documents": [],
						},
					)
					material["required_qty"] += normalize_qty(item.get("required_qty"))
					material["actual_qty"] = max(
						material["actual_qty"], normalize_qty(item.get("actual_qty"))
					)
					material["available_qty"] += normalize_qty(item.get("available_qty"))
					material["open_material_request_qty"] += normalize_qty(
						item.get("open_material_request_qty")
					)
					material["open_purchase_order_qty"] += normalize_qty(
						item.get("open_purchase_order_qty")
					)
					material["current_gap_qty"] += normalize_qty(item.get("current_gap_qty"))
					material["shortage_qty"] += normalize_qty(item.get("shortage_qty"))
					material["sources"].append(
						{
							"production_plan": plan.get("name"),
							"planned_date": plan.get("planned_date"),
							"work_order": work_order.get("name"),
							"sales_order": work_order.get("sales_order"),
							"sales_order_item": work_order_sales_order_item,
							"finished_item": work_order.get("production_item"),
							"required_qty": normalize_qty(item.get("required_qty")),
							"current_gap_qty": normalize_qty(item.get("current_gap_qty")),
							"shortage_qty": normalize_qty(item.get("shortage_qty")),
						}
					)
	return sorted(materials.values(), key=lambda row: (row.get("warehouse") or "", row.get("item_code") or ""))


def get_all_material_demands(company: str):
	"""All open production demands for the company as material-coverage rows.

	Thin wrapper over ``production.get_all_material_demands`` (imported lazily
	because ``production`` imports this module at load time)."""
	from process_simplification.api.production import get_all_material_demands as _all

	return _all(company)


@frappe.whitelist()
def check_all_shortages(company: str | None = None):
	"""Aggregate leaf raw-material shortage across Production Plan Work Orders."""
	frappe.has_permission("Material Request", "read", throw=True)
	defaults = get_company_defaults(company)
	company = company or defaults.company
	if not company:
		throw_chinese("默认公司缺失，请先设置公司。")

	from process_simplification.api.production_readiness import get_production_plan_readiness

	readiness = get_production_plan_readiness(company=company)
	shortages = calculate_plan_purchase_shortages(readiness)
	if not shortages:
		return {"shortages": [], "message": "当前所有订单没有需要采购的缺料。"}
	return {"shortages": shortages}


@frappe.whitelist()
def check_shortage(selected_rows, company: str | None = None):
	frappe.has_permission("Material Request", "read", throw=True)
	rows = _selected_rows(selected_rows)
	validated_rows = [
		_workbench_row(row.get("sales_order"), row.get("sales_order_item")) for row in rows
	]
	selected_companies = {row.get("company") for row in validated_rows if row.get("company")}
	if len(selected_companies) > 1:
		throw_chinese("一次只能检查同一公司的订单明细。")
	selected_company = next(iter(selected_companies), None)
	defaults = get_company_defaults(company or selected_company)
	company = company or selected_company or defaults.company
	if not company:
		throw_chinese("默认公司缺失，请先设置公司。")
	if any(row.get("company") != company for row in validated_rows):
		throw_chinese("所选订单明细不属于当前公司。")
	selected_items = {
		row.get("sales_order_item") for row in validated_rows if row.get("sales_order_item")
	}
	from process_simplification.api.production_readiness import get_production_plan_readiness

	# Load every company plan first so non-selected earlier plans consume shared
	# stock and inbound supply before the selected plans are reported.
	readiness = get_production_plan_readiness(company=company)
	shortages = calculate_plan_purchase_shortages(readiness, selected_items)
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


def _purchase_source_key(source):
	source = frappe._dict(source or {})
	return (
		source.get("production_plan") or "",
		source.get("work_order") or "",
		source.get("sales_order_item") or "",
	)


def _requested_purchase_qty(row):
	"""Keep an explicitly entered zero/negative quantity for validation.

	Older callers may omit ``purchase_qty`` and rely on ``shortage_qty`` as the
	default.  Once the field is present, however, a falsy value is user input and
	must not silently fall back to the suggested shortage.
	"""
	return normalize_qty(
		row.get("purchase_qty") if "purchase_qty" in row else row.get("shortage_qty")
	)


def revalidate_purchase_rows(shortage_rows, current_shortages):
	current_by_key = {
		(row.get("item_code"), row.get("warehouse")): frappe._dict(row)
		for row in current_shortages or []
	}
	validated = []
	for index, source_row in enumerate(shortage_rows or [], start=1):
		row = frappe._dict(deepcopy(dict(source_row)))
		current = current_by_key.get((row.get("item_code"), row.get("warehouse")))
		requested_source_keys = {
			_purchase_source_key(source)
			for source in row.get("sources") or []
			if any(_purchase_source_key(source))
		}
		if not requested_source_keys:
			throw_chinese("第 {0} 行缺少可复核的订单来源，请刷新后重试。".format(index))

		matching_sources = [
			frappe._dict(deepcopy(dict(source)))
			for source in (current or {}).get("sources") or []
			if _purchase_source_key(source) in requested_source_keys
		]
		current_shortage_qty = sum(
			normalize_qty(source.get("shortage_qty")) for source in matching_sources
		)
		purchase_qty = _requested_purchase_qty(row)
		if current_shortage_qty <= 0:
			throw_chinese("第 {0} 行已不再缺料，请刷新后重试。".format(index))
		if purchase_qty <= 0:
			throw_chinese("第 {0} 行采购数量必须大于 0。".format(index))
		if purchase_qty > current_shortage_qty:
			throw_chinese(
				"第 {0} 行采购数量超过最新采购缺口 {1}，请刷新后重试。".format(
					index, current_shortage_qty
				)
			)
		row.shortage_qty = current_shortage_qty
		row.sources = matching_sources
		validated.append(row)
	return validated


@frappe.whitelist(methods=["POST"])
def create_material_request(shortage_rows, company: str | None = None, schedule_date: str | None = None):
	frappe.has_permission("Material Request", "create", throw=True)
	shortage_rows = _parse(shortage_rows) or []
	if not shortage_rows:
		throw_chinese("请至少选择一条缺料记录。")

	row_companies = {row.get("company") for row in shortage_rows if row.get("company")}
	if len(row_companies) > 1:
		throw_chinese("一次只能为同一公司生成采购申请。")
	row_company = next(iter(row_companies), None)
	defaults = get_company_defaults(company or row_company)
	company = company or row_company or defaults.company
	if not company:
		throw_chinese("默认公司缺失，请先设置公司。")
	if any(row.get("company") and row.get("company") != company for row in shortage_rows):
		throw_chinese("缺料记录不属于当前公司。")
	from process_simplification.api.production_readiness import get_production_plan_readiness
	selected_sales_order_items = sorted(
		{
			source.get("sales_order_item")
			for row in shortage_rows
			for source in (row.get("sources") or [])
			if source.get("sales_order_item")
		}
	)
	if not selected_sales_order_items:
		throw_chinese("缺料记录缺少可复核的订单来源，请刷新后重试。")

	current_shortages = calculate_plan_purchase_shortages(
		get_production_plan_readiness(
			company=company,
			sales_order_items=selected_sales_order_items,
		),
		selected_sales_order_items,
	)
	shortage_rows = revalidate_purchase_rows(shortage_rows, current_shortages)

	mr = frappe.new_doc("Material Request")
	mr.material_request_type = "Purchase"
	mr.company = company
	mr.transaction_date = nowdate()
	mr.schedule_date = schedule_date or add_days(nowdate(), 1)

	for index, row in enumerate(shortage_rows, start=1):
		row = frappe._dict(row)
		qty = _requested_purchase_qty(row)
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
