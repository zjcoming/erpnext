"""Fresh, user-scoped notification snapshots for unreliable mobile realtime links."""

import frappe
from frappe.desk.doctype.notification_settings.notification_settings import is_notifications_enabled

from process_simplification.notifications import (
	APP_NAME,
	process_notification_sound_enabled,
	process_notifications_enabled,
)


@frappe.whitelist(methods=["POST"])
def get_notification_snapshot():
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("请先登录后查看通知。", frappe.PermissionError)
	# POST and no HTTP cache decorator: realtime events must not fetch an old list.
	if getattr(frappe.local, "response_headers", None) is not None:
		frappe.local.response_headers.set("Cache-Control", "no-store")
	if not is_notifications_enabled(user):
		return {
			"enabled": False,
			"notification_logs": [],
			"unread_count": 0,
			"seen": 1,
			"user_info": {},
			"latest_process_notification": None,
			"play_sound": False,
		}
	filters = {"for_user": user}
	logs = frappe.get_list(
		"Notification Log",
		filters=filters,
		fields=["*"],
		order_by="creation desc, name desc",
		limit_page_length=20,
	)
	latest = frappe.get_list(
		"Notification Log",
		filters={**filters, "app": APP_NAME},
		fields=["name", "creation", "read"],
		order_by="creation desc, name desc",
		limit_page_length=1,
	)
	user_info = frappe._dict()
	for sender in {log.from_user for log in logs if log.from_user}:
		frappe.utils.add_user_info(sender, user_info)
	return {
		"enabled": True,
		"notification_logs": logs,
		"user_info": user_info,
		"unread_count": frappe.db.count("Notification Log", {**filters, "read": 0}),
		"seen": frappe.db.get_value("Notification Settings", user, "seen") or 0,
		"latest_process_notification": latest[0] if latest else None,
		"play_sound": process_notifications_enabled() and process_notification_sound_enabled(),
	}
