from __future__ import annotations

import hashlib

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import flt, now_datetime

from process_simplification.management_access import WAREHOUSE_OPERATOR_ROLE, user_company_scope
from process_simplification.production_exceptions.constants import (
	APPLIED,
	APPROVED,
	AWAITING_STOCK_ENTRY,
	CAUSES,
	COMPLETED,
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
IMMUTABLE_FIELDS = (
	"request_key",
	"request_type",
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
	if old and any(old.get(fieldname) != doc.get(fieldname) for fieldname in IMMUTABLE_FIELDS):
		frappe.throw(_("Production exception request facts are immutable."))


def _lock_worker_assignment(assignment_name: str):
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
		or assignment.status != "Active"
	):
		frappe.throw(_("You can only submit an exception for your own active assignment."), frappe.PermissionError)
	for fieldname in ("job_card", "work_order", "company", "operation", "operation_id"):
		if assignment.get(fieldname) != job_card.get(fieldname if fieldname != "job_card" else "name"):
			frappe.throw(_("Worker assignment no longer matches its Job Card."))
	assert_supported_job_card(job_card, for_update=True)
	return assignment, job_card


def _open_material_qty(
	work_order: str,
	item_code: str,
	source_warehouse: str,
	*,
	exclude_request: str | None = None,
	for_update: bool = False,
) -> float:
	request = frappe.qb.DocType("Production Exception Request")
	condition = (
		(request.work_order == work_order)
		& (request.item_code == item_code)
		& (request.source_warehouse == source_warehouse)
		& (request.status.isin(sorted(OPEN_MATERIAL_STATUSES)))
	)
	if exclude_request:
		condition &= request.name != exclude_request
	query = frappe.qb.from_(request).select(request.name, request.qty).where(condition)
	if for_update:
		query = query.for_update()
	return sum(flt(row.qty) for row in query.run(as_dict=True))


def _native_material_rows(work_order: str, *, exclude_request: str | None = None, for_update=False):
	from erpnext.stock.doctype.stock_entry.stock_entry import get_available_materials

	work_order_values = frappe.db.get_value(
		"Work Order",
		work_order,
		["name", "company", "wip_warehouse", "scrap_warehouse"],
		as_dict=True,
		for_update=for_update,
	)
	if not work_order_values:
		frappe.throw(_("Work Order does not exist."))
	stock_entry = frappe.qb.DocType("Stock Entry")
	stock_entry_detail = frappe.qb.DocType("Stock Entry Detail")
	returned_rows = (
		frappe.qb.from_(stock_entry)
		.inner_join(stock_entry_detail)
		.on(stock_entry_detail.parent == stock_entry.name)
		.select(
			stock_entry_detail.item_code,
			stock_entry_detail.s_warehouse,
			Sum(stock_entry_detail.transfer_qty).as_("qty"),
		)
		.where(
			(stock_entry.docstatus == 1)
			& (stock_entry.work_order == work_order)
			& (stock_entry.purpose == "Material Transfer for Manufacture")
			& (stock_entry.is_return == 1)
		)
		.groupby(stock_entry_detail.item_code, stock_entry_detail.s_warehouse)
	).run(as_dict=True)
	returned_by_route = {
		(row.item_code, row.s_warehouse): flt(row.qty) for row in returned_rows
	}
	available_materials = list(get_available_materials(work_order).values())
	item_codes = sorted(
		{available.item_details.get("item_code") for available in available_materials}
		- {None, ""}
	)
	item_names = (
		{
			row.item_code: row.item_name
			for row in frappe.get_all(
				"Item",
				filters={"item_code": ["in", item_codes]},
				fields=["item_code", "item_name"],
				limit=0,
			)
		}
		if item_codes
		else {}
	)
	rows = []
	for available in available_materials:
		item = available.item_details
		source_warehouse = item.get("warehouse") or work_order_values.wip_warehouse
		# ERPNext's helper also returns earlier return rows with their target as
		# ``warehouse``.  Only the original WIP balance is an eligible source.
		if source_warehouse != work_order_values.wip_warehouse:
			continue
		return_warehouse = item.get("s_warehouse") or frappe.db.get_value(
			"Work Order Item",
			{"parent": work_order, "item_code": item.item_code},
			"source_warehouse",
		)
		returned_qty = returned_by_route.get((item.item_code, source_warehouse), 0)
		qty = max(flt(available.qty) - flt(returned_qty), 0)
		if qty <= 0 or not source_warehouse or not return_warehouse:
			continue
		reserved = _open_material_qty(
			work_order,
			item.item_code,
			source_warehouse,
			exclude_request=exclude_request,
			for_update=for_update,
		)
		requestable_qty = max(qty - reserved, 0)
		key = _material_key(item.item_code, source_warehouse, return_warehouse)
		rows.append(
			frappe._dict(
				key=key,
				item_code=item.item_code,
				item_name=item_names.get(item.item_code) or item.item_name or item.item_code,
				stock_uom=item.stock_uom,
				source_warehouse=source_warehouse,
				return_warehouse=return_warehouse,
				scrap_warehouse=work_order_values.scrap_warehouse,
				native_available_qty=qty,
				reserved_request_qty=reserved,
				requestable_qty=requestable_qty,
			)
		)
	return rows


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
	materials = _native_material_rows(job_card.work_order)
	return {
		"assignment": assignment_values.name,
		"job_card": job_card.name,
		"work_order": job_card.work_order,
		"operation": job_card.operation,
		"process_loss_available_qty": _process_loss_capacity(job_card),
		"materials": [row for row in materials if flt(row.requestable_qty) > 0],
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
):
	request_type = str(request_type or "").strip()
	cause = str(cause or "").strip()
	reason = str(reason or "").strip()
	request_key = str(request_key or "").strip()
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
		):
			frappe.throw(_("This exception request id was already used with different values."))
		return existing

	assignment_values, job_card = _lock_worker_assignment(assignment)
	values = {}
	if request_type in MATERIAL_REQUEST_TYPES:
		row = _material_row(job_card.work_order, material_key, for_update=True)
		if qty > flt(row.requestable_qty, precision):
			frappe.throw(_("This material currently allows at most {0} to be requested.").format(row.requestable_qty))
		target_warehouse = row.return_warehouse
		if request_type == MATERIAL_SCRAP:
			target_warehouse = row.scrap_warehouse
			if not target_warehouse:
				frappe.throw(_("Configure a Scrap Warehouse on the Work Order before requesting material scrap."))
		values.update(
			material_key=row.key,
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
		}
		if not can_review and not stock_visible:
			continue
		row.can_approve = bool(can_review and row.status in {PENDING_APPROVAL, APPROVED})
		row.can_reject = bool(can_review and row.status == PENDING_APPROVAL)
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
		"processed": [row for row in visible if row.status in {APPLIED, COMPLETED, REJECTED}],
	}


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

	row = _material_row(
		doc.work_order,
		doc.material_key,
		exclude_request=doc.name,
		for_update=True,
	)
	precision = frappe.get_precision("Stock Entry Detail", "transfer_qty") or 6
	if flt(doc.qty, precision) > flt(row.requestable_qty, precision):
		frappe.throw(_("Only {0} of material {1} remains returnable from WIP.").format(row.requestable_qty, doc.item_code))
	if doc.source_warehouse != row.source_warehouse:
		frappe.throw(_("The approved source warehouse no longer matches the WIP material."))
	expected_target = row.return_warehouse if doc.request_type == MATERIAL_RETURN else row.scrap_warehouse
	if not expected_target or doc.target_warehouse != expected_target:
		frappe.throw(_("The approved target warehouse no longer matches the Work Order."))

	stock_entry = frappe.new_doc("Stock Entry")
	stock_entry.company = doc.company
	stock_entry.purpose = "Material Transfer for Manufacture"
	stock_entry.work_order = doc.work_order
	stock_entry.is_return = 1
	stock_entry.from_warehouse = doc.source_warehouse
	stock_entry.to_warehouse = doc.target_warehouse
	stock_entry.custom_production_exception_request = doc.name
	stock_entry.remarks = _("Created from production exception request {0}.").format(doc.name)
	stock_entry.append(
		"items",
		{
			"item_code": doc.item_code,
			"qty": flt(doc.qty),
			"uom": doc.stock_uom,
			"stock_uom": doc.stock_uom,
			"conversion_factor": 1,
			"s_warehouse": doc.source_warehouse,
			"t_warehouse": doc.target_warehouse,
			"original_item": doc.item_code,
		},
	)
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


def approve_exception(name: str):
	job_card, doc = _lock_request_for_review(name)
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
		["name", "work_order"],
		as_dict=True,
	)
	if not initial:
		frappe.throw(_("The linked production exception no longer exists."))
	# Keep the common lock order Work Order -> request.  This avoids reversing
	# the supervisor approval path when a draft is submitted concurrently.
	if initial.work_order:
		frappe.db.get_value("Work Order", initial.work_order, "name", for_update=True)
	doc = frappe.get_doc("Production Exception Request", request_name, for_update=True)
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
	if len(stock_entry.items) != 1:
		frappe.throw(_("A production-exception Stock Entry must contain exactly one approved item."))
	row = stock_entry.items[0]
	precision = frappe.get_precision("Stock Entry Detail", "transfer_qty") or 6
	row_qty = flt(row.transfer_qty or row.qty, precision)
	if (
		row.item_code != doc.item_code
		or row.s_warehouse != doc.source_warehouse
		or row.t_warehouse != doc.target_warehouse
		or row_qty != flt(doc.qty, precision)
	):
		frappe.throw(_("The Stock Entry item or quantity differs from the approved production exception."))
	available = _material_row(
		doc.work_order,
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
	from process_simplification.notifications import notify_stock_entry_completed

	notify_stock_entry_completed(doc)


def reopen_cancelled_stock_entry(stock_entry):
	request_name = stock_entry.get("custom_production_exception_request")
	if not request_name:
		return
	doc = frappe.get_doc("Production Exception Request", request_name, for_update=True)
	if doc.stock_entry != stock_entry.name:
		return
	doc.status = APPROVED
	doc.stock_entry = None
	doc.processed_by = None
	doc.processed_at = None
	_save_request(doc)
	from process_simplification.notifications import notify_stock_entry_cancelled

	notify_stock_entry_cancelled(doc)
