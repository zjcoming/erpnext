from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

import frappe
from frappe import _
from frappe.query_builder import Order
from frappe.utils import flt, getdate

from process_simplification.production_reporting.constants import (
	ADMIN_REVIEW_ROLES,
	REVIEW_ROLES,
	SYSTEM_MANAGER_ROLE,
	WAGE_ROLES,
	WAGE_MANAGER_ROLE,
	WORKER_ROLE,
)


def user_roles(user: str | None = None, *, for_update: bool = False) -> set[str]:
	user = user or frappe.session.user
	if user == "Administrator":
		# Frappe grants Administrator every role without requiring tabHas Role rows.
		return {*REVIEW_ROLES, *WAGE_ROLES}
	if not for_update:
		return set(frappe.get_roles(user))
	if not frappe.db.get_value("User", user, "name", for_update=True):
		return set()
	has_role = frappe.qb.DocType("Has Role")
	rows = (
		frappe.qb.from_(has_role)
		.select(has_role.role)
		.where(
			(has_role.parent == user)
			& (has_role.parenttype == "User")
			& (has_role.parentfield == "roles")
		)
		.for_update()
	).run(as_dict=True)
	return {row.role for row in rows}


def assert_worker_user_isolated(user: str, *, for_update: bool = False):
	roles = user_roles(user, for_update=for_update)
	if WORKER_ROLE not in roles:
		frappe.throw(_("The worker account must have the Production Worker role."), frappe.PermissionError)
	incompatible = roles.intersection(
		{
			"Manufacturing User",
			"Manufacturing Manager",
			"Shop Floor User",
			"Shop Floor Manager",
			*REVIEW_ROLES,
			WAGE_MANAGER_ROLE,
		}
	)
	if incompatible:
		frappe.throw(
			_(
				"The worker account cannot also hold production review, wage management, "
				"standard manufacturing, or Shop Floor roles: {0}."
			).format(", ".join(sorted(incompatible)))
		)


def require_worker():
	assert_worker_user_isolated(frappe.session.user)


def require_reviewer(*, for_update: bool = False):
	if not user_roles(for_update=for_update).intersection(REVIEW_ROLES):
		frappe.throw(_("You are not permitted to review production reports."), frappe.PermissionError)


def wage_manager_companies(
	user: str | None = None,
	*,
	for_update: bool = False,
	throw_if_empty: bool = True,
) -> set[str] | None:
	"""Return the explicit Company scope for a wage manager.

	System Manager and Administrator intentionally receive an unrestricted scope.
	A wage-only account must have at least one top-level Company User Permission;
	absence of a scope is fail-closed because reports contain sensitive wage data.
	"""
	user = user or frappe.session.user
	roles = user_roles(user, for_update=for_update)
	if SYSTEM_MANAGER_ROLE in roles:
		return None
	if WAGE_MANAGER_ROLE not in roles:
		if throw_if_empty:
			frappe.throw(_("Only a production wage manager can perform this action."), frappe.PermissionError)
		return set()

	permission = frappe.qb.DocType("User Permission")
	query = (
		frappe.qb.from_(permission)
		.select(permission.for_value)
		.where(
			(permission.user == user)
			& (permission.allow == "Company")
			& ((permission.applicable_for.isnull()) | (permission.applicable_for == ""))
		)
		.orderby(permission.for_value)
	)
	if for_update:
		query = query.for_update()
	companies = {row.for_value for row in query.run(as_dict=True) if row.for_value}
	if not companies and throw_if_empty:
		frappe.throw(
			_("Production wage managers require an explicit Company User Permission."),
			frappe.PermissionError,
		)
	return companies


def require_wage_manager(
	company: str | None = None,
	*,
	for_update: bool = False,
) -> set[str] | None:
	companies = wage_manager_companies(for_update=for_update)
	if company and companies is not None and company not in companies:
		frappe.throw(
			_("You are not permitted to manage production wages for company {0}.").format(company),
			frappe.PermissionError,
		)
	return companies


def is_admin_reviewer(*, for_update: bool = False) -> bool:
	return bool(user_roles(for_update=for_update).intersection(ADMIN_REVIEW_ROLES))


def employee_for_user(user: str | None = None, *, required: bool = True) -> str | None:
	user = user or frappe.session.user
	employees = frappe.get_all(
		"Employee",
		filters={"user_id": user, "status": "Active"},
		pluck="name",
		limit=2,
	)
	if len(employees) > 1:
		frappe.throw(_("This user is linked to more than one active Employee."))
	if not employees and required:
		frappe.throw(_("This user is not linked to an active Employee."))
	return employees[0] if employees else None


def employee_user(employee: str, *, for_update: bool = False) -> str:
	row = frappe.db.get_value(
		"Employee",
		employee,
		["user_id", "status"],
		as_dict=True,
		for_update=for_update,
	)
	if not row or row.status != "Active" or not row.user_id:
		frappe.throw(_("Employee {0} must be active and linked to a User.").format(employee))
	if not frappe.db.get_value("User", row.user_id, "enabled", for_update=for_update):
		frappe.throw(_("The User linked to employee {0} is disabled.").format(employee))
	assert_worker_user_isolated(row.user_id, for_update=for_update)
	return row.user_id


def money(value) -> float:
	return float(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def sum_field(doctype: str, filters: dict, fieldname: str) -> float:
	return sum(flt(value) for value in frappe.get_all(doctype, filters=filters, pluck=fieldname, limit=0))


def daily_minutes_limit() -> float:
	configured = flt(frappe.conf.get("worker_reporting_daily_minutes_limit") or 1440)
	return min(1440.0, max(1.0, configured))


def job_card_qty_precision() -> int:
	return frappe.get_precision("Job Card", "total_completed_qty") or 6


def request_audit() -> frappe._dict:
	request = getattr(frappe.local, "request", None)
	user_agent = request.headers.get("User-Agent") if request and request.headers else None
	return frappe._dict(
		ip=str(getattr(frappe.local, "request_ip", "") or "")[:140],
		user_agent=str(user_agent or "")[:140],
	)


def job_card_values(job_card: str, *, for_update: bool = False) -> frappe._dict | None:
	return frappe.db.get_value(
		"Job Card",
		job_card,
		[
			"name",
			"work_order",
			"company",
			"operation",
			"operation_id",
			"for_quantity",
			"total_completed_qty",
			"pending_qty",
			"process_loss_qty",
			"docstatus",
			"is_corrective_job_card",
			"track_semi_finished_goods",
			"is_subcontracted",
			"custom_worker_reporting_enabled",
			"custom_worker_reporting_supervisor",
		],
		as_dict=True,
		for_update=for_update,
	)


def has_sub_operations(job_card: str, *, for_update: bool = False) -> bool:
	sub_operation = frappe.qb.DocType("Job Card Operation")
	query = (
		frappe.qb.from_(sub_operation)
		.select(sub_operation.name)
		.where(
			(sub_operation.parent == job_card)
			& (sub_operation.parenttype == "Job Card")
			& (sub_operation.parentfield == "sub_operations")
		)
		.limit(1)
	)
	if for_update:
		query = query.for_update()
	return bool(query.run())


def job_card_block(
	job_card: frappe._dict | None,
	*,
	require_draft: bool = True,
	for_update: bool = False,
) -> frappe._dict | None:
	if not job_card:
		return frappe._dict(code="JOB_CARD_MISSING", message=_("Job Card does not exist."))
	if require_draft and job_card.docstatus != 0:
		return frappe._dict(
			code="JOB_CARD_NOT_DRAFT",
			message=_("Worker reporting is only allowed while the Job Card is Draft."),
		)
	if job_card.is_corrective_job_card:
		return frappe._dict(
			code="CORRECTIVE_JOB_CARD",
			message=_("Corrective Job Cards are not supported by simplified worker reporting."),
		)
	if has_sub_operations(job_card.name, for_update=for_update):
		return frappe._dict(
			code="SUB_OPERATIONS",
			message=_("Job Cards with sub-operations are not supported by simplified worker reporting."),
		)
	if job_card.track_semi_finished_goods or job_card.is_subcontracted:
		return frappe._dict(
			code="SPECIAL_JOB_CARD",
			message=_("Semi-finished or subcontracted Job Cards are not supported in worker reporting v1."),
		)
	if frappe.db.get_single_value(
		"Manufacturing Settings",
		"enforce_time_logs",
		for_update=for_update,
	):
		return frappe._dict(
			code="TIME_LOG_SETTING",
			message=_(
				"Manufacturing Settings requires From/To Time on Job Card rows. "
				"Disable that setting before using worker-entered wage minutes."
			),
		)
	if flt(job_card.process_loss_qty):
		return frappe._dict(
			code="PROCESS_LOSS",
			message=_("Process loss is not supported by simplified worker reporting."),
		)
	if job_card.work_order:
		work_order_status = frappe.db.get_value(
			"Work Order",
			job_card.work_order,
			"status",
			for_update=for_update,
		)
		if work_order_status in {"Closed", "Stopped", "Completed", "Cancelled"}:
			return frappe._dict(
				code="WORK_ORDER_UNAVAILABLE",
				message=_("The Work Order is not executable while its status is {0}.").format(
					work_order_status
				),
			)
	return None


def assert_supported_job_card(
	job_card: frappe._dict,
	*,
	require_draft: bool = True,
	for_update: bool = False,
):
	block = job_card_block(job_card, require_draft=require_draft, for_update=for_update)
	if block:
		frappe.throw(block.message)


def _report_qty_rows(
	job_card: str,
	status: str,
	*,
	exclude_report: str | None = None,
	for_update: bool = False,
):
	report = frappe.qb.DocType("Job Card Work Report")
	condition = (report.job_card == job_card) & (report.status == status)
	if exclude_report:
		condition &= report.name != exclude_report
	query = frappe.qb.from_(report).select(report.name, report.completed_qty).where(condition)
	if for_update:
		query = query.for_update()
	return query.run(as_dict=True)


def pending_report_qty(
	job_card: str,
	*,
	exclude_report: str | None = None,
	for_update: bool = False,
) -> float:
	precision = job_card_qty_precision()
	return flt(
		sum(
			flt(row.completed_qty, precision)
			for row in _report_qty_rows(
				job_card,
				"Pending Approval",
				exclude_report=exclude_report,
				for_update=for_update,
			)
		),
		precision,
	)


def approved_report_qty(job_card: str, *, for_update: bool = False) -> float:
	precision = job_card_qty_precision()
	return flt(
		sum(
			flt(row.completed_qty, precision)
			for row in _report_qty_rows(job_card, "Approved", for_update=for_update)
		),
		precision,
	)


def reportable_qty(job_card: frappe._dict, *, for_update: bool = False) -> float:
	precision = job_card_qty_precision()
	return flt(
		max(
			0.0,
			flt(job_card.for_quantity, precision)
			- flt(job_card.total_completed_qty, precision)
			- pending_report_qty(job_card.name, for_update=for_update),
		),
		precision,
	)


def work_order_material_capacity(
	job_card: frappe._dict,
	*,
	for_update: bool = False,
) -> float | None:
	"""Return the finished-good quantity currently covered by issued materials.

	``None`` means the Work Order consumes directly (``skip_transfer``) or has no
	positive material requirement, so worker reporting keeps the native Job Card
	quantity boundary. Otherwise the v16 Work Order transfer quantity is the
	maximum production quantity that may be reported for each operation.
	"""
	if not job_card or not job_card.work_order:
		return 0.0
	work_order = frappe.db.get_value(
		"Work Order",
		job_card.work_order,
		["qty", "skip_transfer", "material_transferred_for_manufacturing"],
		as_dict=True,
		for_update=for_update,
	)
	if not work_order:
		return 0.0
	if work_order.skip_transfer:
		return None
	required_item = frappe.db.get_value(
		"Work Order Item",
		{"parent": job_card.work_order, "required_qty": [">", 0]},
		"name",
		for_update=for_update,
	)
	if not required_item:
		return None
	return max(
		0.0,
		min(
			flt(work_order.material_transferred_for_manufacturing),
			flt(work_order.qty),
		),
	)


def material_reportable_qty(job_card: frappe._dict, *, for_update: bool = False) -> float:
	"""Apply both Job Card quantity and issued-material boundaries."""
	precision = job_card_qty_precision()
	job_card_remaining = reportable_qty(job_card, for_update=for_update)
	material_capacity = work_order_material_capacity(job_card, for_update=for_update)
	if material_capacity is None:
		return job_card_remaining
	used = flt(job_card.total_completed_qty, precision) + pending_report_qty(
		job_card.name,
		for_update=for_update,
	)
	return flt(
		max(0.0, min(job_card_remaining, flt(material_capacity, precision) - used)),
		precision,
	)


def get_wage_rate(
	company: str,
	operation: str,
	labor_date,
	*,
	for_update: bool = False,
) -> frappe._dict | None:
	labor_date = getdate(labor_date)
	rate = frappe.qb.DocType("Operation Wage Rate")
	query = (
		frappe.qb.from_(rate)
		.select(
			rate.name,
			rate.wage_type,
			rate.rate,
			rate.revision,
			rate.valid_from,
			rate.valid_to,
		)
		.where(
			(rate.company == company)
			& (rate.operation == operation)
			& (rate.enabled == 1)
			& (rate.valid_from <= labor_date)
			& ((rate.valid_to.isnull()) | (rate.valid_to >= labor_date))
		)
		.orderby(rate.valid_from, order=Order.desc)
		.orderby(rate.modified, order=Order.desc)
		.limit(2)
	)
	if for_update:
		query = query.for_update()
	rows = query.run(as_dict=True)
	if len(rows) > 1:
		frappe.throw(_("More than one wage rate is active for this operation and date."))
	return rows[0] if rows else None


def validate_wage_rate(doc):
	require_wage_manager(doc.company)
	if doc.wage_type not in {"Piecework", "Time"}:
		frappe.throw(_("Wage type must be Piecework or Time."))
	if flt(doc.rate) <= 0:
		frappe.throw(_("Wage rate must be greater than zero."))
	if doc.valid_to and getdate(doc.valid_to) < getdate(doc.valid_from):
		frappe.throw(_("Valid To cannot be earlier than Valid From."))

	old = None if doc.is_new() else doc.get_doc_before_save()
	if old:
		for fieldname in ("company", "operation"):
			if old.get(fieldname) != doc.get(fieldname):
				frappe.throw(_("{0} cannot change after a wage rate is created.").format(doc.meta.get_label(fieldname)))
		for fieldname in ("valid_from", "valid_to"):
			old_value = getdate(old.get(fieldname)) if old.get(fieldname) else None
			new_value = getdate(doc.get(fieldname)) if doc.get(fieldname) else None
			if old_value != new_value:
				frappe.throw(_("Wage-rate validity dates are immutable; create a replacement rate instead."))
		if not old.enabled and doc.enabled:
			frappe.throw(_("A disabled wage rate cannot be re-enabled; create a replacement rate instead."))

	if not old:
		doc.revision = 1
	else:
		tracked = ("company", "operation", "wage_type", "rate", "valid_from", "valid_to", "enabled")
		doc.revision = int(old.revision or 0) + 1 if old and any(old.get(f) != doc.get(f) for f in tracked) else int(old.revision or 1)

	if not doc.enabled:
		return
	if old:
		# Enabled existing rows keep an immutable scope and date interval. New rows
		# are serialized below, so changing only the future rate snapshot cannot
		# introduce an overlap and does not need a second lock scope.
		return
	if not frappe.db.get_value("Operation", doc.operation, "name", for_update=True):
		frappe.throw(_("Operation {0} does not exist.").format(doc.operation))
	new_end = getdate(doc.valid_to) if doc.valid_to else getdate("9999-12-31")
	rate = frappe.qb.DocType("Operation Wage Rate")
	overlap = (
		frappe.qb.from_(rate)
		.select(rate.name)
		.where(
			(rate.name != (doc.name or ""))
			& (rate.company == doc.company)
			& (rate.operation == doc.operation)
			& (rate.enabled == 1)
			& (rate.valid_from <= new_end)
			& ((rate.valid_to.isnull()) | (rate.valid_to >= getdate(doc.valid_from)))
		)
		.limit(1)
		.for_update()
	).run()
	if overlap:
		frappe.throw(_("An enabled wage rate already overlaps this operation and date range."))


def validate_wage_rate_delete(doc):
	require_wage_manager(doc.company)
	frappe.throw(_("Wage rates are permanent audit records and cannot be deleted; disable them instead."))
