from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import cint

from process_simplification.production_reporting.constants import (
	REVIEW_ROLES,
	WAGE_MANAGER_ROLE,
	WORKER_ROLE,
)


def ensure_worker_reporting_roles():
	for role_name in (WORKER_ROLE, WAGE_MANAGER_ROLE):
		if not frappe.db.exists("Role", role_name):
			frappe.get_doc(
				{
					"doctype": "Role",
					"role_name": role_name,
					"desk_access": 1,
				}
			).insert(ignore_permissions=True)


def ensure_worker_reporting_reference_permissions():
	"""Give reporting reviewers read-only entry to linked production documents."""
	from frappe.permissions import add_permission

	for doctype in ("Job Card", "Work Order"):
		for role in sorted(REVIEW_ROLES - {"System Manager"}):
			permission = frappe.db.get_value(
				"Custom DocPerm",
				{
					"parent": doctype,
					"role": role,
					"permlevel": 0,
					"if_owner": 0,
				},
			)
			if not permission:
				add_permission(doctype, role, permlevel=0, ptype="read")
				permission = frappe.db.get_value(
					"Custom DocPerm",
					{
						"parent": doctype,
						"role": role,
						"permlevel": 0,
						"if_owner": 0,
					},
				)
			if permission and not frappe.db.get_value("Custom DocPerm", permission, "read"):
				frappe.db.set_value("Custom DocPerm", permission, "read", 1, update_modified=False)
		frappe.clear_cache(doctype=doctype)


def ensure_worker_reporting_reference_fields():
	"""Allow reviewers to read worker links inside their assignment-scoped Job Cards."""
	from frappe.custom.doctype.property_setter.property_setter import make_property_setter

	setter = frappe.db.get_value(
		"Property Setter",
		{
			"doc_type": "Job Card Time Log",
			"field_name": "employee",
			"property": "ignore_user_permissions",
		},
	)
	if setter:
		if str(frappe.db.get_value("Property Setter", setter, "value") or "0") != "1":
			frappe.db.set_value("Property Setter", setter, "value", "1", update_modified=False)
	else:
		make_property_setter(
			"Job Card Time Log",
			"employee",
			"ignore_user_permissions",
			1,
			"Check",
		)
	frappe.clear_cache(doctype="Job Card Time Log")


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
			(["status", "reviewed_at", "name"], "jcwr_review_status_time"),
			(["assignment", "status", "reviewed_at", "name"], "jcwr_assignment_review_time"),
			(["company", "employee", "labor_date", "status"], "jcwr_month_scope"),
		],
		"Operation Wage Rate": [
			(["operation", "company", "enabled", "valid_from", "valid_to"], "owr_active_scope"),
		],
		"Monthly Worker Wage Summary": [
			(["company", "employee", "month_start", "docstatus"], "mwws_employee_month"),
		],
		"Production Exception Request": [
			(["employee", "requested_at", "status", "name"], "per_worker_time_status"),
			(["supervisor", "status", "requested_at", "name"], "per_review_queue"),
			(["work_order", "item_code", "source_warehouse", "status"], "per_material_reservation"),
			(["job_card", "request_type", "status", "name"], "per_job_loss_status"),
		],
	}
	for doctype, definitions in indexes.items():
		if not frappe.db.table_exists(doctype):
			continue
		for fields, index_name in definitions:
			frappe.db.add_index(doctype, fields, index_name)


def backfill_monthly_summary_display_fields():
	if not (
		frappe.db.table_exists("Monthly Worker Wage Summary")
		and frappe.db.has_column("Monthly Worker Wage Summary", "employee_name")
	):
		return
	frappe.db.sql(
		"""
		update `tabMonthly Worker Wage Summary` summary
		inner join `tabEmployee` employee on employee.name = summary.employee
		set summary.employee_name = employee.employee_name
		where ifnull(summary.employee_name, '') != ifnull(employee.employee_name, '')
		"""
	)
	if frappe.db.has_column("Monthly Worker Wage Summary", "wage_month"):
		frappe.db.sql(
			"""
			update `tabMonthly Worker Wage Summary`
			set wage_month = concat(year(month_start), '年', lpad(month(month_start), 2, '0'), '月')
			where month_start is not null
			  and ifnull(wage_month, '') != concat(year(month_start), '年', lpad(month(month_start), 2, '0'), '月')
			"""
		)


def backfill_work_report_employee_names():
	if not (
		frappe.db.table_exists("Job Card Work Report")
		and frappe.db.has_column("Job Card Work Report", "employee_name")
	):
		return
	frappe.db.sql(
		"""
		update `tabJob Card Work Report` report
		inner join `tabEmployee` employee on employee.name = report.employee
		set report.employee_name = employee.employee_name
		where ifnull(report.employee_name, '') != ifnull(employee.employee_name, '')
		"""
	)


def release_cancelled_monthly_summary_keys():
	if not (
		frappe.db.table_exists("Monthly Worker Wage Summary")
		and frappe.db.has_column("Monthly Worker Wage Summary", "summary_key")
	):
		return
	frappe.db.sql(
		"""
		update `tabMonthly Worker Wage Summary`
		set summary_key = null
		where docstatus = 2 and summary_key is not null
		"""
	)


def backfill_operation_wage_rate_modes():
	"""Upgrade the original one-method rule without changing its effective dates."""
	required_columns = {
		"wage_type",
		"rate",
		"enable_piecework",
		"piecework_rate",
		"enable_time",
		"hourly_rate",
	}
	if not frappe.db.table_exists("Operation Wage Rate") or not all(
		frappe.db.has_column("Operation Wage Rate", fieldname)
		for fieldname in required_columns
	):
		return
	frappe.db.sql(
		"""
		update `tabOperation Wage Rate`
		set enable_piecework = case when wage_type = 'Piecework' then 1 else 0 end,
			piecework_rate = case when wage_type = 'Piecework' then rate else 0 end,
			enable_time = case when wage_type = 'Time' then 1 else 0 end,
			hourly_rate = case when wage_type = 'Time' then rate else 0 end
		where wage_type in ('Piecework', 'Time')
		  and ifnull(rate, 0) > 0
		  and ifnull(piecework_rate, 0) = 0
		  and ifnull(hourly_rate, 0) = 0
		"""
	)


def backfill_work_report_wage_option_snapshots():
	"""Keep old reports frozen and recover safe dual choices for active sessions."""
	required_columns = {
		"wage_type",
		"manual_time_entry",
		"rate",
		"piecework_rate_snapshot",
		"hourly_rate_snapshot",
		"time_manual_entry_snapshot",
	}
	if not frappe.db.table_exists("Job Card Work Report") or not all(
		frappe.db.has_column("Job Card Work Report", fieldname)
		for fieldname in required_columns
	):
		return
	frappe.db.sql(
		"""
		update `tabJob Card Work Report`
		set piecework_rate_snapshot = case
				when wage_type = 'Piecework' then rate else 0 end,
			hourly_rate_snapshot = case
				when wage_type = 'Time' then rate else 0 end,
			time_manual_entry_snapshot = case
				when wage_type = 'Time' then manual_time_entry else 0 end
		where wage_type in ('Piecework', 'Time')
		  and ifnull(rate, 0) > 0
		  and ifnull(piecework_rate_snapshot, 0) = 0
		  and ifnull(hourly_rate_snapshot, 0) = 0
		"""
	)

	# The short-lived start-time selection implementation stored only the chosen
	# method. An active report can safely regain both options only while its linked
	# rule is still at exactly the revision captured when the timer started.
	manual_entry_default = cint(
		frappe.db.get_single_value(
			"Process Simplification Settings", "allow_manual_time_entry"
		)
	)
	frappe.db.sql(
		"""
		update `tabJob Card Work Report` report
		inner join `tabOperation Wage Rate` wage_rate
			on wage_rate.name = report.wage_rate
		set report.piecework_rate_snapshot = wage_rate.piecework_rate,
			report.hourly_rate_snapshot = wage_rate.hourly_rate,
			report.time_manual_entry_snapshot = %s,
			report.manual_time_entry = case
				when report.wage_type = 'Time' then %s else 0 end
		where report.status = 'In Progress'
		  and report.wage_rate_revision = wage_rate.revision
		  and wage_rate.enable_piecework = 1
		  and ifnull(wage_rate.piecework_rate, 0) > 0
		  and wage_rate.enable_time = 1
		  and ifnull(wage_rate.hourly_rate, 0) > 0
		""",
		(manual_entry_default, manual_entry_default),
	)


def ensure_process_simplification_settings_defaults():
	if not frappe.db.exists("DocType", "Process Simplification Settings"):
		return
	if frappe.db.get_single_value(
		"Process Simplification Settings", "allow_manual_time_entry"
	) in (None, ""):
		frappe.db.set_single_value(
			"Process Simplification Settings",
			"allow_manual_time_entry",
			1,
			update_modified=False,
		)


def setup_worker_reporting():
	ensure_worker_reporting_roles()
	ensure_worker_reporting_reference_permissions()
	ensure_process_simplification_settings_defaults()
	backfill_operation_wage_rate_modes()
	backfill_work_report_wage_option_snapshots()

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
					"fieldname": "custom_job_card_work_report_segment",
					"label": "Job Card Work Report Segment",
					"fieldtype": "Link",
					"options": "Job Card Work Report",
					"read_only": 1,
					"no_copy": 1,
					"insert_after": "custom_job_card_work_report",
				},
				{
					"fieldname": "custom_reported_employee",
					"label": "Reported Employee",
					"fieldtype": "Link",
					"options": "Employee",
					"read_only": 1,
					"no_copy": 1,
					"in_list_view": 1,
					"ignore_user_permissions": 1,
					"insert_after": "custom_job_card_work_report_segment",
				},
			],
			"Stock Entry": [
				{
					"fieldname": "custom_production_exception_request",
					"label": "Production Exception Request",
					"fieldtype": "Link",
					"options": "Production Exception Request",
					"read_only": 1,
					"unique": 0,
					"search_index": 1,
					"no_copy": 1,
					"insert_after": "work_order",
					"description": "Approved worker return or scrap request that created this Stock Entry.",
				},
			],
		},
		update=True,
	)
	ensure_worker_reporting_reference_fields()
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
	backfill_monthly_summary_display_fields()
	backfill_work_report_employee_names()
	release_cancelled_monthly_summary_keys()
	ensure_worker_reporting_indexes()
	frappe.clear_cache(doctype="Work Order")
	frappe.clear_cache(doctype="Job Card")
	frappe.clear_cache(doctype="Job Card Time Log")
	frappe.clear_cache(doctype="Stock Entry")
