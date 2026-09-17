"""Read native batch facts without changing stock ownership or planning policy.

These helpers are deliberately not whitelisted.  Callers must check access to
their business document, and mutating callers must hold the existing Bin/SRE
locks before reading these facts again.  There is no cross-request cache.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import flt


EPSILON = 1e-9


def is_batch_item(item_code):
	return bool(item_code and frappe.get_cached_value("Item", item_code, "has_batch_no"))


def reservation_remaining(row):
	return max(flt(row.get("reserved_qty")) - sum(
		flt(row.get(field)) for field in ("delivered_qty", "transferred_qty", "consumed_qty")
	), 0)


def _ignored_names(ignore_sre):
	if not ignore_sre:
		return []
	return [ignore_sre] if isinstance(ignore_sre, str) else list(ignore_sre)


def _reservation_parts(row):
	"""Split an outstanding promise into actual batch identities and loose qty.

	Expired identities still count as bound; they must never silently become a
	promise against healthy batches.  Native batch queries only deduct children
	when their parent is in Serial and Batch mode.
	"""
	remaining = reservation_remaining(row)
	bound = defaultdict(float)
	if row.get("reservation_based_on") == "Serial and Batch":
		for child in row.get("sb_entries") or []:
			if child.get("batch_no"):
				bound[child["batch_no"]] += max(flt(child.get("qty")) - flt(child.get("delivered_qty")), 0)
	return remaining, dict(bound), max(remaining - sum(bound.values()), 0)


def calculate_stock_facts(*, actual_qty, eligible_batches, available_batches, reservations):
	"""Combine native batch pools and SREs, each reservation deducted once.

	eligible_batches has native POS deductions but no SRE deductions;
	available_batches also has native batch SRE deductions.  Both exclude
	disabled/expired batches.  Quantity promises are not in that native batch
	deduction, including Work Order promises with no children.
	"""
	eligible = {key: max(flt(value), 0) for key, value in eligible_batches.items()}
	available = {key: max(flt(value), 0) for key, value in available_batches.items()}
	ordered = sorted(reservations, key=lambda row: (str(row.get("creation") or ""), str(row.get("name") or "")))
	parts = [(row, *_reservation_parts(row)) for row in ordered]
	hard_reserved = sum(remaining for _row, remaining, _bound, _unbound in parts)
	unbound_reserved = sum(unbound for _row, _remaining, _bound, unbound in parts)
	free_qty = max(min(
		max(flt(actual_qty) - hard_reserved, 0),
		max(sum(available.values()) - unbound_reserved, 0),
	), 0)

	# Determine which existing promises are currently executable.  Batch
	# identities take precedence over unbound promises, independent of the
	# ordering of the documents.  This is a read-only feasibility calculation,
	# not a new stock reservation or planning-priority rule.
	physical = dict(eligible)
	physical_total = max(flt(actual_qty), 0)
	effective = {row.get("name"): 0.0 for row in ordered if row.get("name")}
	for row, remaining, bound, _unbound in parts:
		for batch_no, qty in bound.items():
			take = min(qty, max(remaining - effective.get(row.get("name"), 0), 0), physical.get(batch_no, 0), physical_total)
			physical[batch_no] = max(physical.get(batch_no, 0) - take, 0)
			physical_total -= take
			if row.get("name"):
				effective[row["name"]] += take
	loose_pool = min(sum(physical.values()), physical_total)
	for row, _remaining, _bound, unbound in parts:
		take = min(unbound, loose_pool)
		loose_pool -= take
		if row.get("name"):
			effective[row["name"]] += take
	return frappe._dict(
		has_batch_no=True,
		actual_qty=flt(actual_qty),
		eligible_qty=sum(eligible.values()),
		available_qty=free_qty,
		unbound_reserved_qty=unbound_reserved,
		batch_available_qty=available,
		effective_reservation_qty=effective,
	)


def _load_reservations(item_code, warehouse, ignore_sre=None):
	filters = {"docstatus": 1, "item_code": item_code, "warehouse": warehouse}
	ignored = _ignored_names(ignore_sre)
	if ignored:
		filters["name"] = ["not in", ignored]
	rows = frappe.get_all(
		"Stock Reservation Entry", filters=filters,
		fields=["name", "creation", "reserved_qty", "delivered_qty", "transferred_qty", "consumed_qty", "reservation_based_on"],
		order_by="creation asc, name asc", limit=0,
	)
	rows = [row for row in rows if reservation_remaining(row) > EPSILON]
	by_name = {row.name: row for row in rows}
	for row in rows:
		row.sb_entries = []
	if by_name:
		for child in frappe.get_all(
			"Serial and Batch Entry",
			filters={"parent": ["in", list(by_name)], "parenttype": "Stock Reservation Entry"},
			fields=["parent", "batch_no", "qty", "delivered_qty"], order_by="idx asc", limit=0,
		):
			by_name[child.parent].sb_entries.append(child)
	return rows


def _native_batch_pool(item_code, warehouse, *, ignore_sre=None, posting_date=None, posting_time=None, ignore_reserved_stock=False):
	from erpnext.stock.doctype.batch.batch import get_batch_qty

	rows = get_batch_qty(
		item_code=item_code, warehouse=warehouse,
		posting_date=posting_date, posting_time=posting_time,
		ignore_voucher_nos=_ignored_names(ignore_sre) or None,
		ignore_reserved_stock=ignore_reserved_stock,
	)
	pool = defaultdict(float)
	for row in rows or []:
		if row.get("batch_no") and row.get("warehouse") == warehouse:
			pool[row["batch_no"]] += flt(row.get("qty"))
	return dict(pool)


def get_stock_snapshot(item_code, warehouse, *, ignore_sre=None, posting_date=None, posting_time=None):
	if not is_batch_item(item_code):
		return frappe._dict(has_batch_no=False, available_qty=_native_available_qty(item_code, warehouse, ignore_sre))
	if not warehouse:
		return calculate_stock_facts(actual_qty=0, eligible_batches={}, available_batches={}, reservations=[])
	kwargs = dict(ignore_sre=ignore_sre, posting_date=posting_date, posting_time=posting_time)
	return calculate_stock_facts(
		actual_qty=frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty") or 0,
		eligible_batches=_native_batch_pool(item_code, warehouse, ignore_reserved_stock=True, **kwargs),
		available_batches=_native_batch_pool(item_code, warehouse, **kwargs),
		reservations=_load_reservations(item_code, warehouse, ignore_sre),
	)


def _native_available_qty(item_code, warehouse, ignore_sre=None):
	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import get_available_qty_to_reserve

	if not item_code or not warehouse:
		return 0.0
	if isinstance(ignore_sre, (list, tuple, set)):
		# Native's scalar API cannot exclude several names.  Existing non-batch
		# paths use a scalar; reject an accidental semantic change explicitly.
		if len(ignore_sre) > 1:
			raise ValueError("Non-batch native availability accepts at most one ignored reservation")
		ignore_sre = next(iter(ignore_sre), None)
	return get_available_qty_to_reserve(item_code, warehouse, ignore_sre=ignore_sre)


def get_available_qty(item_code, warehouse, *, ignore_sre=None, posting_date=None, posting_time=None):
	return get_stock_snapshot(item_code, warehouse, ignore_sre=ignore_sre, posting_date=posting_date, posting_time=posting_time).available_qty


def get_batch_stock_facts(item_code, warehouse, *, ignore_sre=None, posting_date=None, posting_time=None):
	if not is_batch_item(item_code):
		return None
	facts = get_stock_snapshot(item_code, warehouse, ignore_sre=ignore_sre, posting_date=posting_date, posting_time=posting_time)
	return frappe._dict(
		effective_qty=facts.eligible_qty, free_qty=facts.available_qty,
		effective_reserved_by_sre=facts.effective_reservation_qty,
	)


def _source_row(source_detail):
	name = source_detail if isinstance(source_detail, str) else source_detail.get("name")
	if not name:
		frappe.throw("缺少批次来源入库明细。")
	row = frappe.db.get_value("Stock Entry Detail", name,
		["name", "parent", "docstatus", "item_code", "t_warehouse", "transfer_qty", "serial_and_batch_bundle", "batch_no"], as_dict=True)
	if not row or row.docstatus != 1 or not row.t_warehouse or flt(row.transfer_qty) <= 0:
		frappe.throw("批次来源必须是已提交的有效入库明细。")
	if frappe.db.get_value("Stock Entry", row.parent, "docstatus") != 1:
		frappe.throw("批次来源入库单尚未提交或已经取消。")
	if not isinstance(source_detail, str):
		for field in ("parent", "item_code", "t_warehouse"):
			if source_detail.get(field) and source_detail.get(field) != row.get(field):
				frappe.throw("批次来源入库明细与当前业务不匹配，请刷新。")
	return row


def _source_batches(row):
	if row.serial_and_batch_bundle:
		bundle = frappe.get_doc("Serial and Batch Bundle", row.serial_and_batch_bundle)
		if bundle.get("is_cancelled") or bundle.get("docstatus") == 2:
			frappe.throw("来源入库单的批次明细已经取消。")
		for field, expected in (("item_code", row.item_code), ("warehouse", row.t_warehouse), ("voucher_type", "Stock Entry"), ("voucher_no", row.parent), ("voucher_detail_no", row.name)):
			if bundle.get(field) and bundle.get(field) != expected:
				frappe.throw("来源入库单与批次明细不匹配，请核对原单。")
		if bundle.get("type_of_transaction") != "Inward":
			frappe.throw("批次来源必须使用入库批次明细。")
		amounts = defaultdict(float)
		for child in bundle.get("entries") or []:
			if child.get("warehouse") and child.get("warehouse") != row.t_warehouse:
				frappe.throw("来源批次仓库与入库仓库不一致。")
			if not child.get("batch_no") or flt(child.get("qty")) <= 0:
				frappe.throw("来源入库单缺少有效批次，请在原单核对。")
			amounts[child.batch_no] += flt(child.qty)
		if not amounts or abs(sum(amounts.values()) - flt(row.transfer_qty)) > 1e-6:
			frappe.throw("来源批次数量与入库数量不一致，请核对原单。")
		return [frappe._dict(batch_no=batch, qty=qty, warehouse=row.t_warehouse) for batch, qty in amounts.items()]
	if row.batch_no:
		return [frappe._dict(batch_no=row.batch_no, qty=flt(row.transfer_qty), warehouse=row.t_warehouse)]
	frappe.throw("该入库明细没有实际批次，不能自动回补，请核对原单。")


def get_source_batches(source_detail):
	return _source_batches(_source_row(source_detail))


def _load_source_claims(row, ignore_sre=None):
	filters = {"docstatus": 1, "from_voucher_type": "Stock Entry", "from_voucher_no": row.parent, "from_voucher_detail_no": row.name, "item_code": row.item_code}
	ignored = _ignored_names(ignore_sre)
	if ignored:
		filters["name"] = ["not in", ignored]
	claims = frappe.get_all("Stock Reservation Entry", filters=filters, fields=["name", "reserved_qty", "reservation_based_on"], limit=0)
	by_name = {claim.name: claim for claim in claims}
	by_batch = defaultdict(float)
	bound_by_parent = defaultdict(float)
	if by_name:
		for child in frappe.get_all("Serial and Batch Entry", filters={"parent": ["in", list(by_name)], "parenttype": "Stock Reservation Entry"}, fields=["parent", "batch_no", "qty"], order_by="idx asc", limit=0):
			claim = by_name[child.parent]
			if claim.reservation_based_on == "Serial and Batch" and child.batch_no:
				qty = min(max(flt(child.qty), 0), max(flt(claim.reserved_qty) - bound_by_parent[child.parent], 0))
				by_batch[child.batch_no] += qty
				bound_by_parent[child.parent] += qty
	return frappe._dict(by_batch=dict(by_batch), unbound_qty=sum(max(flt(claim.reserved_qty) - bound_by_parent[claim.name], 0) for claim in claims))


def allocate_source_batches(source_batches, qty, *, available_by_batch, available_qty, claimed_by_batch=None, unbound_claimed_qty=0):
	"""Pure allocation of a receipt's unclaimed quantity against current lots."""
	claimed_by_batch = claimed_by_batch or {}
	remaining_claim = max(flt(unbound_claimed_qty), 0)
	# Even legacy claims with a different/unknown batch still occupy the
	# receipt's total quantity; do not let bad old lineage inflate its output.
	source_remaining = max(sum(max(flt(row.get("qty")), 0) for row in source_batches)
		- sum(max(flt(value), 0) for value in claimed_by_batch.values()) - remaining_claim, 0)
	remaining_qty = min(max(flt(qty), 0), max(flt(available_qty), 0), source_remaining)
	result = []
	for row in source_batches:
		batch_no = row.get("batch_no")
		source_qty = max(flt(row.get("qty")) - max(flt(claimed_by_batch.get(batch_no)), 0), 0)
		claimed = min(source_qty, remaining_claim)
		remaining_claim -= claimed
		source_qty -= claimed
		take = min(source_qty, max(flt(available_by_batch.get(batch_no)), 0), remaining_qty)
		if take > EPSILON:
			result.append(frappe._dict(batch_no=batch_no, qty=take, warehouse=row.get("warehouse")))
			remaining_qty -= take
	return result


def source_batch_allocation(source_detail, qty, *, ignore_sre=None, posting_date=None, posting_time=None, batch_budget=None, source_claims=None):
	"""Read a submitted receipt and allocate only its actual batches.

	Pass one mutable batch_budget through a multi-source preview.  Keys are
	(item, warehouse, batch); batch=None is the shared aggregate free budget.
	Writing callers should normally re-read after each submitted SRE instead.
	Additional four-part budget keys track repeated rows in the same preview.
	Claims retain already-delivered/consumed quantity: use of a receipt does
	not make the original receipt quantity available to claim a second time.
	"""
	row = _source_row(source_detail)
	batches = _source_batches(row)
	facts = get_stock_snapshot(row.item_code, row.t_warehouse, ignore_sre=ignore_sre, posting_date=posting_date, posting_time=posting_time)
	if not facts.has_batch_no:
		frappe.throw("来源物料未启用批次，请使用普通库存流程。")
	claims = source_claims if source_claims is not None else _load_source_claims(row, ignore_sre)
	claimed_by_batch = dict(claims.get("by_batch") or {})
	available = dict(facts.batch_available_qty)
	free_qty = facts.available_qty
	pool_key = (row.item_code, row.t_warehouse, None)
	if batch_budget is not None:
		free_qty = min(free_qty, batch_budget.setdefault(pool_key, free_qty))
		for batch_no in available:
			key = (row.item_code, row.t_warehouse, batch_no)
			available[batch_no] = min(available[batch_no], batch_budget.setdefault(key, available[batch_no]))
		for source_batch in batches:
			batch_no = source_batch.batch_no
			key = (None, row.name, row.t_warehouse, batch_no)
			claimed_by_batch[batch_no] = flt(claimed_by_batch.get(batch_no)) + flt(batch_budget.get(key))
	result = allocate_source_batches(batches, qty,
		available_by_batch=available, available_qty=free_qty,
		claimed_by_batch=claimed_by_batch, unbound_claimed_qty=claims.get("unbound_qty", 0))
	if batch_budget is not None:
		batch_budget[pool_key] = max(free_qty - sum(flt(entry.qty) for entry in result), 0)
		for entry in result:
			key = (row.item_code, row.t_warehouse, entry.batch_no)
			batch_budget[key] = max(available.get(entry.batch_no, 0) - entry.qty, 0)
			source_key = (None, row.name, row.t_warehouse, entry.batch_no)
			batch_budget[source_key] = flt(batch_budget.get(source_key)) + entry.qty
	return result
