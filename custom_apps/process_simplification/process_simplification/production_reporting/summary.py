from __future__ import annotations

import hashlib

import frappe
from frappe import _
from frappe.utils import flt, get_first_day, get_last_day, getdate, now_datetime, nowdate

from process_simplification.production_reporting.domain import money, require_wage_manager


def get_wage_management_context():
	companies = require_wage_manager()
	return {
		"companies": (
			frappe.get_all("Company", pluck="name", order_by="name", limit=0)
			if companies is None
			else sorted(companies)
		)
	}


def _summary_key(company: str, employee: str, month_start) -> str:
	return hashlib.sha256(f"{company}|{employee}|{getdate(month_start)}".encode()).hexdigest()


def _report_fields():
	return [
		"name",
		"employee",
		"employee_user",
		"company",
		"job_card",
		"operation",
		"operation_id",
		"labor_date",
		"wage_type",
		"completed_qty",
		"reported_minutes",
		"rate",
		"wage_amount",
		"status",
		"monthly_summary",
	]


def _month_report_rows(
	company: str,
	employee: str,
	month_start,
	*,
	status: str | None = None,
	for_update: bool = False,
):
	month_start = get_first_day(month_start)
	month_end = get_last_day(month_start)
	report = frappe.qb.DocType("Job Card Work Report")
	condition = (
		(report.company == company)
		& (report.employee == employee)
		& (report.labor_date >= month_start)
		& (report.labor_date <= month_end)
	)
	if status:
		condition &= report.status == status
	query = (
		frappe.qb.from_(report)
		.select(*(report[fieldname] for fieldname in _report_fields()), report.creation)
		.where(condition)
		.orderby(report.labor_date)
		.orderby(report.creation)
		.orderby(report.name)
	)
	if for_update:
		query = query.for_update()
	return query.run(as_dict=True)


def _eligible_reports(
	company: str,
	employee: str,
	month_start,
	*,
	include_current_summary: str | None = None,
	for_update: bool = False,
):
	rows = _month_report_rows(
		company,
		employee,
		month_start,
		status="Approved",
		for_update=for_update,
	)
	return [
		row
		for row in rows
		if (not row.monthly_summary or row.monthly_summary == include_current_summary)
	]


def _validate_detail_source(doc, row, month_start, month_end):
	source = frappe.db.get_value(
		"Job Card Work Report",
		row.source_report,
		_report_fields(),
		as_dict=True,
		for_update=True,
	)
	if (
		not source
		or source.status != "Approved"
		or source.employee != doc.employee
		or source.company != doc.company
	):
		frappe.throw(_("Work report {0} is not eligible for this monthly summary.").format(row.source_report))
	if source.monthly_summary and source.monthly_summary != doc.name:
		frappe.throw(_("Work report {0} is already included in another summary.").format(row.source_report))
	if not (getdate(month_start) <= getdate(source.labor_date) <= getdate(month_end)):
		frappe.throw(_("Work report {0} is outside the summary month.").format(row.source_report))
	comparisons = {
		"job_card": source.job_card,
		"operation": source.operation,
		"operation_id": source.operation_id,
		"labor_date": source.labor_date,
		"wage_type": source.wage_type,
		"completed_qty": source.completed_qty,
		"reported_minutes": source.reported_minutes,
		"rate": source.rate,
		"amount": source.wage_amount,
	}
	for fieldname, expected in comparisons.items():
		actual = row.get(fieldname)
		if fieldname in {"completed_qty", "reported_minutes", "rate", "amount"}:
			if flt(actual, 6) != flt(expected, 6):
				frappe.throw(_("Monthly summary details must match immutable report snapshots."))
		elif actual != expected:
			frappe.throw(_("Monthly summary details must match immutable report snapshots."))
	return source


def validate_summary_document(doc):
	if not getattr(doc.flags, "worker_reporting_action", False):
		frappe.throw(_("Monthly wage summaries can only be changed through wage-summary actions."))
	require_wage_manager(doc.company)
	month_start = get_first_day(doc.month_start)
	month_end = get_last_day(month_start)
	doc.month_start = month_start
	doc.month_end = month_end
	doc.summary_key = _summary_key(doc.company, doc.employee, month_start)
	seen = set()
	source_users = set()
	piecework_amount = 0.0
	time_amount = 0.0
	for row in doc.details:
		if not row.source_report or row.source_report in seen:
			frappe.throw(_("Monthly summary must contain unique work reports."))
		seen.add(row.source_report)
		source = _validate_detail_source(doc, row, month_start, month_end)
		if source.employee_user:
			source_users.add(source.employee_user)
		if source.wage_type == "Piecework":
			piecework_amount += flt(source.wage_amount)
		else:
			time_amount += flt(source.wage_amount)
	doc.piecework_amount = money(piecework_amount)
	doc.time_amount = money(time_amount)
	doc.total_amount = money(piecework_amount + time_amount)
	doc.employee_user = next(iter(source_users)) if len(source_users) == 1 else None


def build_monthly_summaries(company: str, month_start, employee: str | None = None):
	require_wage_manager(company)
	month_start = get_first_day(month_start)
	month_end = get_last_day(month_start)
	filters = {
		"company": company,
		"labor_date": ("between", [month_start, month_end]),
		"status": "Approved",
		"monthly_summary": ("is", "not set"),
	}
	employees = (
		[employee]
		if employee
		else sorted(
			set(
				frappe.get_all(
					"Job Card Work Report",
					filters=filters,
					pluck="employee",
					limit=0,
				)
			)
		)
	)

	result = []
	for employee_name in sorted(employees):
		if not frappe.db.get_value("Employee", employee_name, "name", for_update=True):
			frappe.throw(_("Employee {0} does not exist.").format(employee_name))
		key = _summary_key(company, employee_name, month_start)
		existing = frappe.db.get_value(
			"Monthly Worker Wage Summary",
			{"summary_key": key, "docstatus": ("<", 2)},
			["name", "docstatus"],
			as_dict=True,
			for_update=True,
		)
		if existing and existing.docstatus == 1:
			result.append(existing.name)
			continue
		employee_rows = [
			row
			for row in _month_report_rows(
				company,
				employee_name,
				month_start,
				status="Approved",
				for_update=True,
			)
			if not row.monthly_summary
		]
		if not employee_rows:
			continue
		doc = (
			frappe.get_doc("Monthly Worker Wage Summary", existing.name, for_update=True)
			if existing
			else frappe.new_doc("Monthly Worker Wage Summary")
		)
		if not existing:
			doc.company = company
			doc.employee = employee_name
			doc.month_start = month_start
		doc.set("details", [])
		for row in employee_rows:
			doc.append(
				"details",
				{
					"source_report": row.name,
					"job_card": row.job_card,
					"operation": row.operation,
					"operation_id": row.operation_id,
					"labor_date": row.labor_date,
					"wage_type": row.wage_type,
					"completed_qty": row.completed_qty,
					"reported_minutes": row.reported_minutes,
					"rate": row.rate,
					"amount": row.wage_amount,
				},
			)
		doc.flags.worker_reporting_action = True
		if doc.is_new():
			doc.insert(ignore_permissions=True)
		else:
			doc.save(ignore_permissions=True)
		result.append(doc.name)
	return {"summaries": result, "month_start": month_start}


def _lock_summary_context(name: str):
	initial = frappe.db.get_value(
		"Monthly Worker Wage Summary",
		name,
		["name", "company", "employee", "month_start", "docstatus"],
		as_dict=True,
	)
	if not initial:
		frappe.throw(_("Monthly wage summary does not exist."))
	frappe.db.get_value("Employee", initial.employee, "name", for_update=True)
	doc = frappe.get_doc("Monthly Worker Wage Summary", name, for_update=True)
	current_identity = (doc.company, doc.employee, getdate(doc.month_start))
	initial_identity = (initial.company, initial.employee, getdate(initial.month_start))
	if current_identity != initial_identity:
		frappe.throw(_("Monthly summary identity changed while it was being locked; retry the action."))
	_month_report_rows(
		doc.company,
		doc.employee,
		doc.month_start,
		for_update=True,
	)
	return doc


def _assert_complete_source_set(doc):
	pending = len(
		_month_report_rows(
			doc.company,
			doc.employee,
			doc.month_start,
			status="Pending Approval",
			for_update=True,
		)
	)
	if pending:
		frappe.throw(_("Review all pending work reports for this employee and month first."))
	eligible = _eligible_reports(
		doc.company,
		doc.employee,
		doc.month_start,
		include_current_summary=doc.name,
		for_update=True,
	)
	eligible_names = {row.name for row in eligible}
	detail_names = [row.source_report for row in doc.details]
	if eligible_names != set(detail_names) or len(detail_names) != len(set(detail_names)):
		frappe.throw(_("Rebuild the summary so it contains every eligible approved report exactly once."))


def confirm_monthly_summary(name: str):
	require_wage_manager()
	doc = _lock_summary_context(name)
	require_wage_manager(doc.company, for_update=True)
	if doc.docstatus == 1:
		return doc
	if doc.docstatus != 0:
		frappe.throw(_("Only a Draft monthly summary can be confirmed."))
	if getdate(doc.month_end) >= getdate(nowdate()):
		frappe.throw(_("A monthly wage summary can only be confirmed after the natural month ends."))
	_assert_complete_source_set(doc)
	doc.flags.worker_reporting_action = True
	doc.flags.ignore_permissions = True
	doc.submit()
	return doc


def before_submit_summary(doc):
	if not getattr(doc.flags, "worker_reporting_action", False):
		frappe.throw(_("Confirm the monthly summary through its controlled action."))
	if getdate(doc.month_end) >= getdate(nowdate()):
		frappe.throw(_("A monthly wage summary can only be confirmed after the natural month ends."))
	_assert_complete_source_set(doc)


def on_submit_summary(doc):
	if not getattr(doc.flags, "worker_reporting_action", False):
		frappe.throw(_("Confirm the monthly summary through its controlled action."))
	for row in doc.details:
		source = frappe.db.get_value(
			"Job Card Work Report",
			row.source_report,
			["status", "monthly_summary"],
			as_dict=True,
			for_update=True,
		)
		if not source or source.status != "Approved" or (source.monthly_summary and source.monthly_summary != doc.name):
			frappe.throw(_("Work report {0} is no longer eligible.").format(row.source_report))
		frappe.db.set_value(
			"Job Card Work Report",
			row.source_report,
			"monthly_summary",
			doc.name,
			update_modified=False,
		)
	frappe.db.set_value(
		"Monthly Worker Wage Summary",
		doc.name,
		{"confirmed_by": frappe.session.user, "confirmed_at": now_datetime()},
		update_modified=False,
	)


def before_cancel_summary(doc):
	frappe.throw(_("A confirmed monthly wage summary is final and cannot be cancelled."))
