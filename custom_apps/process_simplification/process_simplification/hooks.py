app_name = "process_simplification"
app_title = "流程简化"
app_publisher = "Custom ERPNext Implementation"
app_description = "Simplified manufacturing workflow for small factories"
app_email = "admin@example.com"
app_license = "GPL-3.0"

required_apps = ["erpnext"]

app_include_css = "/assets/process_simplification/css/process_simplification.css?v=8"

after_install = "process_simplification.install.after_install"
after_migrate = "process_simplification.install.after_migrate"

extend_doctype_class = {
	"Work Order": "process_simplification.production_reporting.work_order.WorkerReportingWorkOrderMixin",
	"Stock Entry": "process_simplification.production_reporting.stock_entry.SubassemblyReservationStockEntryMixin",
}

doc_events = {
	"Job Card": {
		"before_save": "process_simplification.production_reporting.job_card.before_save",
		"before_submit": "process_simplification.production_reporting.job_card.before_submit",
		"on_submit": "process_simplification.production_reporting.job_card.on_submit",
		"before_cancel": "process_simplification.production_reporting.job_card.before_cancel",
		"before_discard": "process_simplification.production_reporting.job_card.before_discard",
		"before_update_after_submit": "process_simplification.production_reporting.job_card.before_update_after_submit",
	},
	"Stock Entry": {
		"before_submit": "process_simplification.production_reporting.stock_entry.before_submit",
	},
}

permission_query_conditions = {
	"Job Card Worker Assignment": "process_simplification.production_reporting.permissions.assignment_query",
	"Job Card Work Report": "process_simplification.production_reporting.permissions.report_query",
	"Operation Wage Rate": "process_simplification.production_reporting.permissions.wage_rate_query",
	"Monthly Worker Wage Summary": "process_simplification.production_reporting.permissions.summary_query",
}

has_permission = {
	"Job Card Worker Assignment": "process_simplification.production_reporting.permissions.assignment_permission",
	"Job Card Work Report": "process_simplification.production_reporting.permissions.report_permission",
	"Operation Wage Rate": "process_simplification.production_reporting.permissions.wage_rate_permission",
	"Monthly Worker Wage Summary": "process_simplification.production_reporting.permissions.summary_permission",
}

scheduler_events = {
	"daily": ["process_simplification.api.quick_order.cleanup_expired_quick_order_idempotency"]
}

add_to_apps_screen = [
	{
		"name": app_name,
		"logo": "/assets/process_simplification/images/process-simplification.svg",
		"title": app_title,
		"route": "/desk/process-simplification",
		"has_permission": "process_simplification.permissions.can_access_app",
	}
]
