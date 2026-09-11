from __future__ import annotations

import hashlib
from math import floor

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from process_simplification.production_reporting.domain import (
	assignment_reported_qty,
	job_card_qty_precision,
	material_reportable_qty,
)


RELEASE = "Release"
REDISPATCH = "Redispatch"
RELEASE_REVERSAL = "Release Reversal"
MOVEMENT_TYPES = {RELEASE, REDISPATCH, RELEASE_REVERSAL}


def _hash_key(*values) -> str:
	return hashlib.sha256("|".join(str(value or "") for value in values).encode()).hexdigest()


def movement_table_ready() -> bool:
	return frappe.db.table_exists("Job Card Assignment Movement")


def validate_movement_document(doc):
	if not getattr(doc.flags, "assignment_movement_action", False):
		frappe.throw(_("Assignment movements can only be created through redispatch actions."))
	if doc.movement_type not in MOVEMENT_TYPES or flt(doc.qty) <= 0:
		frappe.throw(_("Assignment movement type and positive quantity are required."))
	for fieldname in (
		"movement_key",
		"job_card",
		"work_order",
		"company",
		"operation",
		"operation_id",
		"source_assignment",
		"moved_by",
		"moved_at",
	):
		if not doc.get(fieldname):
			frappe.throw(_("Assignment movement is missing required immutable facts."))
	if doc.movement_type == RELEASE:
		if doc.target_assignment or doc.source_movement or doc.request_key:
			frappe.throw(_("A release movement cannot contain redispatch facts."))
		if not (doc.reference_doctype and doc.reference_name):
			frappe.throw(_("A release movement requires its source document."))
	elif doc.movement_type == RELEASE_REVERSAL:
		if doc.target_assignment or not doc.source_movement or not doc.reference_name:
			frappe.throw(_("释放冲销必须关联原释放记录和取消的库存单。"))
	elif not (doc.target_assignment and doc.source_movement and doc.request_key):
		frappe.throw(_("A redispatch movement requires its target, source release, and request id."))
	source = frappe.db.get_value(
		"Job Card Worker Assignment",
		doc.source_assignment,
		["job_card", "work_order", "company", "operation", "operation_id"],
		as_dict=True,
	)
	if not source or any(
		source.get(fieldname) != doc.get(fieldname)
		for fieldname in ("job_card", "work_order", "company", "operation", "operation_id")
	):
		frappe.throw(_("Assignment movement source does not match its Job Card facts."))
	if doc.movement_type == REDISPATCH:
		target = frappe.db.get_value(
			"Job Card Worker Assignment",
			doc.target_assignment,
			["job_card", "work_order", "company", "operation", "operation_id", "status"],
			as_dict=True,
		)
		if (
			not target
			or target.status != "Active"
			or any(
				target.get(fieldname) != doc.get(fieldname)
				for fieldname in ("job_card", "work_order", "company", "operation", "operation_id")
			)
		):
			frappe.throw(_("Redispatch target does not match the source Job Card."))
		release = frappe.db.get_value(
			"Job Card Assignment Movement",
			doc.source_movement,
			["movement_type", "job_card", "source_assignment"],
			as_dict=True,
		)
		if (
			not release
			or release.movement_type != RELEASE
			or release.job_card != doc.job_card
			or release.source_assignment != doc.source_assignment
		):
			frappe.throw(_("Redispatch must consume a matching release movement."))
	if doc.movement_type == RELEASE_REVERSAL:
		source_release = frappe.db.get_value("Job Card Assignment Movement", doc.source_movement,
			["movement_type", "job_card", "source_assignment", "qty"], as_dict=True)
		if not source_release or source_release.movement_type != RELEASE or source_release.job_card != doc.job_card or source_release.source_assignment != doc.source_assignment or flt(doc.qty) > flt(source_release.qty):
			frappe.throw(_("释放冲销与原派工记录不一致。"))
	if doc.is_new():
		return
	immutable_fields = (
		"movement_key",
		"movement_type",
		"job_card",
		"work_order",
		"company",
		"operation",
		"operation_id",
		"source_assignment",
		"target_assignment",
		"source_movement",
		"qty",
		"reason",
		"reference_doctype",
		"reference_name",
		"request_key",
		"moved_by",
		"moved_at",
	)
	old = frappe.db.get_value(doc.doctype, doc.name, list(immutable_fields), as_dict=True)
	if old and any(old.get(fieldname) != doc.get(fieldname) for fieldname in immutable_fields):
		frappe.throw(_("Assignment movement facts are immutable."))


def prevent_movement_delete(doc):
	if not getattr(doc.flags, "assignment_movement_action", False):
		frappe.throw(_("Assignment movements are immutable audit records and cannot be deleted."))


def _movement_rows(
	*,
	job_card: str | None = None,
	assignments: list[str] | tuple[str, ...] | None = None,
	request_key: str | None = None,
	for_update: bool = False,
):
	if not movement_table_ready():
		return []
	movement = frappe.qb.DocType("Job Card Assignment Movement")
	condition = movement.name.notnull()
	if job_card:
		condition &= movement.job_card == job_card
	if assignments:
		condition &= (movement.source_assignment.isin(assignments)) | (
			movement.target_assignment.isin(assignments)
		)
	if request_key:
		condition &= movement.request_key == request_key
	query = (
		frappe.qb.from_(movement)
		.select(
			movement.name,
			movement.movement_type,
			movement.job_card,
			movement.source_assignment,
			movement.target_assignment,
			movement.source_movement,
			movement.qty,
			movement.request_key,
			movement.reference_name,
			movement.reference_doctype,
			movement.moved_at,
		)
		.where(condition)
		.orderby(movement.moved_at)
		.orderby(movement.creation)
		.orderby(movement.name)
	)
	if for_update:
		query = query.for_update()
	return query.run(as_dict=True)


def movement_totals(
	assignments: list[str] | tuple[str, ...],
	*,
	rows=None,
	for_update: bool = False,
) -> dict[str, frappe._dict]:
	names = [name for name in assignments if name]
	totals = {
		name: frappe._dict(released_qty=0.0, redispatched_qty=0.0)
		for name in names
	}
	if not names:
		return totals
	rows = rows if rows is not None else _movement_rows(assignments=names, for_update=for_update)
	for row in rows:
		if row.movement_type == RELEASE and row.source_assignment in totals:
			totals[row.source_assignment].released_qty += flt(row.qty)
		elif row.movement_type == REDISPATCH and row.target_assignment in totals:
			totals[row.target_assignment].redispatched_qty += flt(row.qty)
		elif row.movement_type == RELEASE_REVERSAL and row.source_assignment in totals:
			totals[row.source_assignment].released_qty -= flt(row.qty)
	return totals


def assignment_effective_qty(assignment, *, totals=None, for_update: bool = False) -> float:
	precision = job_card_qty_precision()
	name = assignment.get("name")
	row = (
		totals.get(name)
		if totals is not None
		else movement_totals([name], for_update=for_update).get(name)
	) or frappe._dict()
	return flt(
		max(
			flt(assignment.get("assigned_qty"), precision)
			- flt(row.get("released_qty"), precision)
			+ flt(row.get("redispatched_qty"), precision),
			0,
		),
		precision,
	)


def assignment_material_allocation_qty(
	assignment,
	*,
	totals=None,
	for_update: bool = False,
) -> float:
	"""Material ownership grows with inbound redispatch but never with replenishment."""
	precision = job_card_qty_precision()
	name = assignment.get("name")
	row = (
		totals.get(name)
		if totals is not None
		else movement_totals([name], for_update=for_update).get(name)
	) or frappe._dict()
	return flt(
		max(
			flt(assignment.get("assigned_qty"), precision)
			+ flt(row.get("redispatched_qty"), precision),
			0,
		),
		precision,
	)


def released_qty(assignment: str, *, for_update: bool = False) -> float:
	return flt(
		movement_totals([assignment], for_update=for_update)[assignment].released_qty,
		job_card_qty_precision(),
	)


def _release_pool_rows(job_card: str, *, for_update: bool = False):
	rows = _movement_rows(job_card=job_card, for_update=for_update)
	consumed = {}
	for row in rows:
		if row.movement_type in {REDISPATCH, RELEASE_REVERSAL} and row.source_movement:
			consumed[row.source_movement] = consumed.get(row.source_movement, 0.0) + flt(row.qty)
	pool = []
	for row in rows:
		if row.movement_type != RELEASE:
			continue
		remaining = max(flt(row.qty) - flt(consumed.get(row.name)), 0)
		if remaining:
			pool.append(frappe._dict(row, remaining_qty=remaining))
	return pool, rows


def redispatchable_qty(job_card, *, for_update: bool = False) -> float:
	"""Return released quantity that material coverage can safely activate now."""
	if isinstance(job_card, str):
		from process_simplification.production_reporting.domain import job_card_values

		job_card = job_card_values(job_card, for_update=for_update)
	if not job_card or not movement_table_ready():
		return 0.0
	precision = job_card_qty_precision()
	assignment = frappe.qb.DocType("Job Card Worker Assignment")
	assignment_query = (
		frappe.qb.from_(assignment)
		.select(assignment.name, assignment.assigned_qty)
		.where(
			(assignment.job_card == job_card.name)
			& (assignment.status == "Active")
		)
		.orderby(assignment.name)
	)
	if for_update:
		assignment_query = assignment_query.for_update()
	assignments = assignment_query.run(as_dict=True)
	pool_rows, rows = _release_pool_rows(job_card.name, for_update=for_update)
	unconsumed = flt(sum(row.remaining_qty for row in pool_rows), precision)
	if unconsumed <= 0:
		return 0.0
	totals = movement_totals([row.name for row in assignments], rows=rows)
	active_remaining = flt(
		sum(
			max(
				assignment_effective_qty(row, totals=totals)
				- assignment_reported_qty(row.name, for_update=for_update),
				0,
			)
			for row in assignments
		),
		precision,
	)
	material_available = material_reportable_qty(job_card, for_update=for_update)
	whole_material_surplus = floor(
		max(flt(material_available, precision) - active_remaining, 0) + 1e-9
	)
	return flt(
		max(0, min(unconsumed, whole_material_surplus)),
		precision,
	)


def released_pool_qty(job_card: str, *, for_update: bool = False) -> float:
	if not movement_table_ready():
		return 0.0
	return flt(
		sum(row.remaining_qty for row in release_pool_rows(job_card, for_update=for_update)),
		job_card_qty_precision(),
	)


def create_release_movement(request, qty: float):
	precision = job_card_qty_precision()
	qty = flt(qty, precision)
	if qty <= 0:
		return None
	movement_key = _hash_key(RELEASE, "Production Exception Request", request.name, request.get("stock_entry"))
	existing = frappe.db.get_value(
		"Job Card Assignment Movement",
		{"movement_key": movement_key},
		["name", "qty", "source_assignment"],
		as_dict=True,
	)
	if existing:
		if (
			flt(existing.qty, precision) != qty
			or existing.source_assignment != request.assignment
		):
			frappe.throw(_("The material return already has a different assignment release."))
		return frappe.get_doc("Job Card Assignment Movement", existing.name)
	doc = frappe.get_doc(
		{
			"doctype": "Job Card Assignment Movement",
			"movement_key": movement_key,
			"movement_type": RELEASE,
			"job_card": request.job_card,
			"work_order": request.work_order,
			"company": request.company,
			"operation": request.operation,
			"operation_id": request.operation_id,
			"source_assignment": request.assignment,
			"qty": qty,
			"reason": request.reason,
			"reference_doctype": "Production Exception Request",
			"reference_name": request.name,
			"moved_by": frappe.session.user,
			"moved_at": now_datetime(),
		}
	)
	doc.flags.assignment_movement_action = True
	doc.insert(ignore_permissions=True)
	return doc


def reverse_request_releases(request, stock_entry: str):
	"""Append reversals atomically with cancellation; never rewrite assignment history."""
	rows = _movement_rows(job_card=request.job_card, for_update=True)
	for release in rows:
		if release.movement_type != RELEASE or release.reference_doctype != "Production Exception Request" or release.reference_name != request.name:
			continue
		reversed_qty = sum(flt(row.qty) for row in rows if row.movement_type == RELEASE_REVERSAL and row.source_movement == release.name)
		remaining = flt(release.qty) - reversed_qty
		if remaining <= 0:
			continue
		if any(row.movement_type == REDISPATCH and row.source_movement == release.name for row in rows):
			frappe.throw(_("该退料释放的任务已经重新派工，不能直接取消。请保留原退料记录，由主管按补料流程恢复生产，避免改变其他工人的任务和报工。"))
		doc = frappe.get_doc({
			"doctype": "Job Card Assignment Movement",
			"movement_type": RELEASE_REVERSAL,
			"movement_key": _hash_key(RELEASE_REVERSAL, release.name, stock_entry),
			"job_card": request.job_card, "work_order": request.work_order,
			"company": request.company, "operation": request.operation, "operation_id": request.operation_id,
			"source_assignment": release.source_assignment, "source_movement": release.name,
			"qty": remaining, "reason": _("取消退库，恢复尚未重新派出的任务数量。"),
			"reference_doctype": "Stock Entry", "reference_name": stock_entry,
			"moved_by": frappe.session.user, "moved_at": now_datetime(),
		})
		doc.flags.assignment_movement_action = True
		doc.insert(ignore_permissions=True)


def redispatch_request_rows(request_key: str, *, for_update: bool = False):
	return _movement_rows(request_key=request_key, for_update=for_update)


def create_redispatch_movement(
	*,
	release,
	target_assignment,
	qty: float,
	request_key: str,
	reason: str | None = None,
	sequence: int = 0,
):
	qty = flt(qty, job_card_qty_precision())
	movement_key = _hash_key(
		REDISPATCH,
		request_key,
		release.name,
		target_assignment.name,
		sequence,
	)
	doc = frappe.get_doc(
		{
			"doctype": "Job Card Assignment Movement",
			"movement_key": movement_key,
			"movement_type": REDISPATCH,
			"job_card": target_assignment.job_card,
			"work_order": target_assignment.work_order,
			"company": target_assignment.company,
			"operation": target_assignment.operation,
			"operation_id": target_assignment.operation_id,
			"source_assignment": release.source_assignment,
			"target_assignment": target_assignment.name,
			"source_movement": release.name,
			"qty": qty,
			"reason": reason,
			"reference_doctype": "Job Card Worker Assignment",
			"reference_name": target_assignment.name,
			"request_key": request_key,
			"moved_by": frappe.session.user,
			"moved_at": now_datetime(),
		}
	)
	doc.flags.assignment_movement_action = True
	doc.insert(ignore_permissions=True)
	return doc


def release_pool_rows(job_card: str, *, for_update: bool = False):
	return _release_pool_rows(job_card, for_update=for_update)[0]
