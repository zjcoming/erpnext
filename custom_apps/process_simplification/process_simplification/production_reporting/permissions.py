from __future__ import annotations

import frappe

from process_simplification.production_reporting.constants import (
	ADMIN_REVIEW_ROLES,
	WAGE_ROLES,
)
from process_simplification.production_reporting.domain import reviewer_companies, wage_manager_companies


def _roles(user: str | None = None) -> set[str]:
	return set(frappe.get_roles(user or frappe.session.user))


def _is_admin_reviewer(user: str | None = None) -> bool:
	return bool(_roles(user).intersection(ADMIN_REVIEW_ROLES))


def _is_wage_manager(user: str | None = None) -> bool:
	return bool(_roles(user).intersection(WAGE_ROLES))


def _wage_scope(user: str) -> set[str] | None:
	return wage_manager_companies(user, throw_if_empty=False)


def _company_condition(table: str, companies: set[str]) -> str:
	if not companies:
		return "1 = 0"
	values = ", ".join(frappe.db.escape(company) for company in sorted(companies))
	return f"`tab{table}`.`company` in ({values})"


def _review_scope(user: str) -> set[str] | None:
	return reviewer_companies(user, throw_if_empty=False)


def _company_allowed(doc, companies: set[str] | None) -> bool:
	if companies is None:
		return True
	if not doc or not getattr(doc, "company", None):
		return bool(companies)
	return doc.company in companies


def _is_read_permission(permission_type: str | None) -> bool:
	return (permission_type or "read") in {"read", "select", "report", "export", "print", "email"}


def assignment_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if _is_admin_reviewer(user):
		companies = _review_scope(user)
		return "" if companies is None else _company_condition("Job Card Worker Assignment", companies)
	return "1 = 0"


def report_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	clauses = []
	if _is_admin_reviewer(user):
		companies = _review_scope(user)
		if companies is None:
			return ""
		if companies:
			clauses.append(_company_condition("Job Card Work Report", companies))
	if _is_wage_manager(user):
		companies = _wage_scope(user)
		if companies is None:
			return ""
		if companies:
			clauses.append(_company_condition("Job Card Work Report", companies))
	return f"({' or '.join(clauses)})" if clauses else "1 = 0"


def _production_reference_query(table: str, assignment_field: str, user: str | None = None) -> str:
	user = user or frappe.session.user
	if not _is_admin_reviewer(user):
		return ""
	companies = _review_scope(user)
	return "" if companies is None else _company_condition(table, companies)


def job_card_query(user: str | None = None) -> str:
	return _production_reference_query("Job Card", "job_card", user)


def work_order_query(user: str | None = None) -> str:
	return _production_reference_query("Work Order", "work_order", user)


def summary_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if not _is_wage_manager(user):
		return "1 = 0"
	companies = _wage_scope(user)
	return "" if companies is None else _company_condition("Monthly Worker Wage Summary", companies)


def wage_rate_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if not _is_wage_manager(user):
		return "1 = 0"
	companies = _wage_scope(user)
	return "" if companies is None else _company_condition("Operation Wage Rate", companies)


def assignment_permission(doc, ptype: str | None = None, user: str | None = None, debug=False) -> bool:
	if not _is_read_permission(ptype):
		return False
	user = user or frappe.session.user
	return _is_admin_reviewer(user) and _company_allowed(doc, _review_scope(user))


def report_permission(doc, ptype: str | None = None, user: str | None = None, debug=False) -> bool:
	if not _is_read_permission(ptype):
		return False
	user = user or frappe.session.user
	if _is_admin_reviewer(user):
		if _company_allowed(doc, _review_scope(user)):
			return True
	if _is_wage_manager(user):
		companies = _wage_scope(user)
		if companies is None:
			return True
		if not doc or not getattr(doc, "company", None):
			return bool(companies)
		if doc.company in companies:
			return True
	return False


def _production_reference_permission(
	doc,
	assignment_field: str,
	ptype: str | None = None,
	user: str | None = None,
) -> bool:
	user = user or frappe.session.user
	if not _is_read_permission(ptype) or not _is_admin_reviewer(user):
		return True
	return _company_allowed(doc, _review_scope(user))


def job_card_permission(doc, ptype: str | None = None, user: str | None = None, debug=False) -> bool:
	return _production_reference_permission(doc, "job_card", ptype, user)


def work_order_permission(doc, ptype: str | None = None, user: str | None = None, debug=False) -> bool:
	return _production_reference_permission(doc, "work_order", ptype, user)


def summary_permission(doc, ptype: str | None = None, user: str | None = None, debug=False) -> bool:
	if not _is_read_permission(ptype):
		return False
	user = user or frappe.session.user
	if not _is_wage_manager(user):
		return False
	companies = _wage_scope(user)
	if companies is None:
		return True
	if not doc or not getattr(doc, "company", None):
		return bool(companies)
	return doc.company in companies


def wage_rate_permission(doc, ptype: str | None = None, user: str | None = None, debug=False) -> bool:
	if (ptype or "read") in {"delete", "submit", "cancel", "amend"}:
		return False
	user = user or frappe.session.user
	if not _is_wage_manager(user):
		return False
	companies = _wage_scope(user)
	if companies is None:
		return True
	if not doc or not getattr(doc, "company", None):
		return bool(companies)
	return doc.company in companies
