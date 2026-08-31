from __future__ import annotations

from process_simplification.management_access import (
	CAPABILITY_PRODUCTION_REVIEW,
	CAPABILITY_WAGE_MANAGEMENT,
	SUPERVISOR_ROLE,
	WAGE_MANAGER_ROLE,
	roles_for_capability,
)


WORKER_ROLE = "Production Worker"
SYSTEM_MANAGER_ROLE = "System Manager"

REVIEW_ROLES = roles_for_capability(CAPABILITY_PRODUCTION_REVIEW)
ADMIN_REVIEW_ROLES = set(REVIEW_ROLES)
WAGE_ROLES = roles_for_capability(CAPABILITY_WAGE_MANAGEMENT)

ASSIGNMENT_STATUSES = {"Active", "Completed", "Cancelled"}
REPORT_STATUSES = {"In Progress", "Pending Approval", "Approved", "Rejected"}
WAGE_TYPES = {"Piecework", "Time"}
