"""Stock-UOM and lot-aware, submitted-ledger availability for production returns.

A route keeps the actual item, BOM component and original issue warehouse. Native
Work Order child totals cannot distinguish operation duplicates or alternatives.
"""

from collections import defaultdict
import hashlib
import frappe
from frappe.utils import flt

EPSILON = 1e-8


def route_key(item, wip, source, original=None):
	parts = [item or "", wip or "", source or ""]
	if original and original != item:
		parts.append(original)
	return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _fragments(row, bundles):
	if row.get("serial_and_batch_bundle"):
		return [
			(r.get("batch_no"), r.get("serial_no"), abs(flt(r.qty)))
			for r in bundles.get(row.serial_and_batch_bundle, [])
		]
	serials = str(row.get("serial_no") or "").split()
	if serials:
		return [(row.get("batch_no"), serial, 1.0) for serial in serials]
	return [(row.get("batch_no"), None, flt(row.get("transfer_qty")))]


def aggregate_routes(entries, details, bundles=None):
	"""Allocate unlabelled consumption FIFO; batch/serial movements match exactly."""
	entries = {r.name: frappe._dict(r) for r in entries}
	routes = {}
	balances = []
	for raw in details:
		row = frappe._dict(raw)
		entry = entries.get(row.parent)
		if not entry or entry.docstatus != 1:
			continue
		original = row.get("original_item") or row.item_code
		issue = entry.purpose == "Material Transfer for Manufacture" and not entry.is_return
		consumed = (
			entry.purpose in {"Manufacture", "Material Consumption for Manufacture"} and row.s_warehouse
		)
		returned = entry.purpose == "Material Transfer for Manufacture" and entry.is_return
		if issue and row.s_warehouse and row.t_warehouse:
			key = route_key(row.item_code, row.t_warehouse, row.s_warehouse, original)
			route = routes.setdefault(
				key,
				frappe._dict(
					key=key,
					requirement_key=route_key(original, row.t_warehouse, row.s_warehouse),
					item_code=row.item_code,
					original_item=original,
					stock_uom=row.stock_uom,
					source_warehouse=row.t_warehouse,
					return_warehouse=row.s_warehouse,
					issued_qty=0.0,
					returned_qty=0.0,
					lots=[],
				),
			)
			for batch, serial, qty in _fragments(row, bundles or {}):
				route.issued_qty += qty
				fragment = frappe._dict(route=key, batch_no=batch, serial_no=serial, qty=qty)
				balances.append(fragment)
				route.lots.append(fragment)
		elif consumed or returned:
			if consumed and not any(
				r.source_warehouse == row.s_warehouse and r.item_code == row.item_code
				for r in routes.values()
			):
				continue
			source = (
				(row.get("custom_return_source_warehouse") or entry.get("custom_return_source_warehouse"))
				if returned
				else None
			)
			# Legacy normal returns identify their source by the destination.
			if (
				returned
				and not source
				and any(r.return_warehouse == row.t_warehouse for r in routes.values())
			):
				source = row.t_warehouse
			for batch, serial, qty in _fragments(row, bundles or {}):
				candidates = [
					b
					for b in balances
					if b.qty > EPSILON
					and routes[b.route].item_code == row.item_code
					and routes[b.route].original_item == original
					and routes[b.route].source_warehouse == row.s_warehouse
					and (not source or routes[b.route].return_warehouse == source)
					and (not batch or b.batch_no == batch)
					and (not serial or b.serial_no == serial)
				]
				if returned and not source and len({b.route for b in candidates}) > 1:
					frappe.throw("历史退料存在多个原发料来源，请先核对退料来源，不能自动分摊。")
				for balance in candidates:
					take = min(qty, balance.qty)
					balance.qty -= take
					qty -= take
					if returned:
						routes[balance.route].returned_qty += take
					if qty <= EPSILON:
						break
				if qty > EPSILON:
					frappe.throw("工单物料流水的退料或耗用超过对应来源，请先核对库存记录。")
	for route in routes.values():
		route.lots = [b for b in route.lots if b.qty > EPSILON]
		route.native_available_qty = sum(b.qty for b in route.lots)
	return list(routes.values())


def load_routes(work_order, for_update=False):
	entries = frappe.get_all(
		"Stock Entry",
		filters={
			"work_order": work_order,
			"docstatus": 1,
			"purpose": [
				"in",
				["Material Transfer for Manufacture", "Manufacture", "Material Consumption for Manufacture"],
			],
		},
		fields=[
			"name",
			"purpose",
			"is_return",
			"docstatus",
			"custom_return_source_warehouse",
			"posting_date",
			"posting_time",
			"creation",
		],
		order_by="posting_date asc, posting_time asc, creation asc, name asc",
		limit=0,
	)
	if for_update:
		table = frappe.qb.DocType("Stock Entry")
		entries = (
			frappe.qb.from_(table)
			.select(
				table.name,
				table.purpose,
				table.is_return,
				table.docstatus,
				table.custom_return_source_warehouse,
				table.posting_date,
				table.posting_time,
				table.creation,
			)
			.where(
				(table.work_order == work_order)
				& (table.docstatus == 1)
				& table.purpose.isin(
					[
						"Material Transfer for Manufacture",
						"Manufacture",
						"Material Consumption for Manufacture",
					]
				)
			)
			.orderby(table.posting_date, table.posting_time, table.creation, table.name)
			.for_update()
			.run(as_dict=True)
		)
	if not entries:
		return []
	details = frappe.get_all(
		"Stock Entry Detail",
		filters={"parent": ["in", [e.name for e in entries]], "docstatus": 1},
		fields=[
			"parent",
			"idx",
			"item_code",
			"original_item",
			"s_warehouse",
			"t_warehouse",
			"stock_uom",
			"transfer_qty",
			"batch_no",
			"serial_no",
			"serial_and_batch_bundle",
			"custom_return_source_warehouse",
		],
		limit=0,
	)
	if for_update:
		table = frappe.qb.DocType("Stock Entry Detail")
		details = (
			frappe.qb.from_(table)
			.select(
				table.parent,
				table.idx,
				table.item_code,
				table.original_item,
				table.s_warehouse,
				table.t_warehouse,
				table.stock_uom,
				table.transfer_qty,
				table.batch_no,
				table.serial_no,
				table.serial_and_batch_bundle,
				table.custom_return_source_warehouse,
			)
			.where(table.parent.isin([e.name for e in entries]) & (table.docstatus == 1))
			.for_update()
			.run(as_dict=True)
		)
	order = {e.name: i for i, e in enumerate(entries)}
	details.sort(key=lambda d: (order[d.parent], d.idx))
	bundle_names = list({d.serial_and_batch_bundle for d in details if d.serial_and_batch_bundle})
	bundles = defaultdict(list)
	if bundle_names:
		for row in frappe.get_all(
			"Serial and Batch Entry",
			filters={"parent": ["in", bundle_names]},
			fields=["parent", "batch_no", "serial_no", "qty"],
			order_by="idx asc",
			limit=0,
		):
			bundles[row.parent].append(row)
	return aggregate_routes(entries, details, bundles)


def stock_rows(route, qty, target):
	"""Preselect only lots actually issued to this WO; warehouse confirms the draft."""
	remaining = flt(qty)
	selected = {}
	for lot in route.lots:
		take = min(remaining, flt(lot.qty))
		if take <= EPSILON:
			continue
		if lot.serial_no and abs(take - round(take)) > EPSILON:
			frappe.throw("序列号物料的数量必须为整数。")
		key = lot.batch_no or ""
		row = selected.setdefault(
			key,
			dict(
				item_code=route.item_code,
				original_item=route.original_item,
				s_warehouse=route.source_warehouse,
				t_warehouse=target,
				uom=route.stock_uom,
				stock_uom=route.stock_uom,
				conversion_factor=1,
				qty=0,
				batch_no=lot.batch_no,
				serial_no="",
				use_serial_batch_fields=1,
			),
		)
		row["qty"] += take
		if lot.serial_no:
			row["serial_no"] += ("\n" if row["serial_no"] else "") + lot.serial_no
		remaining -= take
		if remaining <= EPSILON:
			break
	if remaining > EPSILON:
		frappe.throw("物料批次或序列号的剩余数量不足，请刷新后重试。")
	return list(selected.values())
