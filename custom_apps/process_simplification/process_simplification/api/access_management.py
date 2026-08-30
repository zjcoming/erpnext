from __future__ import annotations

import frappe
from frappe import _

from process_simplification.management_access import (
	ACCESS_MANAGER_ROLE,
	APP_MANAGED_ROLES,
	ROLE_DEFINITIONS,
	SENSITIVE_ROLES,
	WORKER_INCOMPATIBLE_ROLES,
	WORKER_ROLE,
	can_manage_sensitive_roles,
	require_access_management,
)


def _validate_target_user(user: str):
	if not user or user in {"Administrator", "Guest"}:
		frappe.throw(_("Select an enabled system user other than Administrator."))
	user_details = frappe.db.get_value(
		"User",
		user,
		["name", "full_name", "enabled", "user_type"],
		as_dict=True,
	)
	if not user_details or not user_details.enabled or user_details.user_type != "System User":
		frappe.throw(_("Select an enabled system user."))
	return user_details


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def search_users(doctype, txt, searchfield, start, page_len, filters):
	require_access_management()
	return frappe.db.sql(
		"""
		select name, full_name
		from `tabUser`
		where enabled = 1
		  and user_type = 'System User'
		  and name not in ('Administrator', 'Guest')
		  and (name like %(txt)s or ifnull(full_name, '') like %(txt)s)
		order by full_name asc, name asc
		limit %(start)s, %(page_len)s
		""",
		{
			"txt": f"%{txt}%",
			"start": int(start or 0),
			"page_len": min(int(page_len or 20), 50),
		},
	)


@frappe.whitelist()
def get_user_access(user: str):
	require_access_management()
	user_details = _validate_target_user(user)
	assigned_roles = set(frappe.get_roles(user))
	return {
		"user": user_details,
		"roles": [dict(definition, assigned=definition["role"] in assigned_roles) for definition in ROLE_DEFINITIONS],
		"can_manage_sensitive": can_manage_sensitive_roles(),
	}


@frappe.whitelist(methods=["POST"])
def set_user_access(user: str, roles=None):
	require_access_management()
	user_details = _validate_target_user(user)
	requested_roles = set(frappe.parse_json(roles) or [])
	unknown_roles = requested_roles.difference(APP_MANAGED_ROLES)
	if unknown_roles:
		frappe.throw(_("Unsupported APP role: {0}").format(", ".join(sorted(unknown_roles))))

	current_roles = set(frappe.get_roles(user))
	resulting_roles = current_roles.difference(APP_MANAGED_ROLES).union(requested_roles)
	if WORKER_ROLE in resulting_roles:
		incompatible = resulting_roles.intersection(WORKER_INCOMPATIBLE_ROLES)
		if incompatible:
			frappe.throw(
				_("A production-worker account cannot also hold these roles: {0}.").format(
					", ".join(sorted(incompatible))
				)
			)
	if not can_manage_sensitive_roles():
		if user == frappe.session.user:
			frappe.throw(_("An APP access manager cannot change their own access."), frappe.PermissionError)
		current_sensitive = current_roles.intersection(SENSITIVE_ROLES)
		requested_sensitive = requested_roles.intersection(SENSITIVE_ROLES)
		if current_sensitive != requested_sensitive:
			frappe.throw(
				_("Only a System Manager can grant or remove owner, wage-manager, and APP access-manager roles."),
				frappe.PermissionError,
			)

	user_doc = frappe.get_doc("User", user)
	for role_row in list(user_doc.roles):
		if role_row.role in APP_MANAGED_ROLES:
			user_doc.roles.remove(role_row)
	for role_name in APP_MANAGED_ROLES:
		if role_name in requested_roles:
			user_doc.append("roles", {"role": role_name})
	user_doc.save(ignore_permissions=True)
	frappe.clear_cache(user=user)

	return {
		"user": user_details,
		"assigned_roles": [role for role in APP_MANAGED_ROLES if role in requested_roles],
		"message": _("Process Simplification access updated."),
	}
