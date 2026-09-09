"""Read-only before/after simulation using the company's purchasing calculation.

No Sales Order, reservation or stock movement is written by this module. Existing
Work Order readiness (including issued/reserved stock and inbound allocation) is
kept; only the unplanned remainder changes when free finished stock is allocated.
"""

from copy import deepcopy

from process_simplification.api.shortage import (
	_aggregate_multilevel_purchased_rows,
	calculate_company_purchase_shortages,
	normalize_purchase_qty,
)
from process_simplification.api.utils import normalize_qty
from process_simplification.api.workbench import allocate_finished_stock, order_item_priority_key

PREVIEW_ORDER = "快速开单预检"


def simulate_order_demands(company, delivery_date, preview_rows, fulfillment, production_demands):
	from process_simplification.api.production import build_production_demand

	orders = {o["name"]: o for o in fulfillment.get("orders") or [] if o.get("company") == company}
	existing = {d["sales_order_item"]: d for d in production_demands if d.get("company") == company}
	rows = [
		dict(r, company=company, sales_order=o["name"], order_creation=o.get("creation"))
		for o in orders.values()
		for r in o.get("rows") or []
	]
	before_rows = {r["sales_order_item"]: deepcopy(r) for r in rows}
	orders[PREVIEW_ORDER] = {"name": PREVIEW_ORDER, "company": company, "creation": "9999-12-31"}
	for row in preview_rows:
		rows.append(
			dict(
				row,
				company=company,
				sales_order=PREVIEW_ORDER,
				sales_order_item=f"预检-{row['row']}",
				sales_order_item_idx=row["row"],
				order_creation="9999-12-31",
				delivery_date=delivery_date,
				pending_qty=row["qty"],
				reserved_qty=0,
				active_work_order_qty=0,
				available_stock_snapshot_qty=row.get(
					"available_stock_snapshot_qty", row.get("available_to_reserve", 0)
				),
				material_status="未检查" if row.get("bom_no") else "不涉及生产",
			)
		)
	allocated = allocate_finished_stock(rows)
	result = []
	for row in allocated:
		old = existing.get(row["sales_order_item"], {})
		demand = build_production_demand(orders[row["sales_order"]], row, old.get("work_orders"))
		if demand:
			# Preserve the authoritative plan graph; do not recreate planned need from BOM.
			demand["production_plans"] = deepcopy(old.get("production_plans") or [])
			result.append(demand)
	return result, before_rows, {r["sales_order_item"]: r for r in allocated}


def _source_shortages(shortages):
	result = {}
	for material in shortages:
		for source in material.get("sources") or []:
			key = (source.get("sales_order_item"), material.get("item_code"), material.get("warehouse"))
			row = result.setdefault(key, dict(material, sources=[], shortage_qty=0))
			row["shortage_qty"] += normalize_qty(source.get("shortage_qty"))
	return result


def compare_order_impacts(before, after, before_rows, after_rows):
	previous = _source_shortages(before)
	current = _source_shortages(after)
	impacts = {}
	for item_id, old in before_rows.items():
		new = after_rows[item_id]
		lost_stock = max(
			normalize_purchase_qty(
				normalize_qty(old.get("available_to_reserve"))
				- normalize_qty(new.get("available_to_reserve"))
			),
			0,
		)
		materials = []
		for key, material in current.items():
			if key[0] != item_id:
				continue
			before_qty = normalize_purchase_qty(previous.get(key, {}).get("shortage_qty", 0))
			after_qty = normalize_purchase_qty(material["shortage_qty"])
			delta = normalize_purchase_qty(after_qty - before_qty)
			if delta > 0:
				materials.append(
					{k: material.get(k) for k in ["item_code", "item_name", "warehouse", "stock_uom"]}
					| {
						"before_shortage_qty": before_qty,
						"after_shortage_qty": after_qty,
						"added_shortage_qty": delta,
					}
				)
		if materials or lost_stock:
			impacts[item_id] = {
				k: old.get(k)
				for k in ["sales_order", "sales_order_item", "item_code", "item_name", "delivery_date"]
			} | {
				"lost_finished_stock_qty": lost_stock,
				"materials": sorted(materials, key=lambda m: (m["item_code"], m.get("warehouse") or "")),
			}
	return sorted(impacts.values(), key=order_item_priority_key)


def evaluate_order_material_risk(company, delivery_date, preview_rows):
	from process_simplification.api.production import get_production_overview
	from process_simplification.api.workbench import get_fulfillment_overview

	fulfillment = get_fulfillment_overview(page_size=0)
	baseline = [
		d for d in get_production_overview(page_size=0).get("demands") or [] if d.get("company") == company
	]
	simulated, before_rows, after_rows = simulate_order_demands(
		company, delivery_date, preview_rows, fulfillment, baseline
	)
	before = calculate_company_purchase_shortages(company, production_demands=baseline)
	coverage = {}
	after = calculate_company_purchase_shortages(
		company, production_demands=simulated, coverage_result=coverage
	)
	own_requirements = []
	for requirement in coverage.get("requirements") or []:
		if not any(s.get("sales_order") == PREVIEW_ORDER for s in requirement.get("sources") or []):
			continue
		row = deepcopy(requirement)
		for source in row["sources"]:
			source["row"] = int(source["sales_order_item"].removeprefix("预检-"))
		own_requirements.append(row)
	materials = _aggregate_multilevel_purchased_rows(
		[r for r in own_requirements if r.get("supply_type") == "purchased"]
	)
	return {
		"downstream_impacts": compare_order_impacts(before, after, before_rows, after_rows),
		"coverage": {
			"requirements": own_requirements,
			"materials": materials,
			"shortages": [r for r in materials if r.get("shortage_qty", 0) > 0],
		},
	}
