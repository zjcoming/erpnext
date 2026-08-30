from __future__ import annotations

from process_simplification.management_access import (
	OWNER_ROLE,
	PRODUCTION_MANAGER_ROLE,
	SUPERVISOR_ROLE,
	WAGE_MANAGER_ROLE,
)


WORKER_ROLE = "Production Worker"
SYSTEM_MANAGER_ROLE = "System Manager"

REVIEW_ROLES = {OWNER_ROLE, SUPERVISOR_ROLE, PRODUCTION_MANAGER_ROLE, SYSTEM_MANAGER_ROLE}
ADMIN_REVIEW_ROLES = {OWNER_ROLE, PRODUCTION_MANAGER_ROLE, SYSTEM_MANAGER_ROLE}
WAGE_ROLES = {OWNER_ROLE, WAGE_MANAGER_ROLE, SYSTEM_MANAGER_ROLE}

ASSIGNMENT_STATUSES = {"Active", "Completed", "Cancelled"}
REPORT_STATUSES = {"In Progress", "Pending Approval", "Approved", "Rejected"}
WAGE_TYPES = {"Piecework", "Time"}
