import frappe
from frappe import _
from frappe.model.document import Document

from process_simplification.management_access import OWNER_ROLE, SYSTEM_MANAGER_ROLE


def _require_notification_settings_access() -> None:
	if frappe.session.user == "Administrator":
		return
	if not {OWNER_ROLE, SYSTEM_MANAGER_ROLE}.intersection(frappe.get_roles()):
		frappe.throw(
			_("Only the owner or a System Manager can configure notification routing."),
			frappe.PermissionError,
		)


def _search_filters(filters):
	return frappe._dict(frappe.parse_json(filters) if isinstance(filters, str) else filters or {})


class ProcessSimplificationSettings(Document):
	def validate(self):
		from process_simplification.notifications import validate_notification_routes

		validate_notification_routes(self)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def search_notification_users(doctype, txt, searchfield, start, page_len, filters):
	_require_notification_settings_access()
	filters = _search_filters(filters)
	if not filters.company or not filters.responsibility:
		return []
	from process_simplification.notifications import eligible_notification_users

	needle = str(txt or "").strip().lower()
	rows = [
		row
		for row in eligible_notification_users(filters.company, filters.responsibility)
		if not needle
		or needle in str(row.name or "").lower()
		or needle in str(row.full_name or "").lower()
	]
	start = max(int(start or 0), 0)
	page_len = max(int(page_len or 20), 1)
	return [[row.name, row.full_name or row.name] for row in rows[start : start + page_len]]


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def search_notification_role_profiles(doctype, txt, searchfield, start, page_len, filters):
	_require_notification_settings_access()
	filters = _search_filters(filters)
	if not filters.responsibility:
		return []
	from process_simplification.notifications import allowed_notification_role_profiles

	needle = str(txt or "").strip().lower()
	profiles = [
		profile
		for profile in allowed_notification_role_profiles(filters.responsibility)
		if not needle or needle in profile.lower()
	]
	start = max(int(start or 0), 0)
	page_len = max(int(page_len or 20), 1)
	return [[profile] for profile in profiles[start : start + page_len]]
