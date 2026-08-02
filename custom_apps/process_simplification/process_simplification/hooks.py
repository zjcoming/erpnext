app_name = "process_simplification"
app_title = "流程简化"
app_publisher = "Custom ERPNext Implementation"
app_description = "Simplified manufacturing workflow for small factories"
app_email = "admin@example.com"
app_license = "GPL-3.0"

required_apps = ["erpnext"]

app_include_css = "/assets/process_simplification/css/process_simplification.css?v=5"

after_install = "process_simplification.install.after_install"

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
