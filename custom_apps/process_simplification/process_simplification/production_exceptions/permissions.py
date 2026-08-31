from __future__ import annotations

import frappe

from process_simplification.management_access import WAREHOUSE_OPERATOR_ROLE, user_company_scope
from process_simplification.production_exceptions.constants import (
	APPROVED,
	AWAITING_STOCK_ENTRY,
	COMPLETED,
	MATERIAL_REQUEST_TYPES,
)
from process_simplification.production_reporting.constants import (
	ADMIN_REVIEW_ROLES,
)


def _roles(user: str) -> set[str]:
	if user == "Administrator":
		return {*ADMIN_REVIEW_ROLES, "System Manager"}
	return set(frappe.get_roles(user))


def _is_read(ptype: str | None) -> bool:
	return (ptype or "read") in {"read", "select", "report", "export", "print", "email"}


def _company_condition(companies: set[str]) -> str:
	if not companies:
		return "1 = 0"
	values = ", ".join(frappe.db.escape(company) for company in sorted(companies))
	return f"`tabProduction Exception Request`.`company` in ({values})"


def _company_allowed(doc, companies: set[str] | None) -> bool:
	if companies is None:
		return True
	return bool(doc and getattr(doc, "company", None) in companies)


def request_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	roles = _roles(user)
	companies = user_company_scope(user)
	clauses = []
	if roles.intersection(ADMIN_REVIEW_ROLES):
		if companies is None:
			return ""
		if companies:
			clauses.append(_company_condition(companies))
	if roles.intersection({WAREHOUSE_OPERATOR_ROLE, "Stock User", "Stock Manager"}):
		types = ", ".join(frappe.db.escape(value) for value in sorted(MATERIAL_REQUEST_TYPES))
		statuses = ", ".join(
			frappe.db.escape(value) for value in sorted({APPROVED, AWAITING_STOCK_ENTRY, COMPLETED})
		)
		stock_condition = (
			f"(`tabProduction Exception Request`.`request_type` in ({types}) "
			f"and `tabProduction Exception Request`.`status` in ({statuses}))"
		)
		if companies is not None:
			stock_condition = f"({stock_condition} and {_company_condition(companies)})"
		clauses.append(stock_condition)
	return f"({' or '.join(clauses)})" if clauses else "1 = 0"


def request_permission(doc, ptype: str | None = None, user: str | None = None, debug=False) -> bool:
	if not _is_read(ptype):
		return False
	user = user or frappe.session.user
	roles = _roles(user)
	companies = user_company_scope(user)
	if roles.intersection(ADMIN_REVIEW_ROLES) and _company_allowed(doc, companies):
		return True
	return bool(
		roles.intersection({WAREHOUSE_OPERATOR_ROLE, "Stock User", "Stock Manager"})
		and _company_allowed(doc, companies)
		and doc
		and doc.request_type in MATERIAL_REQUEST_TYPES
		and doc.status in {APPROVED, AWAITING_STOCK_ENTRY, COMPLETED}
	)
