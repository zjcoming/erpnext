"""Owner-only payroll quantity corrections; production facts stay immutable."""

from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, get_datetime, get_first_day, getdate, now_datetime

from process_simplification.production_reporting import wage_adjustments
from process_simplification.production_reporting.domain import money, wage_manager_companies

PAYROLL_FIELDS = [
	"payroll_qty_corrected",
	"payroll_qty",
	"payroll_correction_reason",
	"payroll_corrected_by",
	"payroll_corrected_at",
]
_STORED_FIELDS = [*PAYROLL_FIELDS, "payroll_correction_request_id"]
_ACTION = object()


def payroll_amount(report):
	if report.get("wage_type") == "Piecework" and cint(report.get("payroll_qty_corrected")):
		return money(flt(report.get("payroll_qty")) * flt(report.get("rate")))
	return flt(report.get("wage_amount"))


def _quantity(value):
	try:
		if isinstance(value, bool):
			raise ValueError
		qty = Decimal(str(value))
		if not qty.is_finite() or qty < 0 or qty > Decimal("999999999999.999999"):
			raise ValueError
		if qty != qty.quantize(Decimal("0.000001")):
			raise ValueError
		return float(qty)
	except (InvalidOperation, ValueError, TypeError):
		frappe.throw(_("核定计薪数量须为非负数字，最多保留六位小数。"))


def validate_report_fields(doc):
	"""Do not turn the new payroll fields into a general report editing bypass."""
	old = None if doc.is_new() else frappe.db.get_value(doc.doctype, doc.name, _STORED_FIELDS, as_dict=True)
	changed = any((doc.get(key) or None) != ((old or {}).get(key) or None) for key in _STORED_FIELDS)
	if not changed:
		return
	if doc.flags.get("payroll_quantity_action") is not _ACTION:
		frappe.throw(_("计薪数量只能通过老板核定入口修改。"))
	if doc.status != "Approved" or doc.wage_type != "Piecework":
		frappe.throw(_("只能核定已审核计件报工的计薪数量。"))
	_quantity(doc.payroll_qty)
	if not doc.payroll_qty_corrected or not doc.payroll_correction_reason:
		frappe.throw(_("请填写核定计薪数量及原因。"))


def annotate_reports(rows, *, reviewer=False):
	"""Expose the payroll overlay separately from original quantity and amount."""
	scope = set()
	if reviewer and wage_adjustments.can_record_adjustments():
		scope = wage_manager_companies(throw_if_empty=False)
	eligible = [
		row
		for row in rows
		if row.get("status") == "Approved"
		and row.get("wage_type") == "Piecework"
		and (scope is None or row.get("company") in scope)
	]
	confirmed = set()
	if eligible:
		confirmed = {
			(row.company, row.employee, getdate(row.month_start))
			for row in frappe.get_all(
				"Monthly Worker Wage Summary",
				filters={
					"docstatus": 1,
					"company": ("in", list({row.company for row in eligible})),
					"employee": ("in", list({row.employee for row in eligible})),
					"month_start": ("in", list({get_first_day(row.labor_date) for row in eligible})),
				},
				fields=["company", "employee", "month_start"],
				limit=0,
			)
		}
	for row in rows:
		row.payroll_amount = payroll_amount(row)
		row.can_correct_payroll_qty = False
		row.payroll_correction_block_message = ""
		if row in eligible:
			locked = (
				row.get("monthly_summary")
				or (row.company, row.employee, get_first_day(row.labor_date)) in confirmed
			)
			row.can_correct_payroll_qty = not bool(locked)
			if locked:
				row.payroll_correction_block_message = _("该月工资汇总已确认，不能再核定计薪数量。")
	return rows


def set_report_payroll_quantity(report, payroll_qty, reason, expected_modified, request_id):
	from process_simplification.production_reporting import summary

	wage_adjustments.require_adjustment_owner()
	qty = _quantity(payroll_qty)
	if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000:
		frappe.throw(_("请填写核定原因，最多 1000 字。"))
	reason = reason.strip()
	if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 140:
		frappe.throw(_("缺少有效的核定请求标识，请刷新后重试。"))
	if not expected_modified:
		frappe.throw(_("缺少报工版本，请刷新后重试。"))
	try:
		expected_modified = get_datetime(expected_modified)
	except (ValueError, TypeError, OverflowError):
		frappe.throw(_("报工版本无效，请刷新后重试。"))
	initial = frappe.db.get_value(
		"Job Card Work Report", report, ["company", "employee", "labor_date"], as_dict=True
	)
	if not initial:
		frappe.throw(_("报工记录不存在。"))
	wage_adjustments.require_adjustment_owner(initial.company)
	# Same employee -> summaries -> reports order as monthly confirmation.
	wage_adjustments._lock_employee(initial.company, initial.employee)
	summaries = wage_adjustments._lock_employee_summaries(initial.company, initial.employee)
	wage_adjustments.require_adjustment_owner(initial.company, for_update=True)
	doc = frappe.get_doc("Job Card Work Report", report, for_update=True)
	if (doc.company, doc.employee, getdate(doc.labor_date)) != (
		initial.company,
		initial.employee,
		getdate(initial.labor_date),
	):
		frappe.throw(_("报工归属已变化，请刷新后重试。"))
	if doc.payroll_correction_request_id == request_id:
		if flt(doc.payroll_qty, 6) != qty or doc.payroll_correction_reason != reason:
			frappe.throw(_("此核定请求已用于其他数量或原因，请刷新后重试。"))
		return {"report": annotate_reports([doc.as_dict()], reviewer=True)[0], "summaries": []}
	month = get_first_day(doc.labor_date)
	if doc.monthly_summary or any(
		row.docstatus == 1 and getdate(row.month_start) == month for row in summaries
	):
		frappe.throw(_("该月工资汇总已确认，不能再核定计薪数量。"))
	if doc.status != "Approved" or doc.wage_type != "Piecework":
		frappe.throw(_("只能核定已审核计件报工的计薪数量。"))
	if get_datetime(doc.modified) != expected_modified:
		frappe.throw(_("报工已被更新，请刷新后核对，避免覆盖他人的核定。"))
	old_qty = flt(doc.payroll_qty) if doc.payroll_qty_corrected else flt(doc.completed_qty)
	doc.payroll_qty_corrected = 1
	doc.payroll_qty = qty
	doc.payroll_correction_reason = reason
	doc.payroll_corrected_by = frappe.session.user
	doc.payroll_corrected_at = now_datetime()
	doc.payroll_correction_request_id = request_id
	if payroll_amount(doc) > 999999999999.99:
		frappe.throw(_("核定后的计薪金额过大，请核对数量和单价。"))
	doc.flags.worker_reporting_action = True
	doc.flags.payroll_quantity_action = _ACTION
	# Normal Document.save also rewrites every captured timer segment's audit
	# stamps. Validate the report, then persist only this payroll overlay.
	doc.load_doc_before_save()
	doc.run_method("validate")
	# Report validation refreshes this display name; a payroll-only write must
	# not claim that unrelated change in its response or Version audit.
	doc.employee_name = doc.get_doc_before_save().employee_name
	doc.modified = now_datetime()
	doc.modified_by = frappe.session.user
	frappe.db.set_value(
		doc.doctype,
		doc.name,
		{key: doc.get(key) for key in [*_STORED_FIELDS, "modified", "modified_by"]},
		update_modified=False,
	)
	doc.save_version()
	doc.clear_cache()
	doc.add_comment(
		"Comment",
		_("核定计薪数量：{0} → {1}；原报工数量：{2}。原因：{3}").format(
			old_qty, qty, flt(doc.completed_qty), escape_html(reason)
		),
	)
	updated = []
	if any(row.docstatus == 0 and getdate(row.month_start) == month for row in summaries):
		updated = summary.build_monthly_summaries(doc.company, month, doc.employee)["summaries"]
	return {"report": annotate_reports([doc.as_dict()], reviewer=True)[0], "summaries": updated}
