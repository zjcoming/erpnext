from __future__ import annotations

import hashlib
from contextlib import contextmanager
from math import ceil

import frappe
from frappe import _
from frappe.query_builder import Order
from frappe.utils import (
	cint,
	flt,
	get_datetime,
	get_first_day,
	get_last_day,
	getdate,
	now_datetime,
	nowdate,
	time_diff_in_hours,
)

from process_simplification.production_reporting.constants import (
	ASSIGNMENT_STATUSES,
	REPORT_STATUSES,
	REVIEW_ROLES,
	SUPERVISOR_ROLE,
	SYSTEM_MANAGER_ROLE,
	WAGE_ROLES,
)
from process_simplification.production_reporting.domain import (
	WAGE_TYPES,
	approved_report_qty,
	assert_worker_user_isolated,
	assert_supported_job_card,
	daily_minutes_limit,
	employee_for_user,
	employee_user,
	get_wage_rate,
	get_wage_rates,
	is_admin_reviewer,
	job_card_block,
	job_card_qty_precision,
	job_card_values,
	material_reportable_qty,
	money,
	pending_report_qty,
	reportable_qty,
	request_audit,
	require_reviewer,
	require_wage_manager,
	require_worker,
	user_roles,
	wage_manager_companies,
	work_order_material_capacity,
)


MAX_WORK_SESSION_MINUTES = 24 * 60
DEFAULT_REVIEW_PAGE_LENGTH = 20
MAX_REVIEW_PAGE_LENGTH = 100


def _hash_key(*values) -> str:
	return hashlib.sha256("|".join(str(value or "") for value in values).encode()).hexdigest()


def manual_time_entry_enabled() -> bool:
	"""Return the global time-wage input mode, defaulting safely to manual entry."""
	if not frappe.db.exists("DocType", "Process Simplification Settings"):
		return True
	value = frappe.db.get_single_value(
		"Process Simplification Settings", "allow_manual_time_entry"
	)
	return True if value in (None, "") else bool(cint(value))


def _work_report_wage_options(doc) -> list[frappe._dict]:
	"""Return the wage choices frozen when this work session started."""
	options = []
	piecework_rate = flt(doc.get("piecework_rate_snapshot"))
	hourly_rate = flt(doc.get("hourly_rate_snapshot"))
	if piecework_rate > 0:
		options.append(frappe._dict(wage_type="Piecework", rate=piecework_rate))
	if hourly_rate > 0:
		options.append(frappe._dict(wage_type="Time", rate=hourly_rate))

	# Reports created before dual-method snapshots were introduced retain their
	# original single frozen choice. Never substitute a live rule for old work.
	if not options and doc.get("wage_type") in WAGE_TYPES and flt(doc.get("rate")) > 0:
		options.append(
			frappe._dict(wage_type=doc.get("wage_type"), rate=flt(doc.get("rate")))
		)
	return options


def _time_manual_entry_available(doc) -> bool:
	if flt(doc.get("hourly_rate_snapshot")) > 0:
		return bool(cint(doc.get("time_manual_entry_snapshot")))
	if not flt(doc.get("piecework_rate_snapshot")) and doc.get("wage_type") == "Time":
		return bool(cint(doc.get("manual_time_entry")))
	return False


def _select_work_report_wage_option(doc, wage_type: str | None = None) -> frappe._dict:
	requested_type = str(wage_type or "").strip()
	if requested_type and requested_type not in WAGE_TYPES:
		frappe.throw(_("Wage type must be Piecework or Time."))
	options = _work_report_wage_options(doc)
	if not options:
		frappe.throw(_("The work session has no frozen wage option."))
	if not requested_type:
		# Options are deliberately ordered Piecework then Time.
		return options[0]
	for option in options:
		if option.wage_type == requested_type:
			return option
	frappe.throw(_("The selected wage type was not enabled when this work session started."))


def _validate_work_report_wage_selection(doc) -> None:
	selected = _select_work_report_wage_option(doc, doc.get("wage_type"))
	if flt(selected.rate, 6) != flt(doc.get("rate"), 6):
		frappe.throw(_("The selected wage rate does not match its frozen work-session snapshot."))
	expected_manual = bool(
		doc.get("wage_type") == "Time" and _time_manual_entry_available(doc)
	)
	if bool(cint(doc.get("manual_time_entry"))) != expected_manual:
		frappe.throw(_("The time-wage input mode does not match its frozen work-session snapshot."))


def _time_segments(doc):
	return list(doc.get("time_segments") or [])


def _segment_duration(started_at, ended_at) -> float:
	return flt(time_diff_in_hours(get_datetime(ended_at), get_datetime(started_at)) * 60, 6)


def _append_time_segment(doc, started_at, request_key: str):
	segments = _time_segments(doc)
	if segments and not segments[-1].ended_at:
		frappe.throw(_("The work timer is already running."))
	return doc.append(
		"time_segments",
		{
			"started_at": get_datetime(started_at),
			"start_request_key": request_key,
		},
	)


def _ensure_open_time_segment(doc):
	segments = _time_segments(doc)
	if not segments:
		return _append_time_segment(doc, doc.actual_start_time, doc.request_key)
	if segments[-1].ended_at:
		frappe.throw(_("The work timer is paused."))
	return segments[-1]


def _close_time_segment(doc, ended_at, request_key: str):
	segment = _ensure_open_time_segment(doc)
	ended_at = get_datetime(ended_at)
	if ended_at <= get_datetime(segment.started_at):
		frappe.throw(_("Timer stop time must be later than its start time."))
	segment.ended_at = ended_at
	segment.duration_minutes = _segment_duration(segment.started_at, ended_at)
	segment.stop_request_key = request_key
	return segment


def _captured_timer_minutes(doc, *, include_running_until=None) -> float:
	total = 0.0
	segments = _time_segments(doc)
	if not segments and doc.actual_start_time and include_running_until:
		return _segment_duration(doc.actual_start_time, include_running_until)
	for segment in segments:
		if segment.ended_at:
			total += _segment_duration(segment.started_at, segment.ended_at)
		elif include_running_until:
			total += _segment_duration(segment.started_at, include_running_until)
	return flt(total, 6)


def _validate_time_segments(doc):
	segments = _time_segments(doc)
	if not segments:
		return
	previous_end = None
	for index, segment in enumerate(segments):
		if not segment.started_at:
			frappe.throw(_("Every timer segment requires a start time."))
		started_at = get_datetime(segment.started_at)
		if index == 0 and get_datetime(doc.actual_start_time) != started_at:
			frappe.throw(_("The first timer segment must match the work session start time."))
		if previous_end and started_at < previous_end:
			frappe.throw(_("Work timer segments cannot overlap."))
		if segment.ended_at:
			ended_at = get_datetime(segment.ended_at)
			if ended_at <= started_at:
				frappe.throw(_("Timer segment end time must be later than its start time."))
			duration = _segment_duration(started_at, ended_at)
			if flt(segment.duration_minutes, 6) != duration:
				frappe.throw(_("Timer segment duration does not match its captured times."))
			previous_end = ended_at
		elif index != len(segments) - 1:
			frappe.throw(_("Only the last timer segment may still be running."))

	open_segment = bool(segments and not segments[-1].ended_at)
	if doc.status == "In Progress":
		if doc.timer_paused_at:
			if open_segment or get_datetime(doc.timer_paused_at) != get_datetime(segments[-1].ended_at):
				frappe.throw(_("Paused timer state does not match its last segment."))
		elif not open_segment:
			frappe.throw(_("An active timer must be running or explicitly paused."))
	else:
		if open_segment or doc.timer_paused_at:
			frappe.throw(_("A submitted work report cannot retain an active or paused timer."))
		if flt(doc.actual_minutes, 6) != _captured_timer_minutes(doc):
			frappe.throw(_("Actual production minutes must equal the captured timer segments."))
		if get_datetime(doc.actual_end_time) != get_datetime(segments[-1].ended_at):
			frappe.throw(_("Actual end time must match the final timer segment."))


@contextmanager
def _allow_work_order_update_for_approval(work_order: str):
	"""Permit only the named parent Work Order update without changing web identity."""
	previous_work_order = getattr(
		frappe.flags, "worker_reporting_approval_work_order", None
	)
	try:
		frappe.flags.worker_reporting_approval_work_order = work_order
		yield
	finally:
		if previous_work_order is None:
			frappe.flags.pop("worker_reporting_approval_work_order", None)
		else:
			frappe.flags.worker_reporting_approval_work_order = previous_work_order


def _assert_reviewer_scope(supervisor: str, *, for_update: bool = False):
	# Administrator has virtual all-role semantics and its live Desk session updates
	# the User row. Locking that special row adds contention without protecting any
	# role data, because Administrator has no mutable Has Role rows to validate.
	if supervisor != "Administrator" and not frappe.db.get_value(
		"User", supervisor, "enabled", for_update=for_update
	):
		frappe.throw(_("The reviewing supervisor User is disabled."))
	if not user_roles(supervisor, for_update=for_update).intersection(REVIEW_ROLES):
		frappe.throw(_("The reviewing supervisor must have a production reporting review role."))
	if not is_admin_reviewer(for_update=for_update) and supervisor != frappe.session.user:
		frappe.throw(_("A production supervisor can only manage their own assignments."), frappe.PermissionError)


def _supervisor_companies(supervisor: str) -> set[str] | None:
	if SYSTEM_MANAGER_ROLE in user_roles(supervisor):
		return None
	companies = set(
		frappe.db.sql(
			"""
			select for_value
			from `tabUser Permission`
			where user = %s
				and allow = 'Company'
				and ifnull(applicable_for, '') = ''
			""",
			supervisor,
			pluck=True,
		)
	)
	companies.update(
		frappe.get_all(
			"Employee",
			filters={"user_id": supervisor, "status": "Active"},
			pluck="company",
			limit=0,
		)
	)
	if not companies:
		frappe.throw(
			_("Production supervisor {0} must be linked to an active Employee or a Company User Permission.").format(
				supervisor
			)
		)
	return companies


def _assert_supervisor_company(supervisor: str, company: str):
	companies = _supervisor_companies(supervisor)
	if companies is not None and company not in companies:
		frappe.throw(
			_("Production supervisor {0} is not permitted for company {1}.").format(supervisor, company),
			frappe.PermissionError,
		)


def _assert_report_facts_match_job_card(job_card, *, for_update: bool = False):
	precision = job_card_qty_precision()
	expected = approved_report_qty(job_card.name, for_update=for_update)
	if flt(job_card.total_completed_qty, precision) != flt(expected, precision):
		frappe.throw(
			_(
				"Job Card completed quantity does not match approved work reports. "
				"Do not use standard Shop Floor completion on a worker-reporting Job Card."
			)
		)


def validate_assignment_document(doc):
	if not getattr(doc.flags, "worker_reporting_action", False):
		frappe.throw(_("Worker assignments can only be changed through Production Report Review."))
	if doc.status not in ASSIGNMENT_STATUSES:
		frappe.throw(_("Invalid worker assignment status."))
	if not doc.assignment_key or not doc.job_card or not doc.operation_id or not doc.employee or not doc.supervisor:
		frappe.throw(_("Worker assignment is missing required immutable facts."))
	if doc.is_new():
		return
	old = frappe.db.get_value(
		doc.doctype,
		doc.name,
		[
			"assignment_key",
			"job_card",
			"work_order",
			"company",
			"operation",
			"operation_id",
			"job_card_qty",
			"employee",
			"employee_user",
			"supervisor",
		],
		as_dict=True,
	)
	if old and any(old.get(fieldname) != doc.get(fieldname) for fieldname in old):
		frappe.throw(_("Worker assignment facts are immutable."))


def validate_assignment_deletion(doc):
	if not getattr(doc.flags, "worker_reporting_action", False):
		frappe.throw(_("Worker assignments can only be removed through Production Report Review."))
	if frappe.db.exists("Job Card Work Report", {"assignment": doc.name}):
		frappe.throw(_("An assignment with work reports cannot be removed."))


def validate_report_document(doc):
	if not getattr(doc.flags, "worker_reporting_action", False):
		frappe.throw(_("Work reports can only be changed through reporting actions."))
	if doc.status not in REPORT_STATUSES:
		frappe.throw(_("Invalid work report status."))
	assignment = frappe.db.get_value(
		"Job Card Worker Assignment",
		doc.assignment,
		["job_card", "work_order", "company", "operation", "operation_id", "employee", "employee_user"],
		as_dict=True,
	)
	if not assignment:
		frappe.throw(_("The work report must reference an existing worker assignment."))
	doc.employee_name = frappe.db.get_value("Employee", doc.employee, "employee_name")
	for fieldname in (
		"job_card",
		"work_order",
		"company",
		"operation",
		"operation_id",
		"employee",
	):
		if doc.get(fieldname) != assignment.get(fieldname):
			frappe.throw(_("Work report identity does not match its worker assignment."))
	if not doc.request_key or not doc.wage_rate or flt(doc.rate) <= 0:
		frappe.throw(_("Work session is missing its immutable request or wage-rate snapshot."))
	_validate_work_report_wage_selection(doc)
	_validate_time_segments(doc)
	if doc.status == "In Progress":
		if not doc.actual_start_time:
			frappe.throw(_("An active work session requires its actual start time."))
		if (
			doc.actual_end_time
			or doc.completion_request_key
			or flt(doc.actual_minutes)
			or flt(doc.completed_qty)
			or flt(doc.reported_minutes)
			or flt(doc.wage_amount)
		):
			frappe.throw(_("An active work session cannot contain completion facts."))
	else:
		if flt(doc.completed_qty) <= 0:
			frappe.throw(_("Completed quantity must be greater than zero."))
		if doc.actual_start_time or doc.actual_end_time:
			if not (doc.actual_start_time and doc.actual_end_time and flt(doc.actual_minutes) > 0):
				frappe.throw(_("A timed report requires start time, end time, and positive actual minutes."))
			if get_datetime(doc.actual_end_time) <= get_datetime(doc.actual_start_time):
				frappe.throw(_("Actual end time must be later than actual start time."))
		if doc.wage_type == "Time":
			if flt(doc.reported_minutes) <= 0:
				frappe.throw(_("Wage minutes must be greater than zero for a time wage."))
			if (
				not cint(doc.manual_time_entry)
				and doc.actual_start_time
				and flt(doc.reported_minutes, 6) != flt(doc.actual_minutes, 6)
			):
				frappe.throw(_("Timed wage minutes must equal the captured actual minutes."))
		elif flt(doc.reported_minutes) or cint(doc.manual_time_entry):
			frappe.throw(_("Piecework reports cannot contain wage minutes or manual time mode."))
	if doc.is_new():
		return
	old = frappe.db.get_value(
		doc.doctype,
		doc.name,
		[
			"request_key",
			"completion_request_key",
			"assignment",
			"job_card",
			"work_order",
			"company",
			"operation",
			"operation_id",
			"employee",
			"employee_user",
			"labor_date",
			"wage_type",
			"manual_time_entry",
			"piecework_rate_snapshot",
			"hourly_rate_snapshot",
			"time_manual_entry_snapshot",
			"status",
			"actual_start_time",
			"actual_end_time",
			"actual_minutes",
			"completed_qty",
			"reported_minutes",
			"wage_rate",
			"wage_rate_revision",
			"rate",
			"wage_amount",
		],
		as_dict=True,
	)
	if not old:
		return
	identity_fields = {
		"request_key",
		"assignment",
		"job_card",
		"work_order",
		"company",
		"operation",
		"operation_id",
		"employee",
		"employee_user",
		"labor_date",
		"actual_start_time",
		"wage_rate",
		"wage_rate_revision",
		"piecework_rate_snapshot",
		"hourly_rate_snapshot",
		"time_manual_entry_snapshot",
	}
	if any(old.get(fieldname) != doc.get(fieldname) for fieldname in identity_fields):
		frappe.throw(_("Work session identity and start facts are immutable."))
	selection_fields = {"wage_type", "manual_time_entry", "rate"}
	selection_changed = any(
		old.get(fieldname) != doc.get(fieldname) for fieldname in selection_fields
	)
	selection_at_submission = bool(
		old.status == "In Progress"
		and doc.status == "Pending Approval"
		and getattr(doc.flags, "worker_reporting_wage_selection", False)
	)
	if selection_changed and not selection_at_submission:
		frappe.throw(_("The wage method may only be selected while submitting the work report."))
	if old.status == "In Progress" and doc.status == "Pending Approval":
		return
	immutable_completion_fields = {
		"completion_request_key",
		"actual_end_time",
		"actual_minutes",
		"completed_qty",
		"reported_minutes",
		"wage_amount",
	}
	if any(old.get(fieldname) != doc.get(fieldname) for fieldname in immutable_completion_fields):
		frappe.throw(_("Submitted work report facts are immutable; reject and report again."))
	allowed_transitions = {
		"In Progress": {"In Progress"},
		"Pending Approval": {"Pending Approval", "Approved", "Rejected"},
		"Approved": {"Approved"},
		"Rejected": {"Rejected"},
	}
	if doc.status not in allowed_transitions.get(old.status, set()):
		frappe.throw(_("This work report status transition is not allowed."))


def assign_worker(job_card: str, employee: str, supervisor: str | None = None, notes: str | None = None):
	require_reviewer()
	supervisor = supervisor or frappe.session.user
	if not is_admin_reviewer() and supervisor != frappe.session.user:
		frappe.throw(_("A production supervisor can only manage their own assignments."), frappe.PermissionError)

	jc = job_card_values(job_card, for_update=True)
	if not jc:
		frappe.throw(_("Job Card does not exist."))
	if jc.work_order:
		frappe.db.get_value("Work Order", jc.work_order, "status", for_update=True)
	if not jc.work_order or not jc.company or not jc.operation or not jc.operation_id:
		frappe.throw(_("Job Card must have a Work Order, company, operation, and operation row before assignment."))
	employee_row = frappe.db.get_value(
		"Employee",
		employee,
		["company", "status", "user_id"],
		as_dict=True,
		for_update=True,
	)
	if not employee_row or employee_row.status != "Active" or employee_row.company != jc.company:
		frappe.throw(_("The worker must be an active Employee in the Job Card company."))
	worker_user = employee_user(employee, for_update=True)
	# Administrator/System Manager may assign another reviewer. Pre-lock mutable
	# reviewer users in a stable order before role checks to avoid U1 -> U2 / U2 -> U1.
	# The special Administrator row is intentionally excluded; see
	# _assert_reviewer_scope for why it has no role-drift lock requirement.
	for reviewer_user in sorted({supervisor, frappe.session.user}):
		if reviewer_user != "Administrator":
			frappe.db.get_value("User", reviewer_user, "name", for_update=True)
	_assert_reviewer_scope(supervisor, for_update=True)
	_assert_supervisor_company(supervisor, jc.company)
	if worker_user == supervisor:
		frappe.throw(_("A worker cannot supervise or approve their own production report."))

	key = _hash_key(job_card, employee)
	existing = frappe.db.get_value(
		"Job Card Worker Assignment",
		{"assignment_key": key},
		["name", "status", "supervisor"],
		as_dict=True,
		for_update=True,
	)
	if existing:
		if existing.status == "Active" and existing.supervisor == supervisor:
			return frappe.get_doc("Job Card Worker Assignment", existing.name, for_update=True)
		frappe.throw(_("This worker already has an assignment for the Job Card."))

	assignment_table = frappe.qb.DocType("Job Card Worker Assignment")
	existing_assignments = (
		frappe.qb.from_(assignment_table)
		.select(assignment_table.name, assignment_table.employee, assignment_table.supervisor)
		.where(assignment_table.job_card == job_card)
		.for_update()
	).run(as_dict=True)
	conflicting_supervisors = {
		row.supervisor for row in existing_assignments if row.supervisor != supervisor
	}
	if conflicting_supervisors:
		frappe.throw(
			_(
				"All workers on one Job Card must use the same reviewing supervisor."
			)
		)
	if not existing_assignments and (
		flt(jc.total_completed_qty)
		or frappe.db.get_value("Job Card Time Log", {"parent": job_card}, "name", for_update=True)
	):
		frappe.throw(_("Assign workers before recording any Job Card quantity or time rows."))
	# Keep the write-path lock order aligned with submit/review: configuration is
	# checked only after Job Card -> Work Order -> Employee -> Assignment locks.
	assert_supported_job_card(jc, for_update=True)
	if not get_wage_rate(jc.company, jc.operation, nowdate(), for_update=True):
		frappe.throw(_("Configure an enabled wage rate for this operation before assigning workers."))

	doc = frappe.get_doc(
		{
			"doctype": "Job Card Worker Assignment",
			"assignment_key": key,
			"job_card": jc.name,
			"work_order": jc.work_order,
			"company": jc.company,
			"operation": jc.operation,
			"operation_id": jc.operation_id,
			"job_card_qty": flt(jc.for_quantity),
			"employee": employee,
			"employee_user": worker_user,
			"supervisor": supervisor,
			"status": "Active",
			"notes": str(notes or "").strip(),
			"assigned_by": frappe.session.user,
			"assigned_at": now_datetime(),
		}
	)
	doc.flags.worker_reporting_action = True
	doc.insert(ignore_permissions=True)

	job_card_doc = frappe.get_doc("Job Card", jc.name, for_update=True)
	job_card_doc.custom_worker_reporting_enabled = 1
	job_card_doc.custom_worker_reporting_supervisor = supervisor
	# v16 keeps unfinished quantity explicit. Supervisor-approved process-loss
	# requests reduce this balance without ever becoming paid completed quantity.
	job_card_doc.pending_qty = max(
		flt(job_card_doc.for_quantity)
		- flt(job_card_doc.total_completed_qty)
		- flt(job_card_doc.process_loss_qty),
		0,
	)
	job_card_doc.flags.worker_reporting_assignment = True
	job_card_doc.save(ignore_permissions=True)
	frappe.db.set_value(
		"Work Order",
		jc.work_order,
		"custom_worker_reporting_enabled",
		1,
		update_modified=False,
	)
	return doc


def unassign_worker(assignment: str):
	require_reviewer()
	initial = frappe.db.get_value(
		"Job Card Worker Assignment",
		assignment,
		["name", "job_card", "work_order", "employee", "supervisor", "status"],
		as_dict=True,
	)
	if not initial:
		frappe.throw(_("Worker assignment does not exist."))
	jc = job_card_values(initial.job_card, for_update=True)
	if jc.work_order:
		frappe.db.get_value("Work Order", jc.work_order, "name", for_update=True)
	frappe.db.get_value("Employee", initial.employee, "name", for_update=True)
	doc = frappe.get_doc("Job Card Worker Assignment", assignment, for_update=True)
	if (
		doc.job_card != initial.job_card
		or doc.work_order != initial.work_order
		or doc.employee != initial.employee
		or doc.work_order != jc.work_order
	):
		frappe.throw(_("Worker assignment identity changed while it was being locked; retry the action."))
	_assert_reviewer_scope(doc.supervisor, for_update=True)
	if doc.status != "Active":
		frappe.throw(_("Only an active assignment can be removed."))
	if frappe.db.get_value(
		"Job Card Work Report", {"assignment": doc.name}, "name", for_update=True
	):
		frappe.throw(_("An assignment with work reports cannot be removed."))
	doc.flags.worker_reporting_action = True
	doc.delete(ignore_permissions=True)

	if not frappe.db.get_value(
		"Job Card Worker Assignment", {"job_card": doc.job_card}, "name", for_update=True
	):
		job_card_doc = frappe.get_doc("Job Card", doc.job_card, for_update=True)
		job_card_doc.custom_worker_reporting_enabled = 0
		job_card_doc.custom_worker_reporting_supervisor = None
		job_card_doc.pending_qty = 0
		job_card_doc.flags.worker_reporting_assignment = True
		job_card_doc.save(ignore_permissions=True)
	if not frappe.db.get_value(
		"Job Card Worker Assignment", {"work_order": doc.work_order}, "name", for_update=True
	):
		frappe.db.set_value(
			"Work Order",
			doc.work_order,
			"custom_worker_reporting_enabled",
			0,
			update_modified=False,
		)
	return {"ok": True}


def _daily_used_minutes(employee: str, labor_date, *, for_update: bool = False) -> float:
	report = frappe.qb.DocType("Job Card Work Report")
	query = (
		frappe.qb.from_(report)
		.select(report.name, report.reported_minutes)
		.where(
			(report.employee == employee)
			& (report.labor_date == getdate(labor_date))
			& (report.status.isin(["Pending Approval", "Approved"]))
		)
	)
	if for_update:
		query = query.for_update()
	return sum(flt(row.reported_minutes) for row in query.run(as_dict=True))


def _month_is_confirmed(
	company: str,
	employee: str,
	labor_date,
	*,
	for_update: bool = False,
) -> bool:
	return bool(
		frappe.db.get_value(
			"Monthly Worker Wage Summary",
			{
				"company": company,
				"employee": employee,
				"month_start": get_first_day(labor_date),
				"docstatus": 1,
			},
			"name",
			for_update=for_update,
		)
	)


def get_worker_dashboard():
	require_worker()
	employee = employee_for_user()
	today = getdate(nowdate())
	used_minutes = _daily_used_minutes(employee, today)
	manual_entry_enabled = manual_time_entry_enabled()
	assignments = frappe.get_all(
		"Job Card Worker Assignment",
		filters={"employee": employee, "status": "Active"},
		fields=[
			"name",
			"job_card",
			"work_order",
			"company",
			"operation",
			"operation_id",
			"job_card_qty",
			"supervisor",
			"notes",
		],
		order_by="modified desc",
		limit=0,
	)
	for assignment in assignments:
		jc = frappe.db.get_value(
			"Job Card",
			assignment.job_card,
			[
				"work_order",
				"company",
				"operation",
				"operation_id",
				"for_quantity",
				"total_completed_qty",
				"docstatus",
				"production_item",
				"workstation",
				"process_loss_qty",
				"is_corrective_job_card",
				"track_semi_finished_goods",
				"is_subcontracted",
			],
			as_dict=True,
		)
		active_session = frappe.db.get_value(
			"Job Card Work Report",
			{"assignment": assignment.name, "status": "In Progress"},
			[
				"name",
				"actual_start_time",
				"wage_type",
				"manual_time_entry",
				"piecework_rate_snapshot",
				"hourly_rate_snapshot",
				"time_manual_entry_snapshot",
				"timer_paused_at",
				"rate",
			],
			as_dict=True,
		)
		configured_rate_options = (
			get_wage_rates(assignment.company, assignment.operation, today) if jc else []
		)
		rate_options = (
			_work_report_wage_options(active_session)
			if active_session
			else configured_rate_options
		)
		rate = rate_options[0] if rate_options else None
		assignment.for_quantity = flt(jc.for_quantity) if jc else 0
		assignment.completed_qty = flt(jc.total_completed_qty) if jc else 0
		assignment.production_item = jc.production_item if jc else None
		assignment.workstation = jc.workstation if jc else None
		if jc:
			jc.name = assignment.job_card
		block = job_card_block(jc)
		if not block and any(
			assignment.get(fieldname) != jc.get(fieldname)
			for fieldname in ("work_order", "company", "operation", "operation_id")
		):
			block = frappe._dict(
				code="JOB_CARD_IDENTITY_CHANGED",
				message=_("Job Card identity changed after worker assignment."),
			)
		if not block and flt(assignment.job_card_qty, job_card_qty_precision()) != flt(
			jc.for_quantity, job_card_qty_precision()
		):
			block = frappe._dict(
				code="JOB_CARD_QUANTITY_CHANGED",
				message=_("Job Card quantity changed after worker assignment."),
			)
		assignment.reportable_qty = material_reportable_qty(jc) if jc else 0
		assignment.active_report = active_session.name if active_session else None
		assignment.active_started_at = active_session.actual_start_time if active_session else None
		assignment.timer_paused_at = active_session.timer_paused_at if active_session else None
		assignment.active_minutes = (
			_captured_timer_minutes(
				frappe.get_doc("Job Card Work Report", active_session.name),
				include_running_until=None if active_session.timer_paused_at else now_datetime(),
			)
			if active_session
			else 0
		)
		assignment.wage_type = (
			active_session.wage_type if active_session else (rate.wage_type if rate else None)
		)
		assignment.rate = flt(
			active_session.rate if active_session else (rate.rate if rate else 0)
		)
		assignment.manual_time_entry = bool(
			active_session.manual_time_entry
			if active_session
			else (rate and rate.wage_type == "Time" and manual_entry_enabled)
		)
		assignment.time_manual_entry_available = bool(
			_time_manual_entry_available(active_session)
			if active_session
			else (
				manual_entry_enabled
				and any(option.wage_type == "Time" for option in rate_options)
			)
		)
		assignment.wage_options = [
			{"wage_type": option.wage_type, "rate": flt(option.rate)}
			for option in rate_options
		]
		assignment.can_choose_wage_type = bool(active_session and len(rate_options) > 1)
		assignment.daily_minutes_used = used_minutes
		assignment.daily_minutes_limit = daily_minutes_limit()
		assignment.has_pending_report = bool(
			frappe.db.exists(
				"Job Card Work Report",
				{"assignment": assignment.name, "status": "Pending Approval"},
			)
		)
		assignment.can_start = True
		assignment.can_finish = False
		assignment.block_code = None
		assignment.block_message = None
		if block:
			assignment.can_start = False
			assignment.block_code = block.code
			assignment.block_message = block.message
		elif active_session:
			assignment.can_start = False
			assignment.can_finish = assignment.reportable_qty > 0
			if not assignment.can_finish:
				assignment.block_code = "NO_REMAINING_QTY"
				assignment.block_message = _(
					"The Job Card quantity was filled by another report. Cancel this active session."
				)
		elif assignment.has_pending_report:
			assignment.can_start = False
			assignment.block_code = "PENDING_REPORT"
			assignment.block_message = _("A report is waiting for supervisor review.")
		elif assignment.reportable_qty <= 0:
			assignment.can_start = False
			if jc and reportable_qty(jc) > 0:
				assignment.block_code = "MATERIAL_NOT_TRANSFERRED"
				assignment.block_message = _(
					"Materials have not been issued for the remaining production quantity."
				)
			else:
				assignment.block_code = "NO_REMAINING_QTY"
				assignment.block_message = _("The Job Card quantity is fully reported.")
		elif not configured_rate_options:
			assignment.can_start = False
			assignment.block_code = "RATE_MISSING"
			assignment.block_message = _("No enabled wage rate exists for today.")
		elif (
			all(option.wage_type == "Time" for option in configured_rate_options)
			and used_minutes >= daily_minutes_limit()
		):
			assignment.can_start = False
			assignment.block_code = "DAILY_MINUTES_LIMIT"
			assignment.block_message = _("The daily wage-minute limit is already reached.")

	assignments.sort(key=_worker_assignment_priority)

	reports = frappe.get_all(
		"Job Card Work Report",
		filters={"employee": employee},
		fields=[
			"name",
			"assignment",
			"job_card",
			"operation",
			"labor_date",
			"wage_type",
			"manual_time_entry",
			"status",
			"actual_start_time",
			"actual_end_time",
			"actual_minutes",
			"completed_qty",
			"reported_minutes",
			"rate",
			"wage_amount",
			"rejection_reason",
			"submitted_at",
			"reviewed_at",
		],
		order_by="actual_start_time desc, creation desc",
		limit=100,
	)
	return {
		"employee": employee,
		"assignments": assignments,
		"reports": reports,
		"daily_minutes_used": used_minutes,
		"daily_minutes_limit": daily_minutes_limit(),
		"allow_manual_time_entry": manual_entry_enabled,
	}


def _worker_assignment_priority(assignment) -> int:
	"""Keep active and actionable work ahead of passive material waits."""
	if assignment.get("active_report"):
		return 0
	if assignment.get("can_start"):
		return 1
	if assignment.get("block_code") == "PENDING_REPORT":
		return 2
	if assignment.get("block_code") == "MATERIAL_NOT_TRANSFERRED":
		return 4
	return 3


def get_worker_report_history(
	page=1,
	page_length=DEFAULT_REVIEW_PAGE_LENGTH,
	status=None,
	operation=None,
	work_order=None,
	job_card=None,
	from_date=None,
	to_date=None,
):
	"""Return only the signed-in worker's submitted reports and review trail."""
	require_worker()
	employee = employee_for_user()
	allowed_statuses = {"Pending Approval", "Approved", "Rejected"}
	if status and status not in allowed_statuses:
		frappe.throw(_("Report history status must be Pending Approval, Approved, or Rejected."))

	filters = {
		"employee": employee,
		"employee_user": frappe.session.user,
		"status": status or ("in", sorted(allowed_statuses)),
	}
	if operation:
		filters["operation"] = operation
	if work_order:
		filters["work_order"] = work_order
	if job_card:
		filters["job_card"] = job_card

	start_date = getdate(from_date) if from_date else None
	end_date = getdate(to_date) if to_date else None
	if start_date and end_date and start_date > end_date:
		frappe.throw(_("The report start date cannot be later than the end date."))
	if start_date and end_date:
		filters["labor_date"] = ("between", [start_date, end_date])
	elif start_date:
		filters["labor_date"] = (">=", start_date)
	elif end_date:
		filters["labor_date"] = ("<=", end_date)

	rows, pagination = _review_page(
		"Job Card Work Report",
		filters=filters,
		fields=_review_report_fields(),
		order_by="labor_date desc, submitted_at desc, creation desc",
		page=page,
		page_length=page_length,
	)
	return {"rows": rows, "pagination": pagination}


def _lock_worker_assignment(assignment: str):
	require_worker()
	employee = employee_for_user()
	initial = frappe.db.get_value(
		"Job Card Worker Assignment",
		assignment,
		["job_card", "employee"],
		as_dict=True,
	)
	if not initial:
		frappe.throw(_("Worker assignment does not exist."))
	jc = job_card_values(initial.job_card, for_update=True)
	if not jc:
		frappe.throw(_("Job Card no longer exists."))
	if jc.work_order:
		frappe.db.get_value("Work Order", jc.work_order, "status", for_update=True)
	current_employee = frappe.db.get_value(
		"Employee",
		employee,
		["status", "user_id"],
		as_dict=True,
		for_update=True,
	)
	if (
		not current_employee
		or current_employee.status != "Active"
		or current_employee.user_id != frappe.session.user
	):
		frappe.throw(_("The worker's active Employee-to-User mapping changed; reporting is blocked."))
	if not frappe.db.get_value("User", frappe.session.user, "enabled", for_update=True):
		frappe.throw(_("The worker User is disabled."))
	assert_worker_user_isolated(frappe.session.user, for_update=True)
	task = frappe.db.get_value(
		"Job Card Worker Assignment",
		assignment,
		[
			"name",
			"job_card",
			"work_order",
			"company",
			"operation",
			"operation_id",
			"job_card_qty",
			"employee",
			"employee_user",
			"supervisor",
			"status",
		],
		as_dict=True,
		for_update=True,
	)
	if not task or task.job_card != jc.name or task.employee != employee:
		frappe.throw(_("You can only use your own active worker assignment."), frappe.PermissionError)
	precision = job_card_qty_precision()
	for fieldname in ("work_order", "company", "operation", "operation_id"):
		if task.get(fieldname) != jc.get(fieldname):
			frappe.throw(_("Job Card identity changed after worker assignment; reporting is blocked."))
	if flt(task.job_card_qty, precision) != flt(jc.for_quantity, precision):
		frappe.throw(_("Job Card quantity changed after worker assignment; reporting is blocked."))
	if task.status != "Active":
		frappe.throw(_("This worker assignment is not active."))
	assert_supported_job_card(jc, for_update=True)
	_assert_report_facts_match_job_card(jc, for_update=True)
	return employee, jc, task


def _require_request_id(request_id: str | None, action: str) -> str:
	request_id = str(request_id or "").strip()
	if not request_id:
		frappe.throw(_("A {0} request id is required. Refresh and try again.").format(action))
	return request_id


def start_work_session(
	assignment: str,
	request_id: str | None = None,
	*,
	started_at=None,
):
	request_id = _require_request_id(request_id, _("start"))
	require_worker()
	request_employee = employee_for_user()
	request_key = _hash_key(request_employee, frappe.session.user, "start", request_id)
	# A network retry must still resolve after review has completed the assignment
	# and submitted the Job Card. The immutable request key and worker identity make
	# this early return safe; new work still follows the full locking path below.
	existing = frappe.db.get_value(
		"Job Card Work Report",
		{"request_key": request_key},
		["name", "assignment", "employee", "employee_user"],
		as_dict=True,
	)
	if existing:
		if (
			existing.assignment != assignment
			or existing.employee != request_employee
			or existing.employee_user != frappe.session.user
		):
			frappe.throw(_("This start request id was already used with different report values."))
		return frappe.get_doc("Job Card Work Report", existing.name)
	employee, jc, task = _lock_worker_assignment(assignment)
	existing = frappe.db.get_value(
		"Job Card Work Report",
		{"request_key": request_key},
		["name", "assignment"],
		as_dict=True,
		for_update=True,
	)
	if existing:
		if existing.assignment != assignment:
			frappe.throw(_("This start request id was already used with different report values."))
		return frappe.get_doc("Job Card Work Report", existing.name, for_update=True)
	if frappe.db.get_value(
		"Job Card Work Report",
		{"assignment": assignment, "status": "Pending Approval"},
		"name",
		for_update=True,
	):
		frappe.throw(_("This assignment already has a report waiting for review."))
	active = frappe.db.get_value(
		"Job Card Work Report",
		{"employee": employee, "status": "In Progress"},
		["name", "assignment"],
		as_dict=True,
		for_update=True,
	)
	if active:
		frappe.throw(_("You already have an active work session {0}.").format(active.name))
	if material_reportable_qty(jc, for_update=True) <= 0:
		if reportable_qty(jc, for_update=True) > 0:
			frappe.throw(_("Materials have not been issued for the remaining production quantity."))
		frappe.throw(_("The Job Card has no remaining reportable quantity."))
	started_at = get_datetime(started_at or now_datetime())
	labor_date = getdate(started_at)
	if _month_is_confirmed(task.company, employee, labor_date, for_update=True):
		frappe.throw(_("This employee's monthly wage summary is already confirmed."))
	rate_options = get_wage_rates(
		task.company,
		task.operation,
		labor_date,
		for_update=True,
	)
	if not rate_options:
		frappe.throw(_("No enabled wage rate exists for this operation today."))
	if all(option.wage_type == "Time" for option in rate_options) and _daily_used_minutes(
		employee, labor_date, for_update=True
	) >= daily_minutes_limit():
		frappe.throw(_("The daily wage-minute limit is already reached."))
	rate = rate_options[0]
	piecework_rate = next(
		(flt(option.rate) for option in rate_options if option.wage_type == "Piecework"),
		0,
	)
	hourly_rate = next(
		(flt(option.rate) for option in rate_options if option.wage_type == "Time"),
		0,
	)
	time_manual_entry_snapshot = cint(hourly_rate > 0 and manual_time_entry_enabled())
	doc = frappe.get_doc(
		{
			"doctype": "Job Card Work Report",
			"request_key": request_key,
			"assignment": task.name,
			"job_card": task.job_card,
			"work_order": task.work_order,
			"company": task.company,
			"operation": task.operation,
			"operation_id": task.operation_id,
			"employee": task.employee,
			"employee_user": frappe.session.user,
			"labor_date": labor_date,
			"wage_type": rate.wage_type,
			"manual_time_entry": cint(
				rate.wage_type == "Time" and time_manual_entry_snapshot
			),
			"piecework_rate_snapshot": piecework_rate,
			"hourly_rate_snapshot": hourly_rate,
			"time_manual_entry_snapshot": time_manual_entry_snapshot,
			"status": "In Progress",
			"actual_start_time": started_at,
			"completed_qty": 0,
			"actual_minutes": 0,
			"reported_minutes": 0,
			"wage_rate": rate.name,
			"wage_rate_revision": int(rate.revision or 0),
			"rate": flt(rate.rate),
			"wage_amount": 0,
		}
	)
	_append_time_segment(doc, started_at, request_key)
	doc.flags.worker_reporting_action = True
	doc.insert(ignore_permissions=True)
	return doc


def pause_work_session(
	report: str,
	request_id: str | None = None,
	*,
	paused_at=None,
):
	request_id = _require_request_id(request_id, _("pause"))
	require_worker()
	employee = employee_for_user()
	request_key = _hash_key(employee, frappe.session.user, "pause", request_id)
	initial = frappe.db.get_value(
		"Job Card Work Report",
		report,
		["name", "assignment", "employee", "employee_user"],
		as_dict=True,
	)
	if not initial:
		frappe.throw(_("Work session does not exist."))
	if initial.employee != employee or initial.employee_user != frappe.session.user:
		frappe.throw(_("You can only pause your own active work session."), frappe.PermissionError)
	if frappe.db.exists(
		"Job Card Work Report Time Segment",
		{"parent": report, "stop_request_key": request_key},
	):
		return frappe.get_doc("Job Card Work Report", report)

	locked_employee, _job_card, task = _lock_worker_assignment(initial.assignment)
	doc = frappe.get_doc("Job Card Work Report", report, for_update=True)
	if doc.assignment != task.name or doc.employee != locked_employee:
		frappe.throw(_("You can only pause your own active work session."), frappe.PermissionError)
	if frappe.db.exists(
		"Job Card Work Report Time Segment",
		{"parent": report, "stop_request_key": request_key},
	):
		return doc
	if doc.status != "In Progress":
		frappe.throw(_("Only an active work session can be paused."))
	if doc.timer_paused_at:
		frappe.throw(_("This work timer is already paused."))
	paused_at = get_datetime(paused_at or now_datetime())
	_close_time_segment(doc, paused_at, request_key)
	doc.timer_paused_at = paused_at
	doc.flags.worker_reporting_action = True
	doc.save(ignore_permissions=True)
	return doc


def resume_work_session(
	report: str,
	request_id: str | None = None,
	*,
	resumed_at=None,
):
	request_id = _require_request_id(request_id, _("resume"))
	require_worker()
	employee = employee_for_user()
	request_key = _hash_key(employee, frappe.session.user, "resume", request_id)
	initial = frappe.db.get_value(
		"Job Card Work Report",
		report,
		["name", "assignment", "employee", "employee_user"],
		as_dict=True,
	)
	if not initial:
		frappe.throw(_("Work session does not exist."))
	if initial.employee != employee or initial.employee_user != frappe.session.user:
		frappe.throw(_("You can only resume your own active work session."), frappe.PermissionError)
	if frappe.db.exists(
		"Job Card Work Report Time Segment",
		{"parent": report, "start_request_key": request_key},
	):
		return frappe.get_doc("Job Card Work Report", report)

	locked_employee, _job_card, task = _lock_worker_assignment(initial.assignment)
	doc = frappe.get_doc("Job Card Work Report", report, for_update=True)
	if doc.assignment != task.name or doc.employee != locked_employee:
		frappe.throw(_("You can only resume your own active work session."), frappe.PermissionError)
	if frappe.db.exists(
		"Job Card Work Report Time Segment",
		{"parent": report, "start_request_key": request_key},
	):
		return doc
	if doc.status != "In Progress":
		frappe.throw(_("Only an active work session can be resumed."))
	if not doc.timer_paused_at:
		frappe.throw(_("This work timer is already running."))
	resumed_at = get_datetime(resumed_at or now_datetime())
	if resumed_at < get_datetime(doc.timer_paused_at):
		frappe.throw(_("Resume time cannot be earlier than the pause time."))
	_append_time_segment(doc, resumed_at, request_key)
	doc.timer_paused_at = None
	doc.flags.worker_reporting_action = True
	doc.save(ignore_permissions=True)
	return doc


def finish_work_session(
	report: str,
	completed_qty,
	request_id: str | None = None,
	reported_minutes=None,
	wage_type: str | None = None,
	*,
	ended_at=None,
):
	request_id = _require_request_id(request_id, _("finish"))
	require_worker()
	request_employee = employee_for_user()
	precision = job_card_qty_precision()
	qty = flt(completed_qty, precision)
	requested_minutes = flt(reported_minutes, 6)
	requested_wage_type = str(wage_type or "").strip()
	completion_key = _hash_key(request_employee, frappe.session.user, "finish", request_id)
	initial = frappe.db.get_value(
		"Job Card Work Report",
		report,
		[
			"name",
			"assignment",
			"employee",
			"employee_user",
			"wage_type",
			"manual_time_entry",
			"completion_request_key",
			"completed_qty",
			"reported_minutes",
		],
		as_dict=True,
	)
	if not initial:
		frappe.throw(_("Work session does not exist."))
	if initial.employee != request_employee or initial.employee_user != frappe.session.user:
		frappe.throw(_("You can only finish your own active work session."), frappe.PermissionError)
	if initial.completion_request_key:
		if (
			initial.completion_request_key != completion_key
			or flt(initial.completed_qty, precision) != qty
			or (requested_wage_type and initial.wage_type != requested_wage_type)
			or (
				initial.wage_type == "Time"
				and cint(initial.manual_time_entry)
				and flt(initial.reported_minutes, 6) != requested_minutes
			)
		):
			frappe.throw(_("This finish request id was already used with different report values."))
		return frappe.get_doc("Job Card Work Report", initial.name)
	employee, jc, task = _lock_worker_assignment(initial.assignment)
	doc = frappe.get_doc("Job Card Work Report", report, for_update=True)
	if doc.assignment != task.name or doc.employee != employee or doc.employee_user != frappe.session.user:
		frappe.throw(_("You can only finish your own active work session."), frappe.PermissionError)
	if doc.completion_request_key:
		if (
			doc.completion_request_key != completion_key
			or flt(doc.completed_qty, precision) != qty
			or (requested_wage_type and doc.wage_type != requested_wage_type)
			or (
				doc.wage_type == "Time"
				and cint(doc.manual_time_entry)
				and flt(doc.reported_minutes, 6) != requested_minutes
			)
		):
			frappe.throw(_("This finish request id was already used with different report values."))
		return doc
	if doc.status != "In Progress":
		frappe.throw(_("Only an active work session can be finished."))
	selected_rate = _select_work_report_wage_option(doc, requested_wage_type)
	if _month_is_confirmed(task.company, employee, doc.labor_date, for_update=True):
		frappe.throw(_("This employee's monthly wage summary is already confirmed."))
	if qty <= 0:
		frappe.throw(_("Completed quantity must be greater than zero."))
	remaining = material_reportable_qty(jc, for_update=True)
	if qty > remaining:
		frappe.throw(_("This Job Card currently allows at most {0}.").format(remaining))
	ended_at = get_datetime(ended_at or now_datetime())
	started_at = get_datetime(doc.actual_start_time)
	if ended_at <= started_at:
		frappe.throw(_("Actual end time must be later than actual start time."))
	if flt(time_diff_in_hours(ended_at, started_at) * 60, 6) > MAX_WORK_SESSION_MINUTES:
		frappe.throw(
			_(
				"A work session cannot span more than 24 hours. Cancel this active session and report again."
			)
		)
	if doc.timer_paused_at:
		if ended_at < get_datetime(doc.timer_paused_at):
			frappe.throw(_("Finish time cannot be earlier than the pause time."))
	else:
		_close_time_segment(doc, ended_at, completion_key)
	segments = _time_segments(doc)
	actual_end_time = get_datetime(segments[-1].ended_at)
	actual_minutes = _captured_timer_minutes(doc)
	if actual_minutes <= 0:
		frappe.throw(_("Actual production minutes must be greater than zero."))
	if actual_minutes > MAX_WORK_SESSION_MINUTES:
		frappe.throw(
			_(
				"A single work session cannot exceed 24 hours. Cancel this active session and report again."
			)
		)
	doc.wage_type = selected_rate.wage_type
	doc.rate = flt(selected_rate.rate)
	doc.manual_time_entry = cint(
		doc.wage_type == "Time" and _time_manual_entry_available(doc)
	)
	minutes = 0
	if doc.wage_type == "Time":
		minutes = requested_minutes if cint(doc.manual_time_entry) else actual_minutes
		if minutes <= 0:
			frappe.throw(_("Enter wage minutes greater than zero."))
		used = _daily_used_minutes(employee, doc.labor_date, for_update=True)
		if flt(used + minutes, 6) > flt(daily_minutes_limit(), 6):
			frappe.throw(
				_("Daily wage minutes would be {0}, exceeding the limit of {1}.").format(
					flt(used + minutes, 2),
					daily_minutes_limit(),
				)
			)
	audit = request_audit()
	doc.completion_request_key = completion_key
	doc.actual_end_time = actual_end_time
	doc.actual_minutes = actual_minutes
	doc.completed_qty = qty
	doc.reported_minutes = minutes
	doc.timer_paused_at = None
	doc.wage_amount = money(
		qty * flt(doc.rate)
		if doc.wage_type == "Piecework"
		else minutes / 60 * flt(doc.rate)
	)
	doc.submitted_by = frappe.session.user
	doc.submitted_at = ended_at
	doc.submission_ip = audit.ip
	doc.submission_user_agent = audit.user_agent
	doc.status = "Pending Approval"
	doc.flags.worker_reporting_action = True
	doc.flags.worker_reporting_wage_selection = True
	doc.save(ignore_permissions=True)
	return doc


def cancel_work_session(report: str):
	initial = frappe.db.get_value(
		"Job Card Work Report",
		report,
		["name", "assignment", "job_card", "work_order", "employee", "employee_user"],
		as_dict=True,
	)
	if not initial:
		frappe.throw(_("Work session does not exist."))
	jc = job_card_values(initial.job_card, for_update=True)
	if initial.work_order:
		frappe.db.get_value("Work Order", initial.work_order, "name", for_update=True)
	frappe.db.get_value("Employee", initial.employee, "name", for_update=True)
	task = frappe.db.get_value(
		"Job Card Worker Assignment",
		initial.assignment,
		[
			"name",
			"job_card",
			"work_order",
			"company",
			"employee",
			"employee_user",
			"supervisor",
			"status",
		],
		as_dict=True,
		for_update=True,
	)
	doc = frappe.get_doc("Job Card Work Report", report, for_update=True)
	if (
		not task
		or doc.assignment != task.name
		or doc.job_card != task.job_card
		or doc.work_order != task.work_order
		or doc.employee != task.employee
		or doc.employee_user != task.employee_user
		or (jc and jc.name != task.job_card)
	):
		frappe.throw(_("Work session no longer matches its worker assignment."))
	if doc.employee_user == frappe.session.user:
		require_worker()
	else:
		require_reviewer(for_update=True)
		if not is_admin_reviewer(for_update=True) and task.supervisor != frappe.session.user:
			frappe.throw(_("You can only cancel active sessions assigned to you."), frappe.PermissionError)
		_assert_supervisor_company(frappe.session.user, task.company)
	if doc.status != "In Progress":
		frappe.throw(_("Only an active work session can be cancelled."))
	doc.flags.worker_reporting_action = True
	doc.delete(ignore_permissions=True)
	return {"ok": True}


def _report_filter_for_reviewer() -> dict:
	filters = {}
	if not is_admin_reviewer():
		assignments = frappe.get_all(
			"Job Card Worker Assignment",
			filters={"supervisor": frappe.session.user},
			pluck="name",
			limit=0,
		)
		filters["assignment"] = ("in", assignments or [""])
	return filters


def _review_pagination(page=1, page_length=DEFAULT_REVIEW_PAGE_LENGTH, total_count=0):
	page = max(cint(page) or 1, 1)
	page_length = min(max(cint(page_length) or DEFAULT_REVIEW_PAGE_LENGTH, 1), MAX_REVIEW_PAGE_LENGTH)
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


def _review_page(doctype, *, filters, fields, order_by, page=1, page_length=DEFAULT_REVIEW_PAGE_LENGTH):
	page_meta = _review_pagination(
		page=page,
		page_length=page_length,
		total_count=frappe.db.count(doctype, filters=filters),
	)
	rows = frappe.get_all(
		doctype,
		filters=filters,
		fields=fields,
		order_by=order_by,
		offset=(page_meta["page"] - 1) * page_meta["page_length"],
		limit=page_meta["page_length"],
	)
	return rows, page_meta


def _employee_identity(rows):
	employees = sorted({row.employee for row in rows if row.get("employee")})
	if not employees:
		return {}
	return {
		row.name: row
		for row in frappe.get_all(
			"Employee",
			filters={"name": ("in", employees)},
			fields=["name", "employee_name", "user_id"],
			limit=0,
		)
	}


def _set_employee_names(rows, employee_identity=None):
	employee_identity = employee_identity or _employee_identity(rows)
	for row in rows:
		identity = employee_identity.get(row.employee) if row.get("employee") else None
		row.employee_name = identity.employee_name if identity else row.get("employee")
	return rows


def _review_report_fields():
	return [
		"name",
		"assignment",
		"job_card",
		"work_order",
		"company",
		"operation",
		"employee",
		"employee_user",
		"labor_date",
		"wage_type",
		"manual_time_entry",
		"status",
		"completed_qty",
		"actual_start_time",
		"actual_end_time",
		"actual_minutes",
		"reported_minutes",
		"rate",
		"wage_amount",
		"submitted_by",
		"submitted_at",
		"reviewed_by",
		"reviewed_at",
		"rejection_reason",
		"job_card_time_log",
		"monthly_summary",
		"modified",
	]


def get_review_dashboard(
	pending_page=1,
	assignment_page=1,
	processed_page=1,
	page_length=DEFAULT_REVIEW_PAGE_LENGTH,
):
	require_reviewer()
	base_filters = _report_filter_for_reviewer()
	pending_filters = {**base_filters, "status": "Pending Approval"}
	reports, pending_pagination = _review_page(
		"Job Card Work Report",
		filters=pending_filters,
		fields=_review_report_fields(),
		order_by="submitted_at asc, creation asc",
		page=pending_page,
		page_length=page_length,
	)
	employee_identity = _employee_identity(reports)
	admin_reviewer = is_admin_reviewer()
	for report in reports:
		jc = job_card_values(report.job_card)
		all_pending = pending_report_qty(report.job_card)
		precision = job_card_qty_precision()
		report.for_quantity = flt(jc.for_quantity) if jc else 0
		report.job_card_completed_qty = flt(jc.total_completed_qty) if jc else 0
		report.pending_report_qty = all_pending
		snapshot = frappe.db.get_value(
			"Job Card Worker Assignment",
			report.assignment,
			[
				"job_card",
				"work_order",
				"company",
				"operation",
				"operation_id",
				"job_card_qty",
				"employee",
				"supervisor",
				"status",
			],
			as_dict=True,
		)
		review_block = None
		if (
			not snapshot
			or snapshot.job_card != report.job_card
			or snapshot.employee != report.employee
		):
			review_block = frappe._dict(
				code="ASSIGNMENT_MISMATCH",
				message=_("Work report no longer matches its worker assignment."),
			)
		elif snapshot.status != "Active":
			review_block = frappe._dict(
				code="ASSIGNMENT_INACTIVE",
				message=_("Worker assignment is no longer active."),
			)
		elif not admin_reviewer and snapshot.supervisor != frappe.session.user:
			review_block = frappe._dict(
				code="REVIEW_SCOPE",
				message=_("You can only review reports assigned to you."),
			)
		current_worker = employee_identity.get(report.employee)
		current_worker_user = current_worker.user_id if current_worker else None
		if not review_block and frappe.session.user in {report.employee_user, current_worker_user}:
			review_block = frappe._dict(
				code="SELF_REVIEW",
				message=_("A worker cannot approve or reject their own production report."),
			)

		block = review_block or job_card_block(jc)
		if not block and flt(
			flt(jc.total_completed_qty, precision)
			+ flt(jc.process_loss_qty, precision)
			+ all_pending,
			precision,
		) > flt(
			jc.for_quantity, precision
		):
			block = frappe._dict(
				code="QUANTITY_CONFLICT",
				message=_("Approved production plus pending work reports exceeds the Job Card quantity."),
			)
		material_capacity = work_order_material_capacity(jc) if jc else 0
		if (
			not block
			and material_capacity is not None
			and flt(
				flt(jc.total_completed_qty, precision)
				+ flt(jc.process_loss_qty, precision)
				+ all_pending,
				precision,
			)
			> flt(material_capacity, precision)
		):
			block = frappe._dict(
				code="MATERIAL_NOT_TRANSFERRED",
				message=_(
					"Issued materials do not cover the pending production quantity; reject or wait for material transfer."
				),
			)
		if not block and (
			any(
				snapshot.get(fieldname) != jc.get(fieldname)
				for fieldname in ("work_order", "company", "operation", "operation_id")
			)
			or flt(snapshot.job_card_qty, precision) != flt(jc.for_quantity, precision)
		):
			block = frappe._dict(
				code="ASSIGNMENT_SNAPSHOT_CHANGED",
				message=_("Job Card identity or quantity changed after worker assignment."),
			)
		report.can_approve = not block
		report.approve_block_code = block.code if block else None
		report.approve_block_message = block.message if block else None
		report.can_reject = not review_block
		report.reject_block_code = review_block.code if review_block else None
		report.reject_block_message = review_block.message if review_block else None
		report.capacity_conflict = bool(block)

	assignments, assignment_pagination = _review_page(
		"Job Card Worker Assignment",
		filters={
			"status": "Active",
			**({"supervisor": frappe.session.user} if not admin_reviewer else {}),
		},
		fields=["name", "job_card", "work_order", "operation", "employee", "supervisor", "notes", "assigned_at"],
		order_by="assigned_at desc",
		page=assignment_page,
		page_length=page_length,
	)
	for assignment in assignments:
		active_session = frappe.db.get_value(
			"Job Card Work Report",
			{"assignment": assignment.name, "status": "In Progress"},
			["name", "actual_start_time"],
			as_dict=True,
		)
		assignment.active_report = active_session.name if active_session else None
		assignment.active_started_at = active_session.actual_start_time if active_session else None
		assignment.can_cancel_session = bool(active_session)
		assignment.can_unassign = not frappe.db.exists(
			"Job Card Work Report", {"assignment": assignment.name}
		)
		assignment.unassign_block_message = (
			None
			if assignment.can_unassign
			else _("This assignment has report history and cannot be removed.")
		)
	processed_filters = {
		**base_filters,
		"status": ("in", ["Approved", "Rejected"]),
		"reviewed_at": ("between", [f"{nowdate()} 00:00:00", f"{nowdate()} 23:59:59"]),
	}
	processed, processed_pagination = _review_page(
		"Job Card Work Report",
		filters=processed_filters,
		fields=_review_report_fields(),
		order_by="reviewed_at desc",
		page=processed_page,
		page_length=page_length,
	)
	all_rows = [*reports, *assignments, *processed]
	employee_identity.update(_employee_identity(all_rows))
	_set_employee_names(reports, employee_identity)
	_set_employee_names(assignments, employee_identity)
	_set_employee_names(processed, employee_identity)
	wage_scope = (
		wage_manager_companies(throw_if_empty=False)
		if user_roles().intersection(WAGE_ROLES)
		else set()
	)
	return {
		"reports": reports,
		"assignments": assignments,
		"processed_today": processed,
		"pagination": {
			"reports": pending_pagination,
			"assignments": assignment_pagination,
			"processed_today": processed_pagination,
		},
		"can_manage_wages": wage_scope is None or bool(wage_scope),
		"companies": (
			frappe.get_all("Company", pluck="name", order_by="name", limit=0)
			if wage_scope is None
			else sorted(wage_scope)
		),
	}


def get_review_history(
	page=1,
	page_length=DEFAULT_REVIEW_PAGE_LENGTH,
	status=None,
	employee=None,
	work_order=None,
	job_card=None,
	from_date=None,
	to_date=None,
):
	"""Return reviewer-scoped decisions with bounded server-side pagination."""
	require_reviewer()
	allowed_statuses = {"Approved", "Rejected"}
	if status and status not in allowed_statuses:
		frappe.throw(_("Historical review status must be Approved or Rejected."))
	filters = {
		**_report_filter_for_reviewer(),
		"status": status or ("in", sorted(allowed_statuses)),
	}
	if employee:
		filters["employee"] = employee
	if work_order:
		filters["work_order"] = work_order
	if job_card:
		filters["job_card"] = job_card

	start_date = getdate(from_date) if from_date else None
	end_date = getdate(to_date) if to_date else None
	if start_date and end_date and start_date > end_date:
		frappe.throw(_("The review start date cannot be later than the end date."))
	if start_date and end_date:
		filters["reviewed_at"] = (
			"between",
			[f"{start_date} 00:00:00", f"{end_date} 23:59:59.999999"],
		)
	elif start_date:
		filters["reviewed_at"] = (">=", f"{start_date} 00:00:00")
	elif end_date:
		filters["reviewed_at"] = ("<=", f"{end_date} 23:59:59.999999")

	rows, pagination = _review_page(
		"Job Card Work Report",
		filters=filters,
		fields=_review_report_fields(),
		order_by="reviewed_at desc, name desc",
		page=page,
		page_length=page_length,
	)
	_set_employee_names(rows)
	return {"rows": rows, "pagination": pagination}


def _lock_report_for_review(
	name: str,
	allowed_statuses: set[str],
	*,
	require_job_card_consistency: bool = True,
):
	require_reviewer()
	initial = frappe.db.get_value(
		"Job Card Work Report",
		name,
		["name", "job_card", "employee", "assignment"],
		as_dict=True,
	)
	if not initial:
		frappe.throw(_("Work report does not exist."))
	jc = job_card_values(initial.job_card, for_update=True)
	if jc and jc.work_order:
		frappe.db.get_value("Work Order", jc.work_order, "status", for_update=True)
	current_employee = frappe.db.get_value(
		"Employee",
		initial.employee,
		["status", "user_id"],
		as_dict=True,
		for_update=True,
	)
	require_reviewer(for_update=True)
	assignment = frappe.db.get_value(
		"Job Card Worker Assignment",
		initial.assignment,
		[
			"name",
			"job_card",
			"work_order",
			"company",
			"operation",
			"operation_id",
			"job_card_qty",
			"employee",
			"supervisor",
			"status",
		],
		as_dict=True,
		for_update=True,
	)
	doc = frappe.get_doc("Job Card Work Report", name, for_update=True)
	if not assignment or assignment.job_card != doc.job_card or assignment.employee != doc.employee:
		frappe.throw(_("Work report no longer matches its worker assignment."))
	if not is_admin_reviewer(for_update=True) and assignment.supervisor != frappe.session.user:
		frappe.throw(_("You can only review reports assigned to you."), frappe.PermissionError)
	if doc.status not in allowed_statuses:
		frappe.throw(_("This action is not allowed while the report is {0}.").format(doc.status))
	if doc.employee_user == frappe.session.user or (
		current_employee and current_employee.user_id == frappe.session.user
	):
		frappe.throw(_("A worker cannot approve or reject their own production report."), frappe.PermissionError)
	if doc.status == "Pending Approval" and assignment.status != "Active":
		frappe.throw(_("Worker assignment is no longer active."))
	if doc.status != "Pending Approval" and assignment.status not in {"Active", "Completed"}:
		frappe.throw(_("Worker assignment is no longer valid for this report."))
	if require_job_card_consistency:
		if not jc:
			frappe.throw(_("Job Card no longer exists; approval is blocked."))
		for fieldname in ("work_order", "company", "operation", "operation_id"):
			if assignment.get(fieldname) != jc.get(fieldname):
				frappe.throw(_("Job Card identity changed after worker assignment; approval is blocked."))
		if flt(assignment.job_card_qty, job_card_qty_precision()) != flt(
			jc.for_quantity, job_card_qty_precision()
		):
			frappe.throw(_("Job Card quantity changed after worker assignment; approval is blocked."))
	return jc, assignment, doc


def _assert_approved_report_integrity(jc, doc):
	rows = frappe.get_all(
		"Job Card Time Log",
		filters={"parent": doc.job_card, "custom_job_card_work_report_segment": doc.name},
		fields=[
			"name",
			"employee",
			"completed_qty",
			"from_time",
			"to_time",
			"time_in_mins",
			"custom_job_card_work_report",
			"custom_reported_employee",
			"idx",
		],
		order_by="idx asc",
		limit=0,
	)
	if not rows:
		legacy = frappe.db.get_value(
			"Job Card Time Log",
			{"parent": doc.job_card, "custom_job_card_work_report": doc.name},
			[
				"name", "employee", "completed_qty", "from_time", "to_time",
				"time_in_mins", "custom_job_card_work_report", "custom_reported_employee",
			],
			as_dict=True,
			for_update=True,
		)
		rows = [legacy] if legacy else []
	quantity_rows = [row for row in rows if flt(row.completed_qty)]
	if not rows or len(quantity_rows) != 1 or any(
		row.employee != doc.employee or row.custom_reported_employee != doc.employee for row in rows
	):
		frappe.throw(_("Approved work report is inconsistent with its Job Card quantity row."))
	quantity_row = quantity_rows[0]
	if (
		quantity_row.name != doc.job_card_time_log
		or flt(sum(flt(row.completed_qty) for row in rows), 6) != flt(doc.completed_qty, 6)
		or quantity_row.custom_job_card_work_report != doc.name
	):
		frappe.throw(_("Approved work report is inconsistent with its Job Card quantity row."))
	segments = _time_segments(doc)
	if not segments and (doc.actual_start_time or doc.actual_end_time):
		segments = [
			frappe._dict(
				started_at=doc.actual_start_time,
				ended_at=doc.actual_end_time,
				duration_minutes=doc.actual_minutes,
			)
		]
	if segments:
		if len(rows) != len(segments):
			frappe.throw(_("Approved work report is inconsistent with its Job Card time segments."))
		for row, segment in zip(rows, segments):
			if (
				get_datetime(row.from_time) != get_datetime(segment.started_at)
				or get_datetime(row.to_time) != get_datetime(segment.ended_at)
				or flt(row.time_in_mins, 6) != flt(segment.duration_minutes, 6)
			):
				frappe.throw(_("Approved work report is inconsistent with its Job Card time segments."))
	elif any(row.from_time or row.to_time or flt(row.time_in_mins) for row in rows):
		frappe.throw(_("Legacy untimed work report acquired Job Card time."))
	_assert_report_facts_match_job_card(jc, for_update=True)


def approve_work_report(name: str):
	jc_values, assignment, doc = _lock_report_for_review(name, {"Pending Approval", "Approved"})
	if doc.status == "Approved":
		_assert_approved_report_integrity(jc_values, doc)
		return doc
	reviewer = frappe.session.user
	audit = request_audit()
	assert_supported_job_card(jc_values, for_update=True)
	_assert_report_facts_match_job_card(jc_values, for_update=True)
	precision = job_card_qty_precision()
	all_pending = pending_report_qty(doc.job_card, for_update=True)
	if flt(
		flt(jc_values.total_completed_qty, precision)
		+ flt(jc_values.process_loss_qty, precision)
		+ all_pending,
		precision,
	) > flt(
		jc_values.for_quantity, precision
	):
		frappe.throw(_("Pending reports no longer fit the Job Card quantity. Reject the incorrect report."))
	material_capacity = work_order_material_capacity(jc_values, for_update=True)
	if material_capacity is not None and flt(
		flt(jc_values.total_completed_qty, precision)
		+ flt(jc_values.process_loss_qty, precision)
		+ all_pending,
		precision,
	) > flt(material_capacity, precision):
		frappe.throw(
			_(
				"Issued materials currently cover at most {0}; reject or wait for material transfer."
			).format(flt(material_capacity, precision))
		)

	job_card = frappe.get_doc("Job Card", doc.job_card, for_update=True)
	segments = _time_segments(doc)
	if not segments:
		segments = [
			frappe._dict(
				started_at=doc.actual_start_time,
				ended_at=doc.actual_end_time,
				duration_minutes=doc.actual_minutes,
			)
		]
	quantity_row = None
	for index, segment in enumerate(segments):
		is_quantity_row = index == len(segments) - 1
		row = job_card.append(
			"time_logs",
			{
				"employee": doc.employee,
				"completed_qty": flt(doc.completed_qty) if is_quantity_row else 0,
				"from_time": segment.started_at,
				"to_time": segment.ended_at,
				"custom_job_card_work_report": doc.name if is_quantity_row else None,
				"custom_job_card_work_report_segment": doc.name,
				"custom_reported_employee": doc.employee,
			},
		)
		if is_quantity_row:
			quantity_row = row
	job_card.pending_qty = max(
		flt(job_card.for_quantity, precision)
		- flt(job_card.total_completed_qty, precision)
		- flt(job_card.process_loss_qty, precision)
		- flt(doc.completed_qty, precision),
		0,
	)
	job_card.flags.worker_reporting_approval = doc.name
	job_card.save(ignore_permissions=True)
	if flt(job_card.total_completed_qty, precision) != flt(
		flt(jc_values.total_completed_qty, precision) + flt(doc.completed_qty, precision),
		precision,
	):
		frappe.throw(_("Job Card did not accept the approved quantity exactly; approval was rolled back."))
	if flt(
		flt(job_card.total_completed_qty, precision) + flt(job_card.process_loss_qty, precision),
		precision,
	) == flt(job_card.for_quantity, precision):
		# ERPNext's Job Card submit updates the parent Work Order through a fresh
		# document, which does not inherit this service's ignore_permissions flag.
		# Keep the real web session intact and expose only the exact parent Work
		# Order name to the request-local Work Order mixin for this native submit.
		with _allow_work_order_update_for_approval(job_card.work_order):
			job_card.flags.ignore_permissions = True
			job_card.submit()

	doc.status = "Approved"
	doc.reviewed_by = reviewer
	doc.reviewed_at = now_datetime()
	doc.review_ip = audit.ip
	doc.review_user_agent = audit.user_agent
	doc.job_card_time_log = quantity_row.name if quantity_row else None
	if not doc.job_card_time_log:
		frappe.throw(_("Approved Job Card quantity row could not be linked back to the report."))
	doc.rejection_reason = None
	doc.flags.worker_reporting_action = True
	doc.save(ignore_permissions=True)
	return doc


def reject_work_report(name: str, reason: str):
	reason = str(reason or "").strip()
	if not reason:
		frappe.throw(_("A rejection reason is required."))
	_, _, doc = _lock_report_for_review(
		name,
		{"Pending Approval", "Rejected"},
		require_job_card_consistency=False,
	)
	if doc.status == "Rejected":
		return doc
	audit = request_audit()
	doc.status = "Rejected"
	doc.rejection_reason = reason
	doc.reviewed_by = frappe.session.user
	doc.reviewed_at = now_datetime()
	doc.review_ip = audit.ip
	doc.review_user_agent = audit.user_agent
	doc.flags.worker_reporting_action = True
	doc.save(ignore_permissions=True)
	return doc


def get_work_order_assignment_context(work_order: str):
	"""Return operation-level assignment facts for one production Work Order.

	The production workbench uses this read-only snapshot to keep dispatch in the
	current planning context. Assignment writes still go through ``assign_worker``
	and therefore retain the same locks, role checks, and Job Card invariants.
	"""
	require_reviewer()
	work_order_row = frappe.db.get_value(
		"Work Order",
		work_order,
		[
			"name",
			"company",
			"production_item",
			"qty",
			"produced_qty",
			"status",
			"docstatus",
			"skip_transfer",
			"material_transferred_for_manufacturing",
		],
		as_dict=True,
	)
	if not work_order_row:
		frappe.throw(_("Work Order does not exist."))
	_assert_supervisor_company(frappe.session.user, work_order_row.company)

	job_card_rows = frappe.get_all(
		"Job Card",
		filters={"work_order": work_order},
		fields=["name", "operation", "workstation", "sequence_id", "creation"],
		order_by="sequence_id asc, creation asc",
		limit=0,
	)
	all_assignments = frappe.get_all(
		"Job Card Worker Assignment",
		filters={"work_order": work_order},
		fields=["name", "job_card", "employee", "supervisor", "status", "assigned_at"],
		order_by="assigned_at asc, creation asc",
		limit=0,
	)
	assignment_names = [row.name for row in all_assignments]
	latest_reports = (
		frappe.get_all(
			"Job Card Work Report",
			filters={
				"assignment": ["in", assignment_names],
			},
			fields=["assignment", "status", "modified"],
			order_by="modified desc",
			limit=0,
		)
		if assignment_names
		else []
	)
	report_status_by_assignment = {}
	for report in latest_reports:
		report_status_by_assignment.setdefault(report.assignment, report.status)

	employee_names = {
		row.name: row.employee_name
		for row in frappe.get_all(
			"Employee",
			filters={"name": ["in", sorted({row.employee for row in all_assignments})]},
			fields=["name", "employee_name"],
			limit=0,
		)
	} if all_assignments else {}
	admin_reviewer = is_admin_reviewer()

	job_cards = []
	for job_card_row in job_card_rows:
		job_card = job_card_values(job_card_row.name)
		block = job_card_block(job_card)
		remaining_qty = reportable_qty(job_card) if job_card else 0
		if not block and remaining_qty <= 0:
			block = frappe._dict(
				code="NO_REMAINING_QTY",
				message=_("The Job Card has no remaining reportable quantity."),
			)
		if job_card and not block:
			today = getdate(nowdate())
			wage_rate = frappe.qb.DocType("Operation Wage Rate")
			wage_rates = (
				frappe.qb.from_(wage_rate)
				.select(wage_rate.name)
				.where(
					(wage_rate.company == job_card.company)
					& (wage_rate.operation == job_card.operation)
					& (wage_rate.enabled == 1)
					& (wage_rate.valid_from <= today)
					& ((wage_rate.valid_to.isnull()) | (wage_rate.valid_to >= today))
				)
				.orderby(wage_rate.valid_from, order=Order.desc)
				.orderby(wage_rate.modified, order=Order.desc)
				.limit(2)
			).run(as_dict=True)
		else:
			wage_rates = []
		if not block and not wage_rates:
			block = frappe._dict(
				code="RATE_MISSING",
				message=_("Configure an enabled wage rate for this operation before assigning workers."),
			)
		elif not block and len(wage_rates) > 1:
			block = frappe._dict(
				code="RATE_CONFLICT",
				message=_("More than one wage rate is active for this operation and date."),
			)

		job_assignments = [row for row in all_assignments if row.job_card == job_card_row.name]
		active_job_assignments = [row for row in job_assignments if row.status == "Active"]
		visible_assignments = [
			row
			for row in job_assignments
			if admin_reviewer or row.supervisor == frappe.session.user
		]
		assignment_supervisors = sorted(
			{row.supervisor for row in active_job_assignments if row.supervisor}
		)
		visible_historical_supervisors = sorted(
			{row.supervisor for row in visible_assignments if row.supervisor}
		)
		action_supervisor = (
			assignment_supervisors[0]
			if len(assignment_supervisors) == 1
			else frappe.session.user
		)
		display_supervisor = (
			assignment_supervisors[0]
			if len(assignment_supervisors) == 1
			and (admin_reviewer or assignment_supervisors[0] == frappe.session.user)
			else visible_historical_supervisors[0]
			if len(visible_historical_supervisors) == 1
			else ""
		)
		if not block and len(assignment_supervisors) > 1:
			block = frappe._dict(
				code="SUPERVISOR_CONFLICT",
				message=_("Existing assignments use more than one reviewing supervisor."),
			)
		other_supervisor = any(
			row.supervisor != frappe.session.user for row in active_job_assignments
		)
		if not block and not admin_reviewer and other_supervisor:
			block = frappe._dict(
				code="OTHER_SUPERVISOR",
				message=_("This Job Card is already managed by another production supervisor."),
			)
		material_capacity = work_order_material_capacity(job_card) if job_card else 0
		available_reportable_qty = material_reportable_qty(job_card) if job_card else 0
		if remaining_qty <= 0:
			material_status = "COMPLETED"
			material_status_label = _("Completed")
		elif material_capacity is None or available_reportable_qty > 0:
			material_status = "READY_TO_REPORT"
			material_status_label = _("Ready to report")
		else:
			material_status = "MATERIAL_NOT_TRANSFERRED"
			material_status_label = _("Waiting for material issue")

		job_cards.append(
			{
				"name": job_card_row.name,
				"operation": job_card_row.operation,
				"workstation": job_card_row.workstation,
				"sequence_id": job_card_row.sequence_id,
				"for_quantity": flt(job_card.for_quantity) if job_card else 0,
				"completed_qty": flt(job_card.total_completed_qty) if job_card else 0,
				"remaining_qty": remaining_qty,
				"available_reportable_qty": available_reportable_qty,
				"material_status": material_status,
				"material_status_label": material_status_label,
				"can_assign": not block,
				"block_code": block.code if block else None,
				"block_message": block.message if block else None,
				"assignment_supervisor": action_supervisor,
				"display_supervisor": display_supervisor,
				"can_choose_supervisor": admin_reviewer and not assignment_supervisors,
				"assignments": [
					{
						"name": row.name,
						"employee": row.employee,
						"employee_name": employee_names.get(row.employee) or row.employee,
						"supervisor": row.supervisor,
						"assignment_status": row.status,
						"report_status": report_status_by_assignment.get(row.name),
					}
					for row in visible_assignments
				],
			}
		)

	default_job_card = next((row for row in job_cards if row["can_assign"]), None)
	return {
		"work_order": dict(work_order_row),
		"job_cards": job_cards,
		"assignment_supervisor": (
			default_job_card["assignment_supervisor"]
			if default_job_card
			else frappe.session.user
		),
		"can_choose_supervisor": admin_reviewer,
		"can_assign": any(row["can_assign"] for row in job_cards),
	}


def search_draft_job_cards(
	txt: str = "",
	start: int = 0,
	page_len: int = 20,
	work_order: str | None = None,
):
	require_reviewer()
	if frappe.db.get_single_value("Manufacturing Settings", "enforce_time_logs"):
		return []
	companies = _supervisor_companies(frappe.session.user)
	if work_order:
		work_order_company = frappe.db.get_value("Work Order", work_order, "company")
		if not work_order_company:
			return []
		_assert_supervisor_company(frappe.session.user, work_order_company)
	txt = f"%{str(txt or '').strip()}%"
	company_condition = "" if companies is None else "and jc.company in %(companies)s"
	work_order_condition = "and jc.work_order = %(work_order)s" if work_order else ""
	supervisor_condition = (
		""
		if is_admin_reviewer()
		else """
		  and not exists (
			select 1 from `tabJob Card Worker Assignment` other_assignment
			where other_assignment.job_card = jc.name
			  and other_assignment.supervisor != %(current_user)s
		  )
		"""
	)
	return frappe.db.sql(
		f"""
		select jc.name, jc.operation, jc.work_order
		from `tabJob Card` jc
		inner join `tabWork Order` work_order on work_order.name = jc.work_order
		where jc.docstatus = 0
		  and work_order.status not in ('Closed', 'Stopped', 'Completed', 'Cancelled')
		  and ifnull(jc.is_corrective_job_card, 0) = 0
		  and ifnull(jc.track_semi_finished_goods, 0) = 0
		  and ifnull(jc.is_subcontracted, 0) = 0
		  and ifnull(jc.company, '') != ''
		  and ifnull(jc.operation, '') != ''
		  and ifnull(jc.operation_id, '') != ''
		  and ifnull(jc.for_quantity, 0) > 0
		  and ifnull(jc.total_completed_qty, 0) + ifnull(jc.process_loss_qty, 0) < ifnull(jc.for_quantity, 0)
		  {company_condition}
		  {supervisor_condition}
		  {work_order_condition}
		  and (jc.name like %(txt)s or ifnull(jc.operation, '') like %(txt)s or ifnull(jc.work_order, '') like %(txt)s)
		  and 1 = (
			select count(*) from `tabOperation Wage Rate` wage_rate
			where wage_rate.company = jc.company
			  and wage_rate.operation = jc.operation
			  and wage_rate.enabled = 1
			  and wage_rate.valid_from <= %(today)s
			  and (wage_rate.valid_to is null or wage_rate.valid_to >= %(today)s)
		  )
		  and not exists (
			select 1 from `tabJob Card Operation` subop
			where subop.parent = jc.name and subop.parentfield = 'sub_operations'
		  )
			  and not exists (
				select 1 from `tabJob Card Time Log` time_log
				where time_log.parent = jc.name
				  and ifnull(time_log.custom_job_card_work_report, '') = ''
				  and ifnull(time_log.custom_job_card_work_report_segment, '') = ''
			  )
		order by jc.modified desc
		limit %(start)s, %(page_len)s
		""",
		{
			"txt": txt,
			"companies": tuple(sorted(companies or [])),
			"current_user": frappe.session.user,
			"work_order": work_order,
			"today": getdate(nowdate()),
			"start": int(start),
			"page_len": min(50, int(page_len)),
		},
	)


def search_assignment_supervisors(
	work_order: str,
	txt: str = "",
	start: int = 0,
	page_len: int = 20,
):
	"""Return reviewers who can own assignments for the Work Order company."""
	require_reviewer()
	company = frappe.db.get_value("Work Order", work_order, "company")
	if not company:
		return []
	_assert_supervisor_company(frappe.session.user, company)
	search_text = f"%{str(txt or '').strip()}%"
	if not is_admin_reviewer():
		full_name = frappe.db.get_value("User", frappe.session.user, "full_name")
		label = full_name or frappe.session.user
		if str(txt or "").strip().lower() not in f"{frappe.session.user} {label}".lower():
			return []
		return [(frappe.session.user, label)]

	return frappe.db.sql(
		"""
		select distinct user.name, coalesce(nullif(user.full_name, ''), user.name)
		from `tabUser` user
		where user.enabled = 1
		  and (
			user.name = 'Administrator'
			or exists (
				select 1 from `tabHas Role` reviewer_role
				where reviewer_role.parent = user.name
				  and reviewer_role.parenttype = 'User'
				  and reviewer_role.role in (
					'Process Simplification Owner', 'Production Supervisor',
					'Process Simplification Production Manager', 'System Manager'
				  )
			)
		  )
		  and (
			user.name = 'Administrator'
			or exists (
				select 1 from `tabHas Role` manager_role
				where manager_role.parent = user.name
				  and manager_role.parenttype = 'User'
				  and manager_role.role = 'System Manager'
			)
			or exists (
				select 1 from `tabEmployee` reviewer_employee
				where reviewer_employee.user_id = user.name
				  and reviewer_employee.status = 'Active'
				  and reviewer_employee.company = %(company)s
			)
			or exists (
				select 1 from `tabUser Permission` company_permission
				where company_permission.user = user.name
				  and company_permission.allow = 'Company'
				  and company_permission.for_value = %(company)s
				  and ifnull(company_permission.applicable_for, '') = ''
			)
		  )
		  and (user.name like %(txt)s or ifnull(user.full_name, '') like %(txt)s)
		order by case when user.name = 'Administrator' then 1 else 0 end,
			coalesce(nullif(user.full_name, ''), user.name), user.name
		limit %(start)s, %(page_len)s
		""",
		{
			"company": company,
			"txt": search_text,
			"start": int(start),
			"page_len": min(50, int(page_len)),
		},
	)


def search_workers(job_card: str, txt: str = "", start: int = 0, page_len: int = 20):
	require_reviewer()
	job_card_row = job_card_values(job_card)
	if not job_card_row:
		return []
	_assert_supervisor_company(frappe.session.user, job_card_row.company)
	values = {
		"job_card": job_card,
		"company": job_card_row.company,
		"current_user": frappe.session.user,
		"txt": f"%{str(txt or '').strip()}%",
		"start": int(start),
		"page_len": min(50, int(page_len)),
	}
	return frappe.db.sql(
		"""
		select employee.name, employee.employee_name, employee.user_id
		from `tabEmployee` employee
		inner join `tabUser` worker_user on worker_user.name = employee.user_id and worker_user.enabled = 1
		where employee.status = 'Active'
		  and employee.company = %(company)s
		  and ifnull(employee.user_id, '') != ''
		  and employee.user_id != %(current_user)s
		  and exists (
			select 1 from `tabHas Role` worker_role
			where worker_role.parent = employee.user_id and worker_role.role = 'Production Worker'
		  )
		  and not exists (
			select 1 from `tabHas Role` incompatible_role
			where incompatible_role.parent = employee.user_id
			  and incompatible_role.role in (
				'Process Simplification Owner', 'Process Simplification Sales Operator',
				'Process Simplification Warehouse Operator', 'Process Simplification Production Manager',
				'Process Simplification Access Manager', 'Production Supervisor',
				'Production Wage Manager', 'System Manager',
				'Manufacturing User', 'Manufacturing Manager', 'Shop Floor User', 'Shop Floor Manager'
			  )
		  )
		  and not exists (
			select 1 from `tabJob Card Worker Assignment` existing_assignment
			where existing_assignment.job_card = %(job_card)s
			  and existing_assignment.employee = employee.name
		  )
		  and (employee.name like %(txt)s or ifnull(employee.employee_name, '') like %(txt)s)
		order by employee.employee_name, employee.name
		limit %(start)s, %(page_len)s
		""",
		values,
	)


def search_wage_employees(company: str, txt: str = "", start: int = 0, page_len: int = 20):
	require_wage_manager(company)
	return frappe.db.sql(
		"""
		select employee.name, employee.employee_name, employee.user_id
		from `tabEmployee` employee
		where employee.company = %(company)s
		  and exists (
			select 1 from `tabJob Card Work Report` report
			where report.employee = employee.name and report.company = %(company)s
		  )
		  and (employee.name like %(txt)s or ifnull(employee.employee_name, '') like %(txt)s)
		order by employee.employee_name, employee.name
		limit %(start)s, %(page_len)s
		""",
		{
			"company": company,
			"txt": f"%{str(txt or '').strip()}%",
			"start": int(start),
			"page_len": min(50, int(page_len)),
		},
	)
