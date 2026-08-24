from __future__ import annotations

import hashlib

import frappe
from frappe import _
from frappe.utils import (
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
	approved_report_qty,
	assert_worker_user_isolated,
	assert_supported_job_card,
	daily_minutes_limit,
	employee_for_user,
	employee_user,
	get_wage_rate,
	is_admin_reviewer,
	job_card_block,
	job_card_qty_precision,
	job_card_values,
	money,
	pending_report_qty,
	reportable_qty,
	request_audit,
	require_reviewer,
	require_wage_manager,
	require_worker,
	user_roles,
	wage_manager_companies,
)


def _hash_key(*values) -> str:
	return hashlib.sha256("|".join(str(value or "") for value in values).encode()).hexdigest()


def _assert_reviewer_scope(supervisor: str, *, for_update: bool = False):
	if not frappe.db.get_value("User", supervisor, "enabled", for_update=for_update):
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
			if doc.actual_start_time and flt(doc.reported_minutes, 6) != flt(doc.actual_minutes, 6):
				frappe.throw(_("Timed wage minutes must equal the captured actual minutes."))
		elif flt(doc.reported_minutes):
			frappe.throw(_("Piecework reports cannot contain wage minutes."))
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
		"wage_type",
		"actual_start_time",
		"wage_rate",
		"wage_rate_revision",
		"rate",
	}
	if any(old.get(fieldname) != doc.get(fieldname) for fieldname in identity_fields):
		frappe.throw(_("Work session identity and start facts are immutable."))
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
	# Administrator/System Manager may assign another reviewer. Pre-lock both
	# reviewer users in a stable order before role checks to avoid U1 -> U2 / U2 -> U1.
	for reviewer_user in sorted({supervisor, frappe.session.user}):
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
	# Native pending_qty means quantity intentionally left for another Job Card.
	# This flow keeps reporting repeatedly on the same card, so it stays zero.
	job_card_doc.pending_qty = 0
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
				"rate",
			],
			as_dict=True,
		)
		rate = get_wage_rate(assignment.company, assignment.operation, today) if jc else None
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
		assignment.reportable_qty = reportable_qty(jc) if jc else 0
		assignment.active_report = active_session.name if active_session else None
		assignment.active_started_at = active_session.actual_start_time if active_session else None
		assignment.wage_type = (
			active_session.wage_type if active_session else (rate.wage_type if rate else None)
		)
		assignment.rate = flt(
			active_session.rate if active_session else (rate.rate if rate else 0)
		)
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
			assignment.block_code = "NO_REMAINING_QTY"
			assignment.block_message = _("The Job Card quantity is fully reported.")
		elif not rate:
			assignment.can_start = False
			assignment.block_code = "RATE_MISSING"
			assignment.block_message = _("No enabled wage rate exists for today.")
		elif rate.wage_type == "Time" and used_minutes >= daily_minutes_limit():
			assignment.can_start = False
			assignment.block_code = "DAILY_MINUTES_LIMIT"
			assignment.block_message = _("The daily wage-minute limit is already reached.")

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
	}


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
			frappe.throw(_("This start request id was already used for another assignment."))
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
			frappe.throw(_("This start request id was already used for another assignment."))
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
	if reportable_qty(jc, for_update=True) <= 0:
		frappe.throw(_("The Job Card has no remaining reportable quantity."))
	started_at = get_datetime(started_at or now_datetime())
	labor_date = getdate(started_at)
	if _month_is_confirmed(task.company, employee, labor_date, for_update=True):
		frappe.throw(_("This employee's monthly wage summary is already confirmed."))
	rate = get_wage_rate(task.company, task.operation, labor_date, for_update=True)
	if not rate:
		frappe.throw(_("No enabled wage rate exists for this operation today."))
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
	doc.flags.worker_reporting_action = True
	doc.insert(ignore_permissions=True)
	return doc


def finish_work_session(
	report: str,
	completed_qty,
	request_id: str | None = None,
	*,
	ended_at=None,
):
	request_id = _require_request_id(request_id, _("finish"))
	require_worker()
	request_employee = employee_for_user()
	precision = job_card_qty_precision()
	qty = flt(completed_qty, precision)
	completion_key = _hash_key(request_employee, frappe.session.user, "finish", request_id)
	initial = frappe.db.get_value(
		"Job Card Work Report",
		report,
		[
			"name",
			"assignment",
			"employee",
			"employee_user",
			"completion_request_key",
			"completed_qty",
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
		):
			frappe.throw(_("This finish request id was already used with different report values."))
		return doc
	if doc.status != "In Progress":
		frappe.throw(_("Only an active work session can be finished."))
	if _month_is_confirmed(task.company, employee, doc.labor_date, for_update=True):
		frappe.throw(_("This employee's monthly wage summary is already confirmed."))
	if qty <= 0:
		frappe.throw(_("Completed quantity must be greater than zero."))
	remaining = reportable_qty(jc, for_update=True)
	if qty > remaining:
		frappe.throw(_("This Job Card currently allows at most {0}.").format(remaining))
	ended_at = get_datetime(ended_at or now_datetime())
	started_at = get_datetime(doc.actual_start_time)
	if ended_at <= started_at:
		frappe.throw(_("Actual end time must be later than actual start time."))
	# Use the same calculation as ERPNext Job Card so the immutable report
	# snapshot and the native child row remain exactly comparable.
	actual_minutes = flt(time_diff_in_hours(ended_at, started_at) * 60, 6)
	if actual_minutes <= 0:
		frappe.throw(_("Actual production minutes must be greater than zero."))
	minutes = actual_minutes if doc.wage_type == "Time" else 0
	if doc.wage_type == "Time":
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
	doc.actual_end_time = ended_at
	doc.actual_minutes = actual_minutes
	doc.completed_qty = qty
	doc.reported_minutes = minutes
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
	doc.save(ignore_permissions=True)
	return doc


def cancel_work_session(report: str):
	initial = frappe.db.get_value(
		"Job Card Work Report",
		report,
		["name", "assignment"],
		as_dict=True,
	)
	if not initial:
		frappe.throw(_("Work session does not exist."))
	employee, _job_card, task = _lock_worker_assignment(initial.assignment)
	doc = frappe.get_doc("Job Card Work Report", report, for_update=True)
	if (
		doc.assignment != task.name
		or doc.employee != employee
		or doc.employee_user != frappe.session.user
	):
		frappe.throw(_("You can only cancel your own active work session."), frappe.PermissionError)
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


def get_review_dashboard():
	require_reviewer()
	base_filters = _report_filter_for_reviewer()
	pending_filters = {**base_filters, "status": "Pending Approval"}
	reports = frappe.get_all(
		"Job Card Work Report",
		filters=pending_filters,
		fields=[
			"name",
			"assignment",
			"job_card",
			"work_order",
			"operation",
			"employee",
			"employee_user",
			"labor_date",
			"wage_type",
			"completed_qty",
			"actual_start_time",
			"actual_end_time",
			"actual_minutes",
			"reported_minutes",
			"rate",
			"wage_amount",
			"submitted_at",
			"modified",
		],
		order_by="submitted_at asc, creation asc",
		limit=0,
	)
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
		elif not is_admin_reviewer() and snapshot.supervisor != frappe.session.user:
			review_block = frappe._dict(
				code="REVIEW_SCOPE",
				message=_("You can only review reports assigned to you."),
			)
		current_worker_user = frappe.db.get_value("Employee", report.employee, "user_id")
		if not review_block and frappe.session.user in {report.employee_user, current_worker_user}:
			review_block = frappe._dict(
				code="SELF_REVIEW",
				message=_("A worker cannot approve or reject their own production report."),
			)

		block = review_block or job_card_block(jc)
		if not block and flt(flt(jc.total_completed_qty, precision) + all_pending, precision) > flt(
			jc.for_quantity, precision
		):
			block = frappe._dict(
				code="QUANTITY_CONFLICT",
				message=_("Approved production plus pending work reports exceeds the Job Card quantity."),
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

	assignments = frappe.get_all(
		"Job Card Worker Assignment",
		filters={
			"status": "Active",
			**({"supervisor": frappe.session.user} if not is_admin_reviewer() else {}),
		},
		fields=["name", "job_card", "work_order", "operation", "employee", "supervisor", "notes", "assigned_at"],
		order_by="assigned_at desc",
		limit=0,
	)
	for assignment in assignments:
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
	processed = frappe.get_all(
		"Job Card Work Report",
		filters=processed_filters,
		fields=[
			"name",
			"job_card",
			"operation",
			"employee",
			"status",
			"completed_qty",
			"actual_start_time",
			"actual_end_time",
			"actual_minutes",
			"reported_minutes",
			"reviewed_at",
			"rejection_reason",
		],
		order_by="reviewed_at desc",
		limit=100,
	)
	wage_scope = (
		wage_manager_companies(throw_if_empty=False)
		if user_roles().intersection(WAGE_ROLES)
		else set()
	)
	return {
		"reports": reports,
		"assignments": assignments,
		"processed_today": processed,
		"can_manage_wages": wage_scope is None or bool(wage_scope),
		"companies": (
			frappe.get_all("Company", pluck="name", order_by="name", limit=0)
			if wage_scope is None
			else sorted(wage_scope)
		),
	}


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
	row = frappe.db.get_value(
		"Job Card Time Log",
		{"parent": doc.job_card, "custom_job_card_work_report": doc.name},
		[
			"name",
			"employee",
			"completed_qty",
			"from_time",
			"to_time",
			"time_in_mins",
			"custom_reported_employee",
		],
		as_dict=True,
		for_update=True,
	)
	if (
		not row
		or row.name != doc.job_card_time_log
		or row.employee != doc.employee
		or row.custom_reported_employee != doc.employee
		or flt(row.completed_qty, 6) != flt(doc.completed_qty, 6)
	):
		frappe.throw(_("Approved work report is inconsistent with its Job Card quantity row."))
	if doc.actual_start_time or doc.actual_end_time:
		if (
			get_datetime(row.from_time) != get_datetime(doc.actual_start_time)
			or get_datetime(row.to_time) != get_datetime(doc.actual_end_time)
			or flt(row.time_in_mins, 6) != flt(doc.actual_minutes, 6)
		):
			frappe.throw(_("Approved work report is inconsistent with its Job Card time row."))
	elif row.from_time or row.to_time or flt(row.time_in_mins):
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
	if flt(flt(jc_values.total_completed_qty, precision) + all_pending, precision) > flt(
		jc_values.for_quantity, precision
	):
		frappe.throw(_("Pending reports no longer fit the Job Card quantity. Reject the incorrect report."))

	job_card = frappe.get_doc("Job Card", doc.job_card, for_update=True)
	job_card.append(
		"time_logs",
		{
			"employee": doc.employee,
			"completed_qty": flt(doc.completed_qty),
			"from_time": doc.actual_start_time,
			"to_time": doc.actual_end_time,
			"custom_job_card_work_report": doc.name,
			"custom_reported_employee": doc.employee,
		},
	)
	job_card.pending_qty = 0
	job_card.flags.worker_reporting_approval = doc.name
	job_card.save(ignore_permissions=True)
	if flt(job_card.total_completed_qty, precision) != flt(
		flt(jc_values.total_completed_qty, precision) + flt(doc.completed_qty, precision),
		precision,
	):
		frappe.throw(_("Job Card did not accept the approved quantity exactly; approval was rolled back."))
	if flt(job_card.total_completed_qty, precision) == flt(job_card.for_quantity, precision):
		# ERPNext's Job Card submit updates the parent Work Order through a fresh
		# document, which does not inherit this service's ignore_permissions flag.
		# Perform only that native, fully validated submit as Administrator, then
		# immediately restore the real reviewer used by the immutable report audit.
		previous_user = frappe.session.user
		try:
			frappe.set_user("Administrator")
			job_card.flags.ignore_permissions = True
			job_card.submit()
		finally:
			frappe.set_user(previous_user)

	doc.status = "Approved"
	doc.reviewed_by = reviewer
	doc.reviewed_at = now_datetime()
	doc.review_ip = audit.ip
	doc.review_user_agent = audit.user_agent
	doc.job_card_time_log = frappe.db.get_value(
		"Job Card Time Log",
		{"parent": doc.job_card, "custom_job_card_work_report": doc.name},
		"name",
		for_update=True,
	)
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


def search_draft_job_cards(txt: str = "", start: int = 0, page_len: int = 20):
	require_reviewer()
	if frappe.db.get_single_value("Manufacturing Settings", "enforce_time_logs"):
		return []
	companies = _supervisor_companies(frappe.session.user)
	txt = f"%{str(txt or '').strip()}%"
	company_condition = "" if companies is None else "and jc.company in %(companies)s"
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
		  and ifnull(jc.process_loss_qty, 0) = 0
		  and ifnull(jc.company, '') != ''
		  and ifnull(jc.operation, '') != ''
		  and ifnull(jc.operation_id, '') != ''
		  and ifnull(jc.for_quantity, 0) > 0
		  and ifnull(jc.total_completed_qty, 0) < ifnull(jc.for_quantity, 0)
		  {company_condition}
		  {supervisor_condition}
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
		  )
		order by jc.modified desc
		limit %(start)s, %(page_len)s
		""",
		{
			"txt": txt,
			"companies": tuple(sorted(companies or [])),
			"current_user": frappe.session.user,
			"today": getdate(nowdate()),
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
				'Production Supervisor', 'Production Wage Manager', 'System Manager',
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
