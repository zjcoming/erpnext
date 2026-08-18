from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from process_simplification.production_reporting.constants import (
	SUPERVISOR_ROLE,
	WAGE_MANAGER_ROLE,
	WORKER_ROLE,
)


def ensure_worker_reporting_roles():
	for role_name in (WORKER_ROLE, SUPERVISOR_ROLE, WAGE_MANAGER_ROLE):
		if not frappe.db.exists("Role", role_name):
			frappe.get_doc(
				{
					"doctype": "Role",
					"role_name": role_name,
					"desk_access": 1,
				}
			).insert(ignore_permissions=True)


def ensure_worker_reporting_indexes():
	indexes = {
		"Job Card Worker Assignment": [
			(["job_card", "status", "name"], "jcwa_job_status_name"),
			(["work_order", "status", "name"], "jcwa_work_order_status"),
		],
		"Job Card Work Report": [
			(["job_card", "status", "name"], "jcwr_job_status_name"),
			(["employee", "labor_date", "status", "name"], "jcwr_employee_day_status"),
			(["assignment", "status", "name"], "jcwr_assignment_status"),
			(["company", "employee", "labor_date", "status"], "jcwr_month_scope"),
		],
		"Operation Wage Rate": [
			(["operation", "company", "enabled", "valid_from", "valid_to"], "owr_active_scope"),
		],
		"Monthly Worker Wage Summary": [
			(["company", "employee", "month_start", "docstatus"], "mwws_employee_month"),
		],
	}
	for doctype, definitions in indexes.items():
		if not frappe.db.table_exists(doctype):
			continue
		for fields, index_name in definitions:
			frappe.db.add_index(doctype, fields, index_name)


def setup_worker_reporting():
	ensure_worker_reporting_roles()

	create_custom_fields(
		{
			"Work Order": [
				{
					"fieldname": "custom_worker_reporting_enabled",
					"label": "Worker Reporting History",
					"fieldtype": "Check",
					"read_only": 1,
					"allow_on_submit": 0,
					"no_copy": 1,
					"insert_after": "status",
					"description": (
						"This Work Order has worker-reporting assignments or permanent report history."
					),
				},
			],
			"Job Card": [
				{
					"fieldname": "custom_worker_reporting_enabled",
					"label": "Worker Reporting Enabled",
					"fieldtype": "Check",
					"read_only": 1,
					"no_copy": 1,
					"insert_after": "total_completed_qty",
					"description": "Completion quantity is accepted only by approved Job Card Work Reports.",
				},
				{
					"fieldname": "custom_worker_reporting_supervisor",
					"label": "Reporting Supervisor",
					"fieldtype": "Link",
					"options": "User",
					"read_only": 1,
					"no_copy": 1,
					"insert_after": "custom_worker_reporting_enabled",
				},
			],
			"Job Card Time Log": [
				{
					"fieldname": "custom_job_card_work_report",
					"label": "Job Card Work Report",
					"fieldtype": "Link",
					"options": "Job Card Work Report",
					"read_only": 1,
					"unique": 1,
					"no_copy": 1,
					"insert_after": "completed_qty",
				},
				{
					"fieldname": "custom_reported_employee",
					"label": "Reported Employee",
					"fieldtype": "Link",
					"options": "Employee",
					"read_only": 1,
					"no_copy": 1,
					"in_list_view": 1,
					"insert_after": "custom_job_card_work_report",
				},
			],
		},
		update=True,
	)
	if frappe.db.table_exists("Job Card Worker Assignment"):
		# The parent marker makes cancellation fail closed without taking Job Card
		# locks from a before_cancel hook that already owns the Work Order lock.
		frappe.db.sql(
			"""
			update `tabWork Order` work_order
			set work_order.custom_worker_reporting_enabled = 1
			where ifnull(work_order.custom_worker_reporting_enabled, 0) = 0
			  and exists (
				select 1
				from `tabJob Card Worker Assignment` assignment
				where assignment.work_order = work_order.name
			  )
			"""
		)
	ensure_worker_reporting_indexes()
	frappe.clear_cache(doctype="Work Order")
	frappe.clear_cache(doctype="Job Card")
	frappe.clear_cache(doctype="Job Card Time Log")
