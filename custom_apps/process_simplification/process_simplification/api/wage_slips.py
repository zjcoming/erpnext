"""Read-only, company-scoped printing of saved production wage summaries."""

from __future__ import annotations

import re
from datetime import date

import frappe
from frappe import _

from process_simplification.production_reporting.domain import require_wage_manager

SUMMARY_DOCTYPE = "Monthly Worker Wage Summary"
SLIPS_PER_PAGE = 5


def _month_filters(company, month_start):
	if not isinstance(company, str) or not company.strip():
		frappe.throw(_("请选择公司。"))
	require_wage_manager(company)
	if not isinstance(month_start, str) or not re.fullmatch(r"\d{4}-\d{2}-01", month_start):
		frappe.throw(_("请选择有效的工资月份。"))
	try:
		month = date.fromisoformat(month_start)
	except (ValueError, TypeError):
		frappe.throw(_("请选择有效的工资月份。"))
	return {"company": company, "month_start": month, "docstatus": ("<", 2)}


@frappe.whitelist()
def get_print_periods():
	"""Offer every saved month, including history older than the list's shortcuts."""
	companies = require_wage_manager()
	filters = {"docstatus": ("<", 2)}
	if companies is not None:
		filters["company"] = ("in", sorted(companies))
	return frappe.get_list(
		SUMMARY_DOCTYPE,
		filters=filters,
		fields=["company", "month_start"],
		group_by="company, month_start",
		order_by="month_start desc, company asc",
		limit_page_length=0,
	)


@frappe.whitelist()
def get_print_employees(company, month_start):
	return frappe.get_list(
		SUMMARY_DOCTYPE,
		filters=_month_filters(company, month_start),
		fields=["name", "employee", "employee_name", "docstatus"],
		order_by="employee_name asc, employee asc, name asc",
		limit_page_length=0,
	)


def _selected_names(summary_names):
	# None means all; an empty or malformed selection must never become all employees.
	if summary_names is None:
		return None
	if isinstance(summary_names, str):
		try:
			summary_names = frappe.parse_json(summary_names)
		except (ValueError, TypeError):
			frappe.throw(_("请选择需要打印的员工。"))
	if (
		not isinstance(summary_names, list)
		or not summary_names
		or any(not isinstance(name, str) or not name.strip() for name in summary_names)
	):
		frappe.throw(_("请选择需要打印的员工。"))
	return sorted(set(summary_names))


@frappe.whitelist()
def get_print_html(company, month_start, summary_names=None):
	filters = _month_filters(company, month_start)
	names = _selected_names(summary_names)
	if names is not None:
		filters["name"] = ("in", names)
	# Native read permissions and our summary query hook both apply. Never use
	# get_all here: one company/month may contain more than one list page of wages.
	rows = frappe.get_list(
		SUMMARY_DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"employee",
			"employee_name",
			"docstatus",
			"piecework_amount",
			"time_amount",
			"total_amount",
		],
		order_by="employee_name asc, employee asc, name asc",
		limit_page_length=0,
	)
	if names is not None and {row.name for row in rows} != set(names):
		frappe.throw(
			_("部分汇总已取消、不在所选公司及月份内，或您没有读取权限，请刷新后重试。"),
			frappe.PermissionError,
		)
	if not rows:
		frappe.throw(_("所选月份没有可打印的工资汇总，请先生成月度汇总。"))
	company_info = frappe.get_cached_value(
		"Company", company, ["company_name", "default_currency"], as_dict=True
	)
	month = filters["month_start"]
	return {
		"count": len(rows),
		"html": frappe.render_template(
			"templates/print/monthly_wage_slips.html",
			{
				"company_name": company_info.company_name or company,
				"currency": company_info.default_currency,
				"wage_month": f"{month.year}年{month.month:02d}月",
				"pages": [
					rows[index : index + SLIPS_PER_PAGE] for index in range(0, len(rows), SLIPS_PER_PAGE)
				],
				"count": len(rows),
			},
		),
	}
