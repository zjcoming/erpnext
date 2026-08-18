from __future__ import annotations


WORKER_ROLE = "Production Worker"
SUPERVISOR_ROLE = "Production Supervisor"
WAGE_MANAGER_ROLE = "Production Wage Manager"
SYSTEM_MANAGER_ROLE = "System Manager"

REVIEW_ROLES = {SUPERVISOR_ROLE, SYSTEM_MANAGER_ROLE}
ADMIN_REVIEW_ROLES = {SYSTEM_MANAGER_ROLE}
WAGE_ROLES = {WAGE_MANAGER_ROLE, SYSTEM_MANAGER_ROLE}

ASSIGNMENT_STATUSES = {"Active", "Completed", "Cancelled"}
REPORT_STATUSES = {"Pending Approval", "Approved", "Rejected"}
WAGE_TYPES = {"Piecework", "Time"}
