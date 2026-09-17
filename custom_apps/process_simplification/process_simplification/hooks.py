app_name = "process_simplification"
app_title = "恒算 ERP"
app_publisher = "重庆恒算科技有限公司"
app_description = "恒算 ERP 企业经营管理系统"
app_email = "contact@hengsuankeji.com"
app_license = "GPL-3.0"
app_logo_url = "/assets/process_simplification/images/hengsuan.svg"

website_context = {"favicon": app_logo_url}
update_website_context = "process_simplification.branding.update_website_context"
web_include_css = ["/assets/process_simplification/css/hengsuan_branding.css?v=2"]

required_apps = ["erpnext"]

app_include_css = [
	"/assets/process_simplification/css/process_simplification.css?v=19",
	"/assets/process_simplification/css/process_ui.css?v=14",
	"/assets/process_simplification/css/executive_dashboard.css?v=2",
	"/assets/process_simplification/css/purchasing.css?v=6",
	"/assets/process_simplification/css/warehouse.css?v=3",
	"/assets/process_simplification/css/process_pwa.css?v=1",
	"/assets/process_simplification/css/hengsuan_branding.css?v=2",
	"/assets/process_simplification/css/document_scan.css?v=7",
]
app_include_js = [
	"/assets/process_simplification/js/role_landing.js?v=1",
	"/assets/process_simplification/js/item_identity.js?v=4",
	"/assets/process_simplification/js/batch_quick_entry.js?v=1",
	"/assets/process_simplification/js/worker_assignment.js?v=14",
	"/assets/process_simplification/js/worker_reporting.js?v=17",
	"/assets/process_simplification/js/material_handling.js?v=3",
	"/assets/process_simplification/js/document_scan.js?v=13",
	"/assets/process_simplification/js/notification_sound.js?v=7",
	"/assets/process_simplification/js/notification_sync.js?v=1",
	"/assets/process_simplification/js/process_pwa.js?v=1",
	"/assets/process_simplification/js/hengsuan_branding.js?v=3",
	"/assets/process_simplification/js/page_refresh.js?v=9",
]

after_request = ["process_simplification.page_refresh.after_request"]

# v1 REST uses the pinned framework entry hook; Desk retains native savedocs.
sales_order_draft_creation = "process_simplification.sales_order_creation.create_rest_draft"
override_whitelisted_methods = {
	"frappe.desk.form.save.savedocs": "process_simplification.sales_order_creation.savedocs",
	"erpnext.controllers.accounts_controller.get_missing_company_details": "process_simplification.print_localization.skip_automatic_company_details",
	"erpnext.stock.doctype.batch.batch.get_batch_qty": "process_simplification.batch_permissions.get_batch_qty",
	"erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle.get_auto_data": "process_simplification.batch_permissions.get_auto_data",
	"erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle.get_serial_batch_ledgers": "process_simplification.batch_permissions.get_serial_batch_ledgers",
	"erpnext.controllers.queries.get_batch_no": "process_simplification.batch_permissions.get_batch_no",
	"frappe.desk.search.search_link": "process_simplification.batch_permissions.search_link",
	"frappe.desk.search.search_widget": "process_simplification.batch_permissions.search_widget",
}

page_js = {"print": "public/js/print_defaults.js"}
get_print_format_template = "process_simplification.print_localization.get_print_format_template"
pdf_body_html = "process_simplification.printing.factory_pdf_body_html"

boot_session = [
	"process_simplification.pwa.boot_session",
	"process_simplification.navigation_layout.boot_session",
	"process_simplification.branding.boot_session",
	"process_simplification.role_landing.boot_session",
	"process_simplification.document_scan.boot_session",
]

doctype_js = {"Material Request": "public/js/material_request_purchasing.js"}

after_install = "process_simplification.install.after_install"
after_migrate = "process_simplification.install.after_migrate"

jinja = {
	"methods": [
		"process_simplification.printing.get_document_scan_qr",
		"process_simplification.batch_display.get_print_batch_summary",
		"process_simplification.print_localization.get_print_amount_in_words",
	]
}

extend_doctype_class = {
	"Work Order": "process_simplification.production_reporting.work_order.WorkerReportingWorkOrderMixin",
	"Job Card": "process_simplification.production_reporting.job_card.SimplifiedFlowJobCardMixin",
	"Stock Entry": "process_simplification.production_reporting.stock_entry.SubassemblyReservationStockEntryMixin",
	"Stock Reservation Entry": "process_simplification.production_workflow.stock_reservation.GuidedStockReservationEntryMixin",
}

doc_events = {
	"Workspace": {"on_update": "process_simplification.navigation.repair_workspace_sidebar"},
	"Serial and Batch Bundle": {
		"before_validate": "process_simplification.stock_permissions.validate_batch_bundle_scope",
	},
	"*": {
		"after_insert": "process_simplification.page_refresh.document_changed",
		"on_change": "process_simplification.page_refresh.document_changed",
		"on_trash": "process_simplification.page_refresh.document_changed",
	},
	"Notification Log": {
		"after_insert": "process_simplification.notifications.publish_notification_sound",
	},
	"Material Request": {
		"on_submit": "process_simplification.notifications.notify_reorder_material_request",
		"on_change": "process_simplification.notifications.notify_material_request_received",
	},
	"Purchase Receipt": {
		"before_validate": "process_simplification.purchasing.receipts.lock_receipt_sources",
		"before_cancel": ["process_simplification.purchasing.receipts.lock_receipt_sources", "process_simplification.production_exceptions.handling_followup.before_cancel_return"],
		"before_submit": "process_simplification.production_exceptions.handling_followup.validate_return",
		"on_trash": "process_simplification.production_exceptions.handling_followup.prevent_delete",
		"on_submit": ["process_simplification.purchasing.receipts.record_receipt_event", "process_simplification.production_exceptions.handling_followup.complete_return"],
		"on_cancel": ["process_simplification.purchasing.receipts.record_receipt_event", "process_simplification.production_exceptions.handling_followup.cancel_return"],
	},
	"Purchase Order": {
		"before_validate": "process_simplification.purchasing.allocation.lock_order_sources",
		"validate": "process_simplification.purchasing.allocation.validate_order_allocation",
		"before_update_after_submit": "process_simplification.purchasing.allocation.validate_order_allocation",
		"on_change": "process_simplification.purchasing.allocation.validate_order_allocation",
	},
	"Job Card": {
		"before_save": "process_simplification.production_reporting.job_card.before_save",
		"before_submit": "process_simplification.production_reporting.job_card.before_submit",
		"on_submit": "process_simplification.production_reporting.job_card.on_submit",
		"before_cancel": "process_simplification.production_reporting.job_card.before_cancel",
		"before_discard": "process_simplification.production_reporting.job_card.before_discard",
		"before_update_after_submit": "process_simplification.production_reporting.job_card.before_update_after_submit",
	},
	"Work Order": {"before_submit": "process_simplification.production_exceptions.handling_followup.validate_rework_order"},
	"Stock Entry": {
		"before_submit": "process_simplification.production_reporting.stock_entry.before_submit",
		"on_trash": "process_simplification.production_reporting.stock_entry.on_trash",
	},
}

permission_query_conditions = {
	"Serial and Batch Bundle": "process_simplification.batch_permissions.bundle_query",
	"Material Handling Request": "process_simplification.production_exceptions.handling.query_condition",
	"Stock Entry": "process_simplification.stock_permissions.stock_entry_query",
	"Job Card": "process_simplification.production_reporting.permissions.job_card_query",
	"Work Order": "process_simplification.production_reporting.permissions.work_order_query",
	"Job Card Worker Assignment": "process_simplification.production_reporting.permissions.assignment_query",
	"Job Card Assignment Movement": "process_simplification.production_reporting.permissions.movement_query",
	"Job Card Work Report": "process_simplification.production_reporting.permissions.report_query",
	"Operation Wage Rate": "process_simplification.production_reporting.permissions.wage_rate_query",
	"Monthly Worker Wage Summary": "process_simplification.production_reporting.permissions.summary_query",
	"Production Exception Request": "process_simplification.production_exceptions.permissions.request_query",
}

has_permission = {
	"Serial and Batch Bundle": "process_simplification.batch_permissions.bundle_permission",
	"Material Handling Request": "process_simplification.production_exceptions.handling.document_permission",
	"Job Card": "process_simplification.production_reporting.permissions.job_card_permission",
	"Work Order": "process_simplification.production_reporting.permissions.work_order_permission",
	"Job Card Worker Assignment": "process_simplification.production_reporting.permissions.assignment_permission",
	"Job Card Assignment Movement": "process_simplification.production_reporting.permissions.movement_permission",
	"Job Card Work Report": "process_simplification.production_reporting.permissions.report_permission",
	"Operation Wage Rate": "process_simplification.production_reporting.permissions.wage_rate_permission",
	"Monthly Worker Wage Summary": "process_simplification.production_reporting.permissions.summary_permission",
	"Production Exception Request": "process_simplification.production_exceptions.permissions.request_permission",
}

scheduler_events = {
	"daily": ["process_simplification.api.quick_order.cleanup_expired_quick_order_idempotency"],
	"cron": {"* * * * *": ["process_simplification.purchasing.receipts.retry_pending_events"]},
}

add_to_apps_screen = [
	{
		"name": app_name,
		"logo": app_logo_url,
		"title": app_title,
		"route": "/desk/process-simplification",
		"has_permission": "process_simplification.permissions.can_access_app",
	}
]
