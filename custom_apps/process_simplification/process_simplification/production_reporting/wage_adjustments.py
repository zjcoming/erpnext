from __future__ import annotations

from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import get_first_day, getdate

from process_simplification.management_access import OWNER_ROLE, SYSTEM_MANAGER_ROLE
from process_simplification.production_reporting.domain import require_wage_manager, user_roles

ADJUSTMENT = "Worker Wage Adjustment"
APPLICATION = "Monthly Worker Wage Adjustment Detail"
SUMMARY = "Monthly Worker Wage Summary"
CENT = Decimal("0.01")


def _money(value, *, positive=False):
	try:
		if isinstance(value, bool):
			raise ValueError
		amount = Decimal(str(value))
		if (
			not amount.is_finite()
			or abs(amount) > Decimal("999999999999.99")
			or amount != amount.quantize(CENT)
		):
			raise ValueError
		if positive and amount <= 0:
			raise ValueError
		return amount.quantize(CENT)
	except (InvalidOperation, ValueError, TypeError):
		frappe.throw(
			_("Wage adjustment amounts must be finite, positive amounts with at most two decimal places.")
			if positive
			else _("Invalid wage amount.")
		)


def can_record_adjustments():
	return bool(user_roles().intersection({OWNER_ROLE, SYSTEM_MANAGER_ROLE}))


def require_adjustment_owner(company=None, *, for_update=False):
	if not user_roles(for_update=for_update).intersection({OWNER_ROLE, SYSTEM_MANAGER_ROLE}):
		frappe.throw(_("Only the company owner can record wage adjustments."), frappe.PermissionError)
	return require_wage_manager(company, for_update=for_update)


def _lock_employee(company, employee):
	row = frappe.db.get_value("Employee", employee, ["name", "company"], as_dict=True, for_update=True)
	if not row or row.company != company:
		frappe.throw(_("The employee must belong to the selected company."))


def _lock_employee_summaries(company, employee):
	parent = frappe.qb.DocType(SUMMARY)
	names = (
		frappe.qb.from_(parent)
		.select(parent.name)
		.where((parent.company == company) & (parent.employee == employee))
		.orderby(parent.name)
		.for_update()
	).run(pluck=True)
	return [frappe.get_doc(SUMMARY, name, for_update=True) for name in names]


def _applications(names, *, exclude_summary=None, for_update=False):
	if not names:
		return []
	child = frappe.qb.DocType(APPLICATION)
	parent = frappe.qb.DocType(SUMMARY)
	query = (
		frappe.qb.from_(child)
		.join(parent)
		.on(child.parent == parent.name)
		.select(
			child.wage_adjustment,
			child.amount,
			parent.name.as_("summary"),
			parent.month_start,
			parent.docstatus,
		)
		.where(
			(child.parenttype == SUMMARY)
			& (child.parentfield == "adjustments")
			& child.wage_adjustment.isin(names)
			& (parent.docstatus < 2)
		)
		.orderby(parent.month_start)
		.orderby(parent.name)
	)
	if exclude_summary:
		query = query.where(parent.name != exclude_summary)
	if for_update:
		query = query.for_update()
	return query.run(as_dict=True)


def _balances(docs, *, exclude_summary=None, for_update=False):
	rows = _applications([doc.name for doc in docs], exclude_summary=exclude_summary, for_update=for_update)
	result = {}
	for doc in docs:
		applications = [row for row in rows if row.wage_adjustment == doc.name]
		original = _money(doc.amount, positive=True)
		applied = sum(
			(_money(row.amount, positive=True) for row in applications if row.docstatus == 1), Decimal("0")
		)
		if applied > original:
			frappe.throw(_("Confirmed wage applications exceed the original adjustment."))
		result[doc.name] = {
			"name": doc.name,
			"type": doc.adjustment_type,
			"original_amount": float(original),
			"applied_amount": float(applied),
			"remaining_amount": float(original - applied) if doc.docstatus != 2 else 0.0,
			"applications": [
				{
					"summary": row.summary,
					"month_start": row.month_start,
					"docstatus": row.docstatus,
					"amount": row.amount,
				}
				for row in applications
			],
		}
	return result


def get_wage_adjustment_balance(name):
	require_wage_manager()
	doc = frappe.get_doc(ADJUSTMENT, name)
	require_wage_manager(doc.company)
	return _balances([doc])[doc.name]


def _payload(company, employee, month_start, adjustment_type, amount, reason, source_report):
	if (
		not isinstance(company, str)
		or not company.strip()
		or not isinstance(employee, str)
		or not employee.strip()
		or not month_start
	):
		frappe.throw(_("Company, employee and the first settlement month are required."))
	if adjustment_type not in {"Supplement", "Deduction"}:
		frappe.throw(_("Wage adjustment type must be Supplement or Deduction."))
	if not isinstance(reason, str) or not reason.strip():
		frappe.throw(_("A wage adjustment reason is required."))
	if source_report and not isinstance(source_report, str):
		frappe.throw(_("Invalid source work report."))
	return {
		"company": company,
		"employee": employee,
		"month_start": get_first_day(month_start),
		"adjustment_type": adjustment_type,
		"amount": float(_money(amount, positive=True)),
		"reason": reason.strip(),
		"source_report": source_report or None,
	}


def _same_payload(doc, values):
	return all(
		(
			getdate(doc.get(key)) == getdate(value)
			if key == "month_start"
			else _money(doc.get(key)) == _money(value)
			if key == "amount"
			else (doc.get(key) or None) == (value or None)
		)
		for key, value in values.items()
	)


def record_wage_adjustment(
	company, employee, month_start, adjustment_type, amount, reason, source_report=None, request_id=None
):
	require_adjustment_owner(company)
	values = _payload(company, employee, month_start, adjustment_type, amount, reason, source_report)
	if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 140:
		frappe.throw(_("A valid wage adjustment request ID is required."))
	_lock_employee(company, employee)
	summaries = _lock_employee_summaries(company, employee)
	require_adjustment_owner(company, for_update=True)
	existing = frappe.db.get_value(
		ADJUSTMENT, {"request_id": request_id}, ["name", "company", "employee"], as_dict=True, for_update=True
	)
	if existing:
		require_adjustment_owner(existing.company)
		if existing.company != company or existing.employee != employee:
			frappe.throw(_("The wage adjustment request ID was already used with different values."))
		doc = frappe.get_doc(ADJUSTMENT, existing.name, for_update=True)
		if not _same_payload(doc, values):
			frappe.throw(_("The wage adjustment request ID was already used with different values."))
	else:
		latest = max((getdate(doc.month_start) for doc in summaries if doc.docstatus == 1), default=None)
		if latest and getdate(values["month_start"]) <= latest:
			frappe.throw(_("Record the adjustment in an unconfirmed month after the latest confirmed month."))
		doc = frappe.new_doc(ADJUSTMENT)
		doc.update(values)
		doc.request_id = request_id
		doc.flags.wage_adjustment_action = True
		doc.insert(ignore_permissions=True)
		doc.flags.ignore_permissions = True
		doc.submit()
	balance = _balances([doc], for_update=True)[doc.name]
	return {"name": doc.name, "docstatus": doc.docstatus, "remaining_amount": balance["remaining_amount"]}


def cancel_wage_adjustment(name):
	require_adjustment_owner()
	initial = frappe.db.get_value(ADJUSTMENT, name, ["company", "employee"], as_dict=True)
	if not initial:
		frappe.throw(_("Wage adjustment does not exist."))
	require_adjustment_owner(initial.company)
	_lock_employee(initial.company, initial.employee)
	_lock_employee_summaries(initial.company, initial.employee)
	require_adjustment_owner(initial.company, for_update=True)
	doc = frappe.get_doc(ADJUSTMENT, name, for_update=True)
	if doc.company != initial.company or doc.employee != initial.employee:
		frappe.throw(_("Wage adjustment identity changed; reload and retry."))
	if doc.docstatus != 2:
		if doc.docstatus != 1:
			frappe.throw(_("Only a recorded wage adjustment can be cancelled."))
		if _money(_balances([doc], for_update=True)[doc.name]["applied_amount"]):
			frappe.throw(_("A wage adjustment used by a confirmed summary cannot be cancelled."))
		doc.flags.wage_adjustment_action = True
		doc.flags.ignore_permissions = True
		doc.cancel()
	return {"name": doc.name, "docstatus": doc.docstatus, "remaining_amount": 0.0}


def validate_adjustment_document(doc):
	if not getattr(doc.flags, "wage_adjustment_action", False):
		frappe.throw(_("Use the controlled wage adjustment action."))
	require_adjustment_owner(doc.company)
	values = _payload(
		doc.company,
		doc.employee,
		doc.month_start,
		doc.adjustment_type,
		doc.amount,
		doc.reason,
		doc.source_report,
	)
	employee = frappe.db.get_value("Employee", doc.employee, ["company", "employee_name"], as_dict=True)
	if not employee or employee.company != doc.company:
		frappe.throw(_("The employee must belong to the selected company."))
	if doc.source_report:
		report = frappe.db.get_value(
			"Job Card Work Report", doc.source_report, ["company", "employee", "status"], as_dict=True
		)
		if (
			not report
			or report.company != doc.company
			or report.employee != doc.employee
			or report.status != "Approved"
		):
			frappe.throw(_("The source report must be Approved and belong to this employee and company."))
	doc.update(values)
	doc.employee_name = employee.employee_name
	if not doc.is_new():
		old = doc.get_doc_before_save() or frappe.get_doc(ADJUSTMENT, doc.name)
		if not _same_payload(old, values) or old.request_id != doc.request_id:
			frappe.throw(_("Recorded wage adjustment facts are immutable."))


def before_submit_adjustment(doc):
	if not getattr(doc.flags, "wage_adjustment_action", False):
		frappe.throw(_("Use the controlled wage adjustment action."))


def before_cancel_adjustment(doc):
	if not getattr(doc.flags, "wage_adjustment_action", False):
		frappe.throw(_("Use the controlled wage adjustment action."))
	if _money(_balances([doc], for_update=True)[doc.name]["applied_amount"]):
		frappe.throw(_("A wage adjustment used by a confirmed summary cannot be cancelled."))


def _eligible_docs(company, employee, month_start, *, for_update=False):
	adjustment = frappe.qb.DocType(ADJUSTMENT)
	query = (
		frappe.qb.from_(adjustment)
		.select(adjustment.name)
		.where(
			(adjustment.company == company)
			& (adjustment.employee == employee)
			& (adjustment.docstatus == 1)
			& (adjustment.month_start <= get_first_day(month_start))
		)
		.orderby(adjustment.name)
	)
	if for_update:
		query = query.for_update()
	names = query.run(pluck=True)
	docs = [frappe.get_doc(ADJUSTMENT, name, for_update=for_update) for name in names]
	return sorted(docs, key=lambda doc: (getdate(doc.month_start), doc.creation, doc.name))


def adjustment_employees(company, month_start):
	names = frappe.get_all(
		ADJUSTMENT,
		filters={"company": company, "docstatus": 1, "month_start": ("<=", get_first_day(month_start))},
		pluck="name",
		order_by="name",
		limit=0,
	)
	docs = [frappe.get_doc(ADJUSTMENT, name) for name in names]
	balances = _balances(docs)
	return {doc.employee for doc in docs if _money(balances[doc.name]["remaining_amount"]) > 0}


def plan_applications(base_amount, records):
	"""Pure allocation: supplements first; deterministic input order for deductions."""
	available = _money(base_amount)
	if available < 0:
		frappe.throw(_("Base wages cannot be negative."))
	applications = []
	supplement = deduction = pending = Decimal("0")
	for record in records:
		if record["type"] == "Supplement":
			amount = _money(record["remaining_amount"])
			if amount > 0:
				applications.append(
					{
						"wage_adjustment": record["name"],
						"adjustment_type": "Supplement",
						"amount": float(amount),
					}
				)
				supplement += amount
	available += supplement
	for record in records:
		if record["type"] == "Deduction":
			remaining = _money(record["remaining_amount"])
			amount = min(remaining, available)
			if amount > 0:
				applications.append(
					{
						"wage_adjustment": record["name"],
						"adjustment_type": "Deduction",
						"amount": float(amount),
					}
				)
				deduction += amount
				available -= amount
			pending += remaining - amount
	return {
		"applications": applications,
		"supplement_amount": float(supplement),
		"deduction_amount": float(deduction),
		"pending_deduction_amount": float(pending),
		"total_amount": float(available),
	}


def summary_plan(doc, *, for_update=False):
	docs = _eligible_docs(doc.company, doc.employee, doc.month_start, for_update=for_update)
	balances = _balances(docs, exclude_summary=doc.name, for_update=for_update)
	records = [balances[item.name] for item in docs]
	plan = plan_applications(_money(doc.piecework_amount or 0) + _money(doc.time_amount or 0), records)
	plan["has_pending_records"] = any(_money(item["remaining_amount"]) > 0 for item in records)
	return plan


def set_summary_plan(doc):
	plan = summary_plan(doc, for_update=True)
	doc.set("adjustments", [])
	for row in plan["applications"]:
		doc.append("adjustments", row)
	for field in ("supplement_amount", "deduction_amount", "pending_deduction_amount", "total_amount"):
		doc.set(field, plan[field])
	return plan


def validate_summary_plan(doc):
	plan = summary_plan(doc, for_update=True)
	actual = []
	seen = set()
	for row in doc.get("adjustments") or []:
		if not row.wage_adjustment or row.wage_adjustment in seen:
			frappe.throw(_("Monthly wage adjustments must be unique."))
		seen.add(row.wage_adjustment)
		actual.append(
			{
				"wage_adjustment": row.wage_adjustment,
				"adjustment_type": row.adjustment_type,
				"amount": float(_money(row.amount, positive=True)),
			}
		)
	if actual != plan["applications"]:
		frappe.throw(
			_("Wage adjustment balances or plans changed; rebuild this monthly draft before confirming.")
		)
	for field in ("supplement_amount", "deduction_amount", "pending_deduction_amount"):
		if _money(doc.get(field) or 0) != _money(plan[field]):
			frappe.throw(
				_("Wage adjustment balances or plans changed; rebuild this monthly draft before confirming.")
			)
	doc.total_amount = plan["total_amount"]
	if (
		not doc.details
		and not plan["has_pending_records"]
		and not getattr(doc.flags, "clear_empty_wage_adjustment_draft", False)
	):
		frappe.throw(_("A monthly wage summary needs work reports or an outstanding wage adjustment."))
	return plan


def assert_confirmation_order(doc):
	docs = _eligible_docs(doc.company, doc.employee, doc.month_start, for_update=True)
	balances = _balances(docs, exclude_summary=doc.name, for_update=True)
	pending = [item for item in docs if _money(balances[item.name]["remaining_amount"]) > 0]
	if not pending:
		return
	latest = frappe.db.get_value(
		SUMMARY,
		{"company": doc.company, "employee": doc.employee, "docstatus": 1, "name": ("!=", doc.name)},
		"month_start",
		order_by="month_start desc",
		for_update=True,
	)
	if latest and getdate(latest) >= getdate(doc.month_start):
		frappe.throw(
			_("A later wage month is already confirmed; this older draft cannot consume adjustments.")
		)
	first_month = min(getdate(item.month_start) for item in pending)
	parent = frappe.qb.DocType(SUMMARY)
	earlier = (
		frappe.qb.from_(parent)
		.select(parent.name, parent.month_start)
		.where(
			(parent.company == doc.company)
			& (parent.employee == doc.employee)
			& (parent.docstatus == 0)
			& (parent.month_start >= first_month)
			& (parent.month_start < getdate(doc.month_start))
		)
		.for_update()
	).run(as_dict=True)
	if any(getdate(row.month_start) < getdate(doc.month_start) for row in earlier):
		frappe.throw(
			_("Confirm earlier monthly drafts before applying carried wage adjustments to this month.")
		)
	report = frappe.qb.DocType("Job Card Work Report")
	unclosed = (
		frappe.qb.from_(report)
		.select(report.name)
		.where(
			(report.company == doc.company)
			& (report.employee == doc.employee)
			& (report.labor_date >= first_month)
			& (report.labor_date < getdate(doc.month_start))
			& report.status.isin(["In Progress", "Paused", "Pending Approval", "Approved"])
			& ((report.monthly_summary.isnull()) | (report.monthly_summary == ""))
		)
		.limit(1)
		.for_update()
	).run()
	if unclosed:
		frappe.throw(
			_("Rebuild and confirm the earlier report month before consuming carried wage adjustments.")
		)
