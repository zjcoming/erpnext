from __future__ import annotations

import frappe

from process_simplification.management_access import WAREHOUSE_OPERATOR_ROLE
from process_simplification.production_exceptions.constants import (
	APPROVED,
	AWAITING_STOCK_ENTRY,
	COMPLETED,
	MATERIAL_REQUEST_TYPES,
)
from process_simplification.production_reporting.constants import (
	ADMIN_REVIEW_ROLES,
	SUPERVISOR_ROLE,
)


def _roles(user: str) -> set[str]:
	if user == "Administrator":
		return {*ADMIN_REVIEW_ROLES, "System Manager"}
	return set(frappe.get_roles(user))


def _is_read(ptype: str | None) -> bool:
	return (ptype or "read") in {"read", "select", "report", "export", "print", "email"}


def request_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	roles = _roles(user)
	if roles.intersection(ADMIN_REVIEW_ROLES):
		return ""
	clauses = []
	if SUPERVISOR_ROLE in roles:
		clauses.append(
			f"`tabProduction Exception Request`.`supervisor` = {frappe.db.escape(user)}"
		)
	if roles.intersection({WAREHOUSE_OPERATOR_ROLE, "Stock User", "Stock Manager"}):
		types = ", ".join(frappe.db.escape(value) for value in sorted(MATERIAL_REQUEST_TYPES))
		statuses = ", ".join(
			frappe.db.escape(value) for value in sorted({APPROVED, AWAITING_STOCK_ENTRY, COMPLETED})
		)
		clauses.append(
			f"(`tabProduction Exception Request`.`request_type` in ({types}) "
			f"and `tabProduction Exception Request`.`status` in ({statuses}))"
		)
	return f"({' or '.join(clauses)})" if clauses else "1 = 0"


def request_permission(doc, ptype: str | None = None, user: str | None = None, debug=False) -> bool:
	if not _is_read(ptype):
		return False
	user = user or frappe.session.user
	roles = _roles(user)
	if roles.intersection(ADMIN_REVIEW_ROLES):
		return True
	if SUPERVISOR_ROLE in roles and (not doc or doc.supervisor == user):
		return True
	return bool(
		roles.intersection({WAREHOUSE_OPERATOR_ROLE, "Stock User", "Stock Manager"})
		and doc
		and doc.request_type in MATERIAL_REQUEST_TYPES
		and doc.status in {APPROVED, AWAITING_STOCK_ENTRY, COMPLETED}
	)
