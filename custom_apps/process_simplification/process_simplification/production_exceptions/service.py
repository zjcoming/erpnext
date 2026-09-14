from __future__ import annotations

import hashlib
from math import ceil, floor

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import cint, flt, getdate, now_datetime

from process_simplification.management_access import WAREHOUSE_OPERATOR_ROLE, user_company_scope
from process_simplification.production_exceptions.constants import (
	APPLIED,
	APPROVED,
	AWAITING_STOCK_ENTRY,
	CAUSES,
	COMPLETED,
	CONTINUE_PRODUCTION,
	RELEASE_ASSIGNMENT,
	WITHDRAWN,
	MATERIAL_REQUEST_TYPES,
	MATERIAL_RETURN,
	MATERIAL_SCRAP,
	OPEN_MATERIAL_STATUSES,
	PENDING_APPROVAL,
	PROCESS_LOSS,
	REJECTED,
	REQUEST_STATUSES,
	REQUEST_TYPES,
)
from process_simplification.production_reporting.constants import (
	ADMIN_REVIEW_ROLES,
	REVIEW_ROLES,
)
from process_simplification.production_reporting.domain import (
	assignment_reported_qty,
	assert_supported_job_card,
	employee_for_user,
	job_card_qty_precision,
	job_card_values,
	material_reportable_qty,
	pending_report_qty,
	request_audit,
	user_roles,
)


STOCK_VIEW_ROLES = {WAREHOUSE_OPERATOR_ROLE, "Stock User", "Stock Manager", "System Manager"}
DEFAULT_EXCEPTION_HISTORY_PAGE_LENGTH = 20
MAX_EXCEPTION_HISTORY_PAGE_LENGTH = 100
EXCEPTION_HISTORY_STATUSES = {APPLIED, COMPLETED, REJECTED, WITHDRAWN}
ASSIGNMENT_MATERIAL_STATUSES = {
	PENDING_APPROVAL,
	APPROVED,
	AWAITING_STOCK_ENTRY,
	COMPLETED,
}
IMMUTABLE_FIELDS = (
	"request_key",
	"request_type",
	"material_action",
	"assignment",
	"job_card",
	"work_order",
	"company",
	"operation",
	"operation_id",
	"employee",
	"employee_name",
	"employee_user",
	"supervisor",
	"cause",
	"reason",
	"material_key",
	"requirement_key",
	"original_item",
	"item_code",
	"item_name",
	"stock_uom",
	"qty",
	"source_warehouse",
	"target_warehouse",
	"requested_by",
	"requested_at",
)


def _material_key(item_code: str, source_warehouse: str, return_warehouse: str) -> str:
	payload = "\x1f".join((item_code or "", source_warehouse or "", return_warehouse or ""))
	return hashlib.sha256(payload.encode()).hexdigest()


def _request_fields() -> list[str]:
	return [
		"name",
		"request_type",
		"material_action",
		"status",
		"assignment",
		"job_card",
		"work_order",
		"company",
		"operation",
		"employee",
		"employee_name",
		"supervisor",
		"cause",
		"reason",
		"item_code",
		"item_name",
		"stock_uom",
		"qty",
		"source_warehouse",
		"target_warehouse",
		"requested_at",
		"reviewed_by",
		"reviewed_at",
		"rejection_reason",
		"stock_entry",
		"processed_by",
		"processed_at",
		"withdrawn_by",
		"withdrawn_at",
		"withdrawal_reason",
	]


def _save_request(doc):
	doc.flags.production_exception_action = True
	doc.save(ignore_permissions=True)
	return doc


def validate_request_document(doc):
	if not getattr(doc.flags, "production_exception_action", False):
		frappe.throw(_("Production exception requests can only be changed through exception actions."))
	if doc.request_type not in REQUEST_TYPES or doc.status not in REQUEST_STATUSES:
		frappe.throw(_("Invalid production exception type or status."))
	if not all(
		doc.get(fieldname)
		for fieldname in (
			"request_key",
			"assignment",
			"job_card",
			"work_order",
			"company",
			"operation",
			"operation_id",
			"employee",
			"employee_user",
			"supervisor",
			"cause",
			"reason",
			"requested_by",
			"requested_at",
		)
	):
		frappe.throw(_("Production exception request is missing required audit facts."))
	if doc.cause not in CAUSES or flt(doc.qty) <= 0:
		frappe.throw(_("Production exception cause and positive quantity are required."))
	if doc.get("material_action") and doc.material_action not in {CONTINUE_PRODUCTION, RELEASE_ASSIGNMENT}:
		frappe.throw(_("请选择有效的退料后任务安排。"))
	if doc.request_type in MATERIAL_REQUEST_TYPES:
		if not all(
			doc.get(fieldname)
			for fieldname in (
				"material_key",
				"item_code",
				"stock_uom",
				"source_warehouse",
				"target_warehouse",
			)
		):
			frappe.throw(_("A material exception requires an item and warehouse route."))
	elif any(doc.get(fieldname) for fieldname in ("material_key", "item_code", "source_warehouse", "target_warehouse")):
		frappe.throw(_("A process-loss request cannot contain a material-transfer route."))
	if doc.is_new():
		return
	old = frappe.db.get_value(doc.doctype, doc.name, list(IMMUTABLE_FIELDS), as_dict=True)
	mutable_decision = bool(getattr(doc.flags, "material_decision", False))
	if old and any(old.get(fieldname) != doc.get(fieldname) for fieldname in IMMUTABLE_FIELDS if not (fieldname == "material_action" and mutable_decision)):
		frappe.throw(_("Production exception request facts are immutable."))


def _lock_worker_assignment(assignment_name: str, *, require_executable: bool = True):
	from process_simplification.production_reporting.domain import require_worker

	require_worker()
	employee = employee_for_user()
	initial = frappe.db.get_value(
		"Job Card Worker Assignment",
		assignment_name,
		["name", "job_card", "employee"],
		as_dict=True,
	)
	if not initial:
		frappe.throw(_("Worker assignment does not exist."))
	job_card = job_card_values(initial.job_card, for_update=True)
	if not job_card:
		frappe.throw(_("Job Card no longer exists."))
	if job_card.work_order:
		frappe.db.get_value("Work Order", job_card.work_order, "name", for_update=True)
	assignment = frappe.db.get_value(
		"Job Card Worker Assignment",
		assignment_name,
		[
			"name",
			"job_card",
			"work_order",
			"company",
			"operation",
			"operation_id",
			"job_card_qty",
			"assigned_qty",
			"employee",
			"employee_user",
			"supervisor",
			"status",
		],
		as_dict=True,
		for_update=True,
	)
	if (
		not assignment
		or assignment.employee != employee
		or assignment.employee_user != frappe.session.user
		or (require_executable and assignment.status != "Active")
	):
		frappe.throw(_("You can only submit an exception for your own active assignment."), frappe.PermissionError)
	for fieldname in ("job_card", "work_order", "company", "operation", "operation_id"):
		if assignment.get(fieldname) != job_card.get(fieldname if fieldname != "job_card" else "name"):
			frappe.throw(_("Worker assignment no longer matches its Job Card."))
	if require_executable:
		assert_supported_job_card(job_card, for_update=True)
	return assignment, job_card


def _open_material_qty(
	work_order: str,
	item_code: str,
	source_warehouse: str,
	*,
	exclude_request: str | None = None,
	for_update: bool = False,
	material_key: str | None = None,
) -> float:
	request = frappe.qb.DocType("Production Exception Request")
	condition = (
		(request.work_order == work_order)
		& (request.item_code == item_code)
		& (request.source_warehouse == source_warehouse)
		& (request.status.isin(sorted(OPEN_MATERIAL_STATUSES)))
	)
	if material_key:
		condition &= request.material_key == material_key
	if exclude_request:
		condition &= request.name != exclude_request
	query = frappe.qb.from_(request).select(request.name, request.qty).where(condition)
	if for_update:
		query = query.for_update()
	return sum(flt(row.qty) for row in query.run(as_dict=True))


def _work_order_material_requirements(work_order: str, *, for_update: bool = False):
	work_order_values = frappe.db.get_value(
		"Work Order",
		work_order,
		[
			"name",
			"company",
			"qty",
			"skip_transfer",
			"source_warehouse",
			"wip_warehouse",
			"scrap_warehouse",
		],
		as_dict=True,
		for_update=for_update,
	)
	if not work_order_values:
		frappe.throw(_("Work Order does not exist."))
	item = frappe.qb.DocType("Work Order Item")
	query = (
		frappe.qb.from_(item)
		.select(
			item.name,
			item.item_code,
			item.source_warehouse,
			item.required_qty,
			item.transferred_qty,
			item.returned_qty,
			item.include_item_in_manufacturing,
		)
		.where((item.parent == work_order) & (item.required_qty > 0))
	)
	if for_update:
		query = query.for_update()
	routes = {}
	for row in query.run(as_dict=True):
		if not row.include_item_in_manufacturing:
			continue
		return_warehouse = row.source_warehouse or work_order_values.source_warehouse
		key = _material_key(
			row.item_code,
			work_order_values.wip_warehouse,
			return_warehouse,
		)
		route = routes.setdefault(
			key,
			frappe._dict(
				key=key,
				item_code=row.item_code,
				source_warehouse=work_order_values.wip_warehouse,
				return_warehouse=return_warehouse,
				required_qty=0.0,
				transferred_qty=0.0,
				returned_qty=0.0,
			),
		)
		route.required_qty += flt(row.required_qty)
		# Native Work Order rows may repeat the item aggregate on matching rows.
		route.transferred_qty = max(route.transferred_qty, flt(row.transferred_qty))
		route.returned_qty = max(route.returned_qty, flt(row.returned_qty))
	from process_simplification.production_stock_facts import load_work_order_stock_facts, get_work_order_stock_fact
	facts = load_work_order_stock_facts([work_order])
	for route in routes.values():
		fact = get_work_order_stock_fact(facts, work_order, route.item_code, route.return_warehouse)
		route.transferred_qty = fact.gross_issued_qty
		route.returned_qty = fact.returned_qty
	return work_order_values, list(routes.values())


def material_warehouse_settings(work_order):
	"""Use explicit WO overrides or company defaults, never warehouse-name guesses."""
	company = frappe.db.get_value(
		"Company", work_order.company,
		["default_scrap_warehouse", "custom_material_quarantine_warehouse"], as_dict=True,
	) or frappe._dict()
	result = frappe._dict(
		scrap_warehouse=work_order.get("scrap_warehouse") or company.default_scrap_warehouse,
		quarantine_warehouse=company.custom_material_quarantine_warehouse,
		messages={},
	)
	for field, label in (("scrap_warehouse", "报废仓"), ("quarantine_warehouse", "待检隔离仓")):
		name = result[field]
		warehouse = frappe.db.get_value("Warehouse", name, ["company", "is_group", "disabled"], as_dict=True) if name else None
		if not warehouse or warehouse.company != work_order.company or warehouse.is_group or warehouse.disabled or name in {work_order.wip_warehouse, work_order.source_warehouse}:
			result[field] = None
			result.messages[field] = _("{0}未配置或不可用，请联系主管在公司设置中选择同公司的独立{0}；仅新建仓库不会自动关联。").format(label)
	if result.quarantine_warehouse and result.quarantine_warehouse == result.scrap_warehouse:
		result.quarantine_warehouse = None
		result.messages["quarantine_warehouse"] = _("待检隔离仓不能与报废仓相同，请主管在公司设置中分别配置。")
	return result


def _target_warehouse(row, request_type, cause):
	if request_type == MATERIAL_SCRAP:
		return row.scrap_warehouse
	if cause == "Material Defect":
		return row.quarantine_warehouse
	return row.return_warehouse


def _material_request_rows(
	work_order: str,
	material_key: str,
	*,
	exclude_request: str | None = None,
	for_update: bool = False,
):
	if not frappe.db.table_exists("Production Exception Request"):
		return []
	request = frappe.qb.DocType("Production Exception Request")
	condition = (
		(request.work_order == work_order)
		& ((request.material_key == material_key) | (request.requirement_key == material_key))
		& (request.request_type.isin(sorted(MATERIAL_REQUEST_TYPES)))
		& (request.status.isin(sorted(ASSIGNMENT_MATERIAL_STATUSES)))
	)
	if exclude_request:
		condition &= request.name != exclude_request
	query = (
		frappe.qb.from_(request)
		.select(
			request.name,
			request.assignment,
			request.status,
			request.qty,
			request.requested_at,
		)
		.where(condition)
	)
	if for_update:
		query = query.for_update()
	return query.run(as_dict=True)


def _assignment_outstanding_material_qty(
	requirement,
	assignment: str,
	*,
	exclude_request: str | None = None,
	for_update: bool = False,
) -> float:
	"""Attribute the still-unreplenished material gap to its worker requests."""
	requests = _material_request_rows(
		requirement.work_order,
		requirement.key,
		exclude_request=exclude_request,
		for_update=for_update,
	)
	if not requests:
		return 0.0
	managed_qty = sum(flt(row.qty) for row in requests)
	if managed_qty <= 0:
		return 0.0
	open_qty = sum(
		flt(row.qty) for row in requests if row.status != COMPLETED
	)
	projected_net_transferred = max(
		flt(requirement.transferred_qty)
		- flt(requirement.returned_qty)
		- open_qty,
		0,
	)
	projected_gap = max(
		flt(requirement.required_qty) - projected_net_transferred,
		0,
	)
	# Replacement issues have no worker link. Restore the oldest requests first;
	# leaving the newest requests outstanding avoids proportional fractions that
	# could round down several workers for a one-unit global material gap.
	remaining_gap = min(projected_gap, managed_qty)
	owned_outstanding = 0.0
	ordered_requests = sorted(
		requests,
		key=lambda row: (str(row.requested_at or ""), row.name or ""),
		reverse=True,
	)
	for row in ordered_requests:
		if remaining_gap <= 0:
			break
		outstanding_qty = min(flt(row.qty), remaining_gap)
		if row.assignment == assignment:
			owned_outstanding += outstanding_qty
		remaining_gap -= outstanding_qty
	return owned_outstanding


def _native_material_rows(work_order: str, *, exclude_request: str | None = None, for_update=False):
    from process_simplification.production_exceptions.material_routes import load_routes
    work_order_values, requirements = _work_order_material_requirements(work_order, for_update=for_update)
    if work_order_values.skip_transfer:
        return []
    settings = material_warehouse_settings(work_order_values)
    by_key = {r.key:r for r in requirements}
    rows = load_routes(work_order, for_update=for_update)
    for row in rows:
        requirement=by_key.get(row.requirement_key)
        row.item_name=frappe.get_cached_value("Item",row.item_code,"item_name") or row.item_code
        row.scrap_warehouse=settings.scrap_warehouse if settings.scrap_warehouse!=row.return_warehouse else None
        row.quarantine_warehouse=settings.quarantine_warehouse if settings.quarantine_warehouse!=row.return_warehouse else None
        row.reserved_request_qty=_open_material_qty(work_order,row.item_code,row.source_warehouse,exclude_request=exclude_request,for_update=for_update,material_key=row.key)
        row.requestable_qty=max(row.native_available_qty-row.reserved_request_qty,0)
        row.required_qty=flt(requirement.required_qty) if requirement else 0
        row.transferred_qty=flt(requirement.transferred_qty) if requirement else row.issued_qty
        row.returned_qty=flt(requirement.returned_qty) if requirement else row.returned_qty
        row.work_order_qty=flt(work_order_values.qty)
    return [r for r in rows if r.native_available_qty>0]


def _material_row(
	work_order: str,
	material_key: str,
	*,
	exclude_request: str | None = None,
	for_update: bool = False,
):
	matches = [
		row
		for row in _native_material_rows(
			work_order,
			exclude_request=exclude_request,
			for_update=for_update,
		)
		if row.key == material_key
	]
	if len(matches) != 1:
		frappe.throw(_("The selected material is no longer uniquely returnable from WIP."))
	return matches[0]


def _assignment_snapshot(assignment, *, for_update: bool = False):
	if isinstance(assignment, str):
		return frappe.db.get_value(
			"Job Card Worker Assignment",
			assignment,
			["name", "job_card", "work_order", "assigned_qty"],
			as_dict=True,
			for_update=for_update,
		)
	as_dict = getattr(assignment, "as_dict", None)
	if callable(as_dict):
		assignment = as_dict()
	return frappe._dict(assignment or {})


def _apply_assignment_material_limit(
	row,
	assignment,
	job_card,
	*,
	exclude_request: str | None = None,
	for_update: bool = False,
):
	assignment = _assignment_snapshot(assignment, for_update=for_update)
	if (
		not assignment
		or assignment.job_card != job_card.name
		or assignment.work_order != job_card.work_order
	):
		frappe.throw(_("Worker assignment no longer matches its Job Card."))
	work_order_qty = flt(row.work_order_qty)
	required_qty = flt(row.required_qty)
	if work_order_qty <= 0 or required_qty <= 0:
		row.assignment_allocated_qty = 0.0
		row.assignment_outstanding_qty = 0.0
		row.assignment_reported_material_qty = 0.0
		row.requestable_qty = 0.0
		return row
	per_finished_qty = required_qty / work_order_qty
	requirement = frappe._dict(
		work_order=job_card.work_order,
		key=row.get("requirement_key") or row.key,
		required_qty=required_qty,
		transferred_qty=row.transferred_qty,
		returned_qty=row.returned_qty,
	)
	outstanding = _assignment_outstanding_material_qty(
		requirement,
		assignment.name,
		exclude_request=exclude_request,
		for_update=for_update,
	)
	reported_qty = assignment_reported_qty(
		assignment.name,
		for_update=for_update,
	)
	from process_simplification.production_reporting.assignment_movement import (
		assignment_effective_qty,
		assignment_material_allocation_qty,
	)

	allocated_output_qty = assignment_material_allocation_qty(
		assignment,
		for_update=for_update,
	)
	effective_output_qty = assignment_effective_qty(
		assignment,
		for_update=for_update,
	)
	allocated_qty = allocated_output_qty * per_finished_qty
	reported_material_qty = reported_qty * per_finished_qty
	personal_requestable = max(
		allocated_qty - reported_material_qty - outstanding,
		0,
	)
	personal_requestable = min(
		personal_requestable,
		max(effective_output_qty - reported_qty, 0) * per_finished_qty,
	)
	precision = frappe.get_precision("Stock Entry Detail", "transfer_qty") or 6
	row.assignment_allocated_qty = flt(allocated_qty, precision)
	row.assignment_outstanding_qty = flt(outstanding, precision)
	row.assignment_reported_material_qty = flt(reported_material_qty, precision)
	row.requestable_qty = flt(
		min(flt(row.requestable_qty), personal_requestable),
		precision,
	)
	return row


def _assignment_material_row(
	assignment,
	job_card,
	material_key: str,
	*,
	exclude_request: str | None = None,
	for_update: bool = False,
):
	row = _material_row(
		job_card.work_order,
		material_key,
		exclude_request=exclude_request,
		for_update=for_update,
	)
	return _apply_assignment_material_limit(
		row,
		assignment,
		job_card,
		exclude_request=exclude_request,
		for_update=for_update,
	)


def assignment_material_output_capacity(
	job_card,
	assignment,
	*,
	exclude_request: str | None = None,
	for_update: bool = False,
) -> float | None:
	"""Return this worker's whole finished-unit capacity after their material requests."""
	if isinstance(job_card, str):
		job_card = job_card_values(job_card, for_update=for_update)
	else:
		as_dict = getattr(job_card, "as_dict", None)
		if callable(as_dict):
			job_card = as_dict()
		job_card = frappe._dict(job_card or {})
	assignment = _assignment_snapshot(assignment, for_update=for_update)
	if not job_card or not assignment:
		return 0.0
	if assignment.job_card != job_card.name or assignment.work_order != job_card.work_order:
		frappe.throw(_("Worker assignment no longer matches its Job Card."))
	work_order, requirements = _work_order_material_requirements(
		job_card.work_order,
		for_update=for_update,
	)
	if work_order.skip_transfer or not requirements:
		return None
	if flt(work_order.qty) <= 0:
		return 0.0
	from process_simplification.production_reporting.assignment_movement import (
		assignment_material_allocation_qty,
	)

	allocated_output_qty = assignment_material_allocation_qty(
		assignment,
		for_update=for_update,
	)
	capacities = []
	for requirement in requirements:
		requirement.work_order = job_card.work_order
		per_finished_qty = flt(requirement.required_qty) / flt(work_order.qty)
		if per_finished_qty <= 0:
			continue
		outstanding = _assignment_outstanding_material_qty(
			requirement,
			assignment.name,
			exclude_request=exclude_request,
			for_update=for_update,
		)
		allocated_qty = allocated_output_qty * per_finished_qty
		remaining_material = max(allocated_qty - outstanding, 0)
		capacities.append(floor((remaining_material / per_finished_qty) + 1e-9))
	if not capacities:
		return None
	return float(
		max(
			0,
			min(
				floor(flt(allocated_output_qty) + 1e-9),
				*capacities,
			),
		)
	)


def _completed_assignment_material_qty(
	assignment: str,
	material_key: str,
	*,
	for_update: bool = False,
) -> float:
	request = frappe.qb.DocType("Production Exception Request")
	query = (
		frappe.qb.from_(request)
		.select(request.name, request.qty)
		.where(
			(request.assignment == assignment)
			& ((request.material_key == material_key) | (request.requirement_key == material_key))
			& (request.request_type.isin(sorted(MATERIAL_REQUEST_TYPES)))
			& (request.status == COMPLETED)
			& ((request.material_action == RELEASE_ASSIGNMENT) | request.material_action.isnull() | (request.material_action == ""))
		)
	)
	if for_update:
		query = query.for_update()
	return sum(flt(row.qty) for row in query.run(as_dict=True))


def assignment_completed_material_output_loss(
	job_card,
	assignment,
	*,
	for_update: bool = False,
) -> float:
	"""Return whole output units permanently released by posted worker returns."""
	if isinstance(job_card, str):
		job_card = job_card_values(job_card, for_update=for_update)
	else:
		as_dict = getattr(job_card, "as_dict", None)
		if callable(as_dict):
			job_card = as_dict()
		job_card = frappe._dict(job_card or {})
	assignment = _assignment_snapshot(assignment, for_update=for_update)
	if not job_card or not assignment:
		return 0.0
	from process_simplification.production_reporting.assignment_movement import (
		assignment_material_allocation_qty,
	)

	allocated_output_qty = assignment_material_allocation_qty(
		assignment,
		for_update=for_update,
	)
	work_order, requirements = _work_order_material_requirements(
		job_card.work_order,
		for_update=for_update,
	)
	if work_order.skip_transfer or not requirements or flt(work_order.qty) <= 0:
		return 0.0
	capacities = []
	for requirement in requirements:
		per_finished_qty = flt(requirement.required_qty) / flt(work_order.qty)
		if per_finished_qty <= 0:
			continue
		completed_return_qty = _completed_assignment_material_qty(
			assignment.name,
			requirement.key,
			for_update=for_update,
		)
		allocated_material = allocated_output_qty * per_finished_qty
		capacities.append(
			floor(
				(max(allocated_material - completed_return_qty, 0) / per_finished_qty)
				+ 1e-9
			)
		)
	if not capacities:
		return 0.0
	supported_output = max(
		0,
		min(floor(flt(allocated_output_qty) + 1e-9), *capacities),
	)
	return float(max(floor(flt(allocated_output_qty) + 1e-9) - supported_output, 0))


def ensure_completed_material_release(request, *, for_update: bool = False):
	"""Persist newly lost whole units; replacement stock does not reclaim them."""
	if request.request_type not in MATERIAL_REQUEST_TYPES or request.status != COMPLETED or request.get("material_action") == CONTINUE_PRODUCTION:
		return None
	from process_simplification.production_reporting.assignment_movement import (
		assignment_effective_qty,
		create_release_movement,
		released_qty,
	)

	job_card = job_card_values(request.job_card, for_update=for_update)
	assignment = _assignment_snapshot(request.assignment, for_update=for_update)
	if not job_card or not assignment:
		return None
	desired_release = assignment_completed_material_output_loss(
		job_card,
		assignment,
		for_update=for_update,
	)
	existing_release = released_qty(assignment.name, for_update=for_update)
	reported_qty = assignment_reported_qty(assignment.name, for_update=for_update)
	releasable_qty = max(
		assignment_effective_qty(assignment, for_update=for_update) - reported_qty,
		0,
	)
	delta = flt(
		min(max(desired_release - existing_release, 0), releasable_qty),
		job_card_qty_precision(),
	)
	return create_release_movement(request, delta) if delta > 0 else None


def _pending_process_loss(job_card: str, *, exclude_request: str | None = None, for_update=False) -> float:
	request = frappe.qb.DocType("Production Exception Request")
	condition = (
		(request.job_card == job_card)
		& (request.request_type == PROCESS_LOSS)
		& (request.status == PENDING_APPROVAL)
	)
	if exclude_request:
		condition &= request.name != exclude_request
	query = frappe.qb.from_(request).select(request.name, request.qty).where(condition)
	if for_update:
		query = query.for_update()
	return sum(flt(row.qty) for row in query.run(as_dict=True))


def expected_process_loss(job_card: str, *, in_flight_request: str | None = None, for_update=False) -> float:
	if not frappe.db.table_exists("Production Exception Request"):
		return 0.0
	request = frappe.qb.DocType("Production Exception Request")
	query = (
		frappe.qb.from_(request)
		.select(request.name, request.qty)
		.where(
			(request.job_card == job_card)
			& (request.request_type == PROCESS_LOSS)
			& (request.status == APPLIED)
		)
	)
	if for_update:
		query = query.for_update()
	qty = sum(flt(row.qty) for row in query.run(as_dict=True))
	if in_flight_request:
		row = frappe.db.get_value(
			"Production Exception Request",
			in_flight_request,
			["job_card", "request_type", "status", "qty"],
			as_dict=True,
			for_update=for_update,
		)
		if (
			row
			and row.job_card == job_card
			and row.request_type == PROCESS_LOSS
			and row.status == PENDING_APPROVAL
		):
			qty += flt(row.qty)
	return flt(qty, job_card_qty_precision())


def _process_loss_capacity(job_card, *, exclude_request=None, for_update=False) -> float:
	precision = job_card_qty_precision()
	reserved = pending_report_qty(job_card.name, for_update=for_update) + _pending_process_loss(
		job_card.name,
		exclude_request=exclude_request,
		for_update=for_update,
	)
	quantity_capacity = max(
		flt(job_card.for_quantity, precision)
		- flt(job_card.total_completed_qty, precision)
		- flt(job_card.process_loss_qty, precision)
		- flt(reserved, precision),
		0,
	)
	material_capacity = material_reportable_qty(job_card, for_update=for_update)
	return flt(min(quantity_capacity, material_capacity), precision)


def get_exception_options(assignment: str):
	assignment_values, job_card = _lock_worker_assignment(assignment)
	materials = [
		_apply_assignment_material_limit(row, assignment_values, job_card)
		for row in _native_material_rows(job_card.work_order)
	]
	work_order, _requirements = _work_order_material_requirements(job_card.work_order)
	warehouses = material_warehouse_settings(work_order)
	return {
		"assignment": assignment_values.name,
		"job_card": job_card.name,
		"work_order": job_card.work_order,
		"operation": job_card.operation,
		"process_loss_available_qty": _process_loss_capacity(job_card),
		"materials": [row for row in materials if flt(row.requestable_qty) > 0],
		"warehouse_settings": warehouses,
		"empty_material_message": _("当前没有可申请的未用物料：请确认工单已发料，并检查个人剩余任务及待处理申请。") if not any(flt(row.requestable_qty) > 0 for row in materials) else "",
	}


def get_my_requests(limit=100):
	from process_simplification.production_reporting.domain import require_worker

	require_worker()
	employee = employee_for_user()
	return frappe.get_all(
		"Production Exception Request",
		filters={"employee": employee, "employee_user": frappe.session.user},
		fields=_request_fields(),
		order_by="requested_at desc, creation desc",
		limit=min(max(int(limit or 100), 1), 200),
	)


def _existing_idempotent_request(request_key: str):
	name = frappe.db.get_value("Production Exception Request", {"request_key": request_key}, "name")
	return frappe.get_doc("Production Exception Request", name) if name else None


def submit_exception(
	assignment: str,
	request_type: str,
	qty,
	cause: str,
	reason: str,
	request_key: str,
	material_key: str | None = None,
	material_action: str = CONTINUE_PRODUCTION,
):
	request_type = str(request_type or "").strip()
	cause = str(cause or "").strip()
	reason = str(reason or "").strip()
	request_key = str(request_key or "").strip()
	material_action = str(material_action or CONTINUE_PRODUCTION).strip()
	if material_action not in {CONTINUE_PRODUCTION, RELEASE_ASSIGNMENT}:
		frappe.throw(_("请选择补料后继续或交回剩余任务。"))
	precision = job_card_qty_precision()
	qty = flt(qty, precision)
	if request_type not in REQUEST_TYPES or cause not in CAUSES:
		frappe.throw(_("Select a valid exception type and cause."))
	if qty <= 0 or not reason or not request_key:
		frappe.throw(_("Positive quantity, exception details, and request id are required."))
	if len(reason) > 1000 or len(request_key) > 140:
		frappe.throw(_("Exception details or request id is too long."))

	existing = _existing_idempotent_request(request_key)
	if existing:
		if (
			existing.employee_user != frappe.session.user
			or existing.assignment != assignment
			or existing.request_type != request_type
			or flt(existing.qty, precision) != qty
			or existing.cause != cause
			or existing.reason != reason
			or (existing.material_key or None) != (material_key or None)
			or (existing.get("material_action") or RELEASE_ASSIGNMENT) != material_action
		):
			frappe.throw(_("This exception request id was already used with different values."))
		return existing

	assignment_values, job_card = _lock_worker_assignment(assignment)
	values = {}
	if request_type in MATERIAL_REQUEST_TYPES:
		row = _assignment_material_row(
			assignment_values,
			job_card,
			material_key,
			for_update=True,
		)
		if qty > flt(row.requestable_qty, precision):
			frappe.throw(
				_("This worker's BOM allocation currently allows at most {0} to be requested.").format(
					row.requestable_qty
				)
			)
		target_warehouse = _target_warehouse(row, request_type, cause)
		if not target_warehouse:
			frappe.throw(_("尚未配置可用的报废仓或待检隔离仓，请联系主管完成公司仓库配置后刷新物料。"))
		values.update(
			material_key=row.key,
			requirement_key=row.requirement_key,
			original_item=row.original_item,
			item_code=row.item_code,
			item_name=row.item_name,
			stock_uom=row.stock_uom,
			source_warehouse=row.source_warehouse,
			target_warehouse=target_warehouse,
		)
	else:
		available = _process_loss_capacity(job_card, for_update=True)
		if qty > available:
			frappe.throw(_("This Job Card currently allows at most {0} process loss.").format(available))

	audit = request_audit()
	doc = frappe.get_doc(
		{
			"doctype": "Production Exception Request",
			"request_key": request_key,
			"request_type": request_type,
			"material_action": material_action,
			"status": PENDING_APPROVAL,
			"assignment": assignment_values.name,
			"job_card": job_card.name,
			"work_order": job_card.work_order,
			"company": job_card.company,
			"operation": job_card.operation,
			"operation_id": job_card.operation_id,
			"employee": assignment_values.employee,
			"employee_name": frappe.db.get_value("Employee", assignment_values.employee, "employee_name"),
			"employee_user": assignment_values.employee_user,
			"supervisor": assignment_values.supervisor,
			"cause": cause,
			"reason": reason,
			"qty": qty,
			"requested_by": frappe.session.user,
			"requested_at": now_datetime(),
			"request_ip": audit.ip,
			"request_user_agent": audit.user_agent,
			**values,
		}
	)
	doc.flags.production_exception_action = True
	doc.insert(ignore_permissions=True)
	from process_simplification.notifications import notify_exception_submitted

	notify_exception_submitted(doc)
	return doc


def _can_review_request(doc, *, for_update=False) -> bool:
	roles = user_roles(for_update=for_update)
	companies = user_company_scope()
	return bool(
		roles.intersection(ADMIN_REVIEW_ROLES)
		and (companies is None or doc.company in companies)
	)


def _can_view_stock_requests(doc=None) -> bool:
	companies = user_company_scope()
	return bool(
		user_roles().intersection(STOCK_VIEW_ROLES)
		and (doc is None or companies is None or doc.company in companies)
	)


def require_exception_viewer():
	roles = user_roles()
	if not roles.intersection(REVIEW_ROLES | STOCK_VIEW_ROLES):
		frappe.throw(_("You are not permitted to view production exception requests."), frappe.PermissionError)


def get_review_dashboard(limit=200):
	require_exception_viewer()
	companies = user_company_scope()
	rows = frappe.get_all(
		"Production Exception Request",
		filters={} if companies is None else {"company": ("in", sorted(companies))},
		fields=_request_fields(),
		order_by="requested_at desc, creation desc",
		limit=min(max(int(limit or 200), 1), 500),
	)
	visible = []
	for row in rows:
		can_review = _can_review_request(row)
		stock_visible = _can_view_stock_requests(row) and row.request_type in MATERIAL_REQUEST_TYPES and row.status in {
			APPROVED,
			AWAITING_STOCK_ENTRY,
			COMPLETED,
			WITHDRAWN,
		}
		if not can_review and not stock_visible:
			continue
		row.can_approve = bool(can_review and row.status in {PENDING_APPROVAL, APPROVED})
		row.can_reject = bool(can_review and row.status == PENDING_APPROVAL)
		row.can_withdraw = bool(can_review and row.status in OPEN_MATERIAL_STATUSES)
		row.can_open_stock_entry = bool(
			row.stock_entry
			and frappe.has_permission("Stock Entry", ptype="read", doc=row.stock_entry)
		)
		row.stock_entry_docstatus = (
			frappe.db.get_value("Stock Entry", row.stock_entry, "docstatus") if row.stock_entry else None
		)
		visible.append(row)
	return {
		"pending": [row for row in visible if row.status == PENDING_APPROVAL],
		"stock_queue": [row for row in visible if row.status in {APPROVED, AWAITING_STOCK_ENTRY}],
		"processed": [row for row in visible if row.status in EXCEPTION_HISTORY_STATUSES],
	}


def _exception_history_pagination(
	page=1,
	page_length=DEFAULT_EXCEPTION_HISTORY_PAGE_LENGTH,
	total_count=0,
):
	page = max(cint(page) or 1, 1)
	page_length = min(
		max(cint(page_length) or DEFAULT_EXCEPTION_HISTORY_PAGE_LENGTH, 1),
		MAX_EXCEPTION_HISTORY_PAGE_LENGTH,
	)
	total_count = cint(total_count)
	total_pages = ceil(total_count / page_length) if total_count else 0
	if total_pages:
		page = min(page, total_pages)
	return {
		"page": page,
		"page_length": page_length,
		"total_count": total_count,
		"total_pages": total_pages,
		"has_next": bool(total_pages and page < total_pages),
		"has_prev": bool(total_count and page > 1),
	}


def get_review_history(
	page=1,
	page_length=DEFAULT_EXCEPTION_HISTORY_PAGE_LENGTH,
	status=None,
	request_type=None,
	employee=None,
	work_order=None,
	job_card=None,
	from_date=None,
	to_date=None,
):
	"""Return permission-scoped, processed exception history with server pagination."""
	require_exception_viewer()
	if status and status not in EXCEPTION_HISTORY_STATUSES:
		frappe.throw(_("Historical exception status must be applied, completed, rejected, or withdrawn."))
	if request_type and request_type not in REQUEST_TYPES:
		frappe.throw(_("Invalid production exception type."))

	roles = user_roles()
	can_review = bool(roles.intersection(ADMIN_REVIEW_ROLES))
	can_view_stock = bool(roles.intersection(STOCK_VIEW_ROLES))
	filters = {}
	companies = user_company_scope()
	if companies is not None:
		filters["company"] = ("in", sorted(companies))

	if can_review:
		filters["status"] = status or ("in", sorted(EXCEPTION_HISTORY_STATUSES))
	elif can_view_stock:
		# Warehouse-only users can only trace material requests whose stock move
		# has completed; supervisor rejection and process loss remain out of scope.
		if status and status not in {COMPLETED, WITHDRAWN}:
			filters["name"] = "__not_accessible__"
		else:
			filters["status"] = status or ("in", [COMPLETED, WITHDRAWN])
		filters["request_type"] = request_type or ("in", sorted(MATERIAL_REQUEST_TYPES))

	if request_type and "request_type" not in filters:
		filters["request_type"] = request_type
	if employee:
		filters["employee"] = employee
	if work_order:
		filters["work_order"] = work_order
	if job_card:
		filters["job_card"] = job_card

	start_date = getdate(from_date) if from_date else None
	end_date = getdate(to_date) if to_date else None
	if start_date and end_date and start_date > end_date:
		frappe.throw(_("The exception review start date cannot be later than the end date."))
	if start_date and end_date:
		filters["modified"] = (
			"between",
			[f"{start_date} 00:00:00", f"{end_date} 23:59:59.999999"],
		)
	elif start_date:
		filters["modified"] = (">=", f"{start_date} 00:00:00")
	elif end_date:
		filters["modified"] = ("<=", f"{end_date} 23:59:59.999999")

	pagination = _exception_history_pagination(
		page=page,
		page_length=page_length,
		total_count=frappe.db.count("Production Exception Request", filters=filters),
	)
	rows = frappe.get_all(
		"Production Exception Request",
		filters=filters,
		fields=_request_fields(),
		order_by="modified desc, name desc",
		offset=(pagination["page"] - 1) * pagination["page_length"],
		limit=pagination["page_length"],
	)
	for row in rows:
		row.can_approve = False
		row.can_reject = False
		row.can_open_stock_entry = bool(
			row.stock_entry
			and frappe.has_permission("Stock Entry", ptype="read", doc=row.stock_entry)
		)
	return {"rows": rows, "pagination": pagination}


def _lock_request_for_review(name: str):
	initial = frappe.db.get_value(
		"Production Exception Request",
		name,
		["name", "job_card", "work_order", "assignment", "employee", "supervisor"],
		as_dict=True,
	)
	if not initial:
		frappe.throw(_("Production exception request does not exist."))
	job_card = job_card_values(initial.job_card, for_update=True)
	if initial.work_order:
		frappe.db.get_value("Work Order", initial.work_order, "name", for_update=True)
	frappe.db.get_value("Job Card Worker Assignment", initial.assignment, "name", for_update=True)
	doc = frappe.get_doc("Production Exception Request", name, for_update=True)
	if not _can_review_request(doc, for_update=True):
		frappe.throw(_("You can only review exception requests assigned to you."), frappe.PermissionError)
	if doc.employee_user == frappe.session.user:
		frappe.throw(_("A worker cannot review their own production exception."), frappe.PermissionError)
	if not job_card or any(
		doc.get(fieldname) != job_card.get(fieldname)
		for fieldname in ("work_order", "company", "operation", "operation_id")
	):
		frappe.throw(_("Production exception no longer matches its Job Card."))
	return job_card, doc


def _set_review_audit(doc):
	if doc.reviewed_at:
		return
	audit = request_audit()
	doc.reviewed_by = frappe.session.user
	doc.reviewed_at = now_datetime()
	doc.review_ip = audit.ip
	doc.review_user_agent = audit.user_agent
	doc.rejection_reason = None


def _make_material_stock_entry(doc):
	if doc.stock_entry:
		docstatus = frappe.db.get_value("Stock Entry", doc.stock_entry, "docstatus")
		if docstatus in (0, 1):
			return frappe.get_doc("Stock Entry", doc.stock_entry)
		doc.stock_entry = None

	job_card = job_card_values(doc.job_card, for_update=True)
	row = _assignment_material_row(
		doc.assignment,
		job_card,
		doc.material_key,
		exclude_request=doc.name,
		for_update=True,
	)
	precision = frappe.get_precision("Stock Entry Detail", "transfer_qty") or 6
	if flt(doc.qty, precision) > flt(row.requestable_qty, precision):
		frappe.throw(_("Only {0} of material {1} remains returnable from WIP.").format(row.requestable_qty, doc.item_code))
	if doc.source_warehouse != row.source_warehouse:
		frappe.throw(_("The approved source warehouse no longer matches the WIP material."))
	expected_target = _target_warehouse(row, doc.request_type, doc.cause)
	# Historical approved routes remain auditable; never silently redirect them.
	if not doc.get("material_action") and doc.request_type == MATERIAL_RETURN:
		expected_target = row.return_warehouse
	if not expected_target or doc.target_warehouse != expected_target:
		frappe.throw(_("The approved target warehouse no longer matches the Work Order."))

	stock_entry = frappe.new_doc("Stock Entry")
	stock_entry.company = doc.company
	stock_entry.purpose = "Material Transfer for Manufacture"
	stock_entry.work_order = doc.work_order
	stock_entry.is_return = 1
	stock_entry.custom_return_source_warehouse = row.return_warehouse
	stock_entry.from_warehouse = doc.source_warehouse
	stock_entry.to_warehouse = doc.target_warehouse
	stock_entry.custom_production_exception_request = doc.name
	stock_entry.remarks = _("Created from production exception request {0}.").format(doc.name)
	from process_simplification.production_exceptions.material_routes import stock_rows
	for value in stock_rows(row, doc.qty, doc.target_warehouse):
		value["custom_return_source_warehouse"] = row.return_warehouse
		stock_entry.append("items", value)
	stock_entry.set_stock_entry_type()
	stock_entry.insert(ignore_permissions=True)
	return stock_entry


def _apply_process_loss(job_card_values_row, doc):
	assert_supported_job_card(job_card_values_row, for_update=True)
	available = _process_loss_capacity(
		job_card_values_row,
		exclude_request=doc.name,
		for_update=True,
	)
	precision = job_card_qty_precision()
	if flt(doc.qty, precision) > flt(available, precision):
		frappe.throw(_("This Job Card now allows at most {0} process loss.").format(available))

	job_card = frappe.get_doc("Job Card", doc.job_card, for_update=True)
	job_card.process_loss_qty = flt(job_card.process_loss_qty, precision) + flt(doc.qty, precision)
	job_card.pending_qty = max(
		flt(job_card.for_quantity, precision)
		- flt(job_card.total_completed_qty, precision)
		- flt(job_card.process_loss_qty, precision),
		0,
	)
	job_card.flags.production_exception_approval = doc.name
	job_card.save(ignore_permissions=True)

	_set_review_audit(doc)
	doc.status = APPLIED
	_save_request(doc)

	is_complete = flt(
		flt(job_card.total_completed_qty, precision) + flt(job_card.process_loss_qty, precision),
		precision,
	) == flt(job_card.for_quantity, precision)
	if is_complete:
		if frappe.db.exists("Job Card Work Report", {"job_card": job_card.name, "status": "In Progress"}):
			frappe.throw(_("Cancel active worker timers before approving the final process loss."))
		if pending_report_qty(job_card.name, for_update=True):
			frappe.throw(_("Review pending work reports before approving the final process loss."))
		from process_simplification.production_reporting.service import _allow_work_order_update_for_approval

		with _allow_work_order_update_for_approval(job_card.work_order):
			job_card.flags.ignore_permissions = True
			job_card.submit()
	return doc


def approve_exception(name: str, material_action=None):
	job_card, doc = _lock_request_for_review(name)
	if material_action is not None and doc.status == PENDING_APPROVAL and doc.request_type in MATERIAL_REQUEST_TYPES:
		if material_action not in {CONTINUE_PRODUCTION, RELEASE_ASSIGNMENT}:
			frappe.throw("请选择保留任务或交回剩余任务。")
		doc.material_action = material_action
		doc.flags.material_decision = True
	if doc.status in {APPLIED, COMPLETED, AWAITING_STOCK_ENTRY}:
		return doc
	if doc.status not in {PENDING_APPROVAL, APPROVED}:
		frappe.throw(_("Only a pending or approved exception can be applied."))
	if doc.request_type == PROCESS_LOSS:
		doc = _apply_process_loss(job_card, doc)
		from process_simplification.notifications import notify_exception_approved

		notify_exception_approved(doc)
		return doc

	_set_review_audit(doc)
	doc.status = APPROVED
	_save_request(doc)
	stock_entry = _make_material_stock_entry(doc)
	doc.stock_entry = stock_entry.name
	doc.status = COMPLETED if stock_entry.docstatus == 1 else AWAITING_STOCK_ENTRY
	doc = _save_request(doc)
	from process_simplification.notifications import notify_exception_approved

	notify_exception_approved(doc)
	return doc


def reject_exception(name: str, reason: str):
	reason = str(reason or "").strip()
	if not reason:
		frappe.throw(_("A rejection reason is required."))
	_, doc = _lock_request_for_review(name)
	if doc.status == REJECTED:
		return doc
	if doc.status != PENDING_APPROVAL:
		frappe.throw(_("Only a pending exception request can be rejected."))
	_set_review_audit(doc)
	doc.status = REJECTED
	doc.rejection_reason = reason[:1000]
	doc = _save_request(doc)
	from process_simplification.notifications import notify_exception_rejected

	notify_exception_rejected(doc)
	return doc


def _request_name_for_stock_entry(stock_entry) -> str | None:
	request_name = stock_entry.get("custom_production_exception_request")
	is_new = getattr(stock_entry, "is_new", None)
	if (
		not frappe.db.table_exists("Production Exception Request")
		or (callable(is_new) and is_new())
		or not stock_entry.get("name")
	):
		return request_name
	backlinks = frappe.get_all(
		"Production Exception Request",
		filters={"stock_entry": stock_entry.name},
		pluck="name",
		limit=2,
	)
	if len(backlinks) > 1:
		frappe.throw(_("More than one production exception points to this Stock Entry."))
	if backlinks and request_name and backlinks[0] != request_name:
		frappe.throw(_("The Stock Entry production-exception link is inconsistent."))
	return request_name or (backlinks[0] if backlinks else None)


def validate_linked_stock_entry(stock_entry):
	request_name = _request_name_for_stock_entry(stock_entry)
	if not request_name:
		return
	# Read-only fields are a UI affordance, not a server trust boundary. Restore
	# the backlink in memory so a crafted update cannot detach an approved draft
	# immediately before submission.
	stock_entry.custom_production_exception_request = request_name
	initial = frappe.db.get_value(
		"Production Exception Request",
		request_name,
		["name", "job_card", "work_order", "assignment"],
		as_dict=True,
	)
	if not initial:
		frappe.throw(_("The linked production exception no longer exists."))
	# Keep the reporting/exception lock order Job Card -> Work Order -> assignment
	# -> request so a stock operator cannot race a worker's report or return limit.
	job_card = job_card_values(initial.job_card, for_update=True)
	if initial.work_order:
		frappe.db.get_value("Work Order", initial.work_order, "name", for_update=True)
	if initial.assignment:
		frappe.db.get_value(
			"Job Card Worker Assignment",
			initial.assignment,
			"name",
			for_update=True,
		)
	doc = frappe.get_doc("Production Exception Request", request_name, for_update=True)
	if doc.status == WITHDRAWN:
		frappe.throw(_("该申请已撤回，原库存草稿仅保留追溯，不能再提交。请按正确数量重新申请。"))
	if (
		doc.request_type not in MATERIAL_REQUEST_TYPES
		or doc.status != AWAITING_STOCK_ENTRY
		or doc.stock_entry != stock_entry.name
	):
		frappe.throw(_("The linked production exception is not awaiting this Stock Entry."))
	if (
		stock_entry.purpose != "Material Transfer for Manufacture"
		or not stock_entry.is_return
		or stock_entry.work_order != doc.work_order
		or stock_entry.company != doc.company
	):
		frappe.throw(_("The Stock Entry no longer matches the approved production exception."))
	approved_route = _material_row(doc.work_order, doc.material_key, exclude_request=doc.name, for_update=True)
	if stock_entry.get("custom_return_source_warehouse") != approved_route.return_warehouse:
		# Existing drafts created before this field was installed are backfilled
		# from the immutable request route, not from browser input.
		stock_entry.custom_return_source_warehouse = approved_route.return_warehouse
	precision = frappe.get_precision("Stock Entry Detail", "transfer_qty") or 6
	if not stock_entry.items or any(
		r.item_code != doc.item_code or (r.original_item or r.item_code) != (doc.original_item or doc.item_code)
		or r.s_warehouse != doc.source_warehouse or r.t_warehouse != doc.target_warehouse
		for r in stock_entry.items
	) or flt(sum(flt(r.transfer_qty or r.qty) for r in stock_entry.items), precision) != flt(doc.qty, precision):
		frappe.throw(_("The Stock Entry item or quantity differs from the approved production exception."))
	for row in stock_entry.items:
		row.custom_return_source_warehouse = approved_route.return_warehouse
	from process_simplification.production_exceptions.handling import validate_lots, validate_physical_stock
	validate_lots(stock_entry, doc, {approved_route.key: approved_route})
	validate_physical_stock(stock_entry)
	available = _assignment_material_row(
		doc.assignment,
		job_card,
		doc.material_key,
		exclude_request=doc.name,
		for_update=True,
	).requestable_qty
	if flt(doc.qty, precision) > flt(available, precision):
		frappe.throw(_("The approved material quantity is no longer available in WIP."))


def prevent_linked_stock_entry_delete(stock_entry):
	if _request_name_for_stock_entry(stock_entry):
		frappe.throw(
			_(
				"A Stock Entry linked to a production exception is an audit record and cannot be deleted. "
				"Cancel a submitted entry; the approved request can then generate a replacement draft."
			)
		)


def complete_linked_stock_entry(stock_entry):
	request_name = stock_entry.get("custom_production_exception_request")
	if not request_name:
		return
	doc = frappe.get_doc("Production Exception Request", request_name, for_update=True)
	if doc.stock_entry != stock_entry.name:
		frappe.throw(_("The submitted Stock Entry is not the one linked to this production exception."))
	doc.status = COMPLETED
	doc.processed_by = frappe.session.user
	doc.processed_at = now_datetime()
	_save_request(doc)
	ensure_completed_material_release(doc, for_update=True)
	from process_simplification.notifications import notify_stock_entry_completed

	notify_stock_entry_completed(doc)


def validate_cancel_linked_stock_entry(stock_entry):
	request_name = _request_name_for_stock_entry(stock_entry)
	if not request_name:
		return
	initial = frappe.db.get_value("Production Exception Request", request_name,
		["job_card", "work_order", "assignment"], as_dict=True)
	if not initial:
		frappe.throw(_("退料申请不存在，不能取消关联库存单。"))
	job_card_values(initial.job_card, for_update=True)
	frappe.db.get_value("Work Order", initial.work_order, "name", for_update=True)
	frappe.db.get_value("Job Card Worker Assignment", initial.assignment, "name", for_update=True)
	request = frappe.get_doc("Production Exception Request", request_name, for_update=True)
	from process_simplification.production_reporting.assignment_movement import assert_request_release_cancellable

	assert_request_release_cancellable(request)


def reopen_cancelled_stock_entry(stock_entry):
	request_name = stock_entry.get("custom_production_exception_request")
	if not request_name:
		return
	doc = frappe.get_doc("Production Exception Request", request_name, for_update=True)
	if doc.stock_entry != stock_entry.name:
		return
	from process_simplification.production_reporting.assignment_movement import reverse_request_releases

	reverse_request_releases(doc, stock_entry.name)
	doc.status = APPROVED
	doc.stock_entry = None
	doc.processed_by = None
	doc.processed_at = None
	_save_request(doc)
	from process_simplification.notifications import notify_stock_entry_cancelled

	notify_stock_entry_cancelled(doc)


def withdraw_exception(name: str, reason: str):
	"""Withdraw an unposted request; its draft remains a non-postable audit record."""
	reason = str(reason or "").strip()
	if not reason:
		frappe.throw(_("请填写撤回原因。"))
	initial = frappe.db.get_value("Production Exception Request", name, ["assignment", "employee_user", "status"], as_dict=True)
	if initial and initial.employee_user == frappe.session.user and initial.status in {PENDING_APPROVAL, WITHDRAWN}:
		_lock_worker_assignment(initial.assignment, require_executable=False)
		doc = frappe.get_doc("Production Exception Request", name, for_update=True)
		if doc.status not in {PENDING_APPROVAL, WITHDRAWN}:
			frappe.throw("主管已处理此申请，请联系主管撤回。")
	else:
		_, doc = _lock_request_for_review(name)
	if doc.status == WITHDRAWN:
		return doc
	if doc.status not in OPEN_MATERIAL_STATUSES:
		frappe.throw(_("只能撤回尚未过账的申请；已过账请由库房处理原库存单。"))
	if doc.stock_entry and frappe.db.get_value("Stock Entry", doc.stock_entry, "docstatus") != 0:
		frappe.throw(_("库存单状态已变化，请刷新后重试。"))
	doc.status = WITHDRAWN
	doc.withdrawn_by = frappe.session.user
	doc.withdrawn_at = now_datetime()
	doc.withdrawal_reason = reason[:1000]
	if _can_review_request(doc):
		_set_review_audit(doc)
	return _save_request(doc)
