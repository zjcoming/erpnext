from __future__ import annotations

import frappe
from frappe import _

from process_simplification.management_access import (
	ALL_APP_MANAGED_ROLES,
	APP_MANAGED_ROLE_PROFILES,
	APP_MANAGED_ROLES,
	CAPABILITY_LABELS,
	EMPLOYEE_ROLE_PROFILE,
	OWNER_ROLE,
	PRODUCTION_MANAGER_ROLE,
	ROLE_DEFINITION_BY_PROFILE,
	ROLE_DEFINITIONS,
	SALES_OPERATOR_ROLE,
	SENSITIVE_ROLES,
	WAGE_MANAGER_ROLE,
	WAREHOUSE_OPERATOR_ROLE,
	WORKER_INCOMPATIBLE_ROLES,
	WORKER_ROLE,
	can_manage_sensitive_roles,
	ensure_employee_role_profile_for_user,
	ensure_management_role_profiles,
	migrate_user_to_management_role_profiles,
	require_access_management,
)

SCOPED_BUSINESS_ROLES = {
	OWNER_ROLE,
	SALES_OPERATOR_ROLE,
	WAREHOUSE_OPERATOR_ROLE,
	PRODUCTION_MANAGER_ROLE,
	WAGE_MANAGER_ROLE,
}


def _parse_set(value) -> set[str]:
	if value in (None, ""):
		return set()
	return {str(item) for item in (frappe.parse_json(value) or []) if item}


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


def _top_level_user_permissions(user: str, allow: str) -> set[str]:
	rows = frappe.get_all(
		"User Permission",
		filters={"user": user, "allow": allow},
		fields=["for_value", "applicable_for"],
		limit=0,
	)
	return {row.for_value for row in rows if row.for_value and not row.applicable_for}


def _warehouse_hide_descendants(user: str) -> set[str]:
	return {
		row.for_value
		for row in frappe.get_all(
			"User Permission", filters={"user": user, "allow": "Warehouse", "hide_descendants": 1},
			fields=["for_value", "applicable_for"], limit=0,
		)
		if row.for_value and not row.applicable_for
	}


def _linked_employee(user: str):
	employees = frappe.get_all(
		"Employee",
		filters={"user_id": user, "status": "Active"},
		fields=["name", "employee_name", "company", "user_id"],
		limit=2,
	)
	if len(employees) > 1:
		frappe.throw(_("This user is linked to more than one active Employee."))
	return employees[0] if employees else None


def _scope_options(user: str):
	companies = frappe.get_all("Company", fields=["name"], order_by="name", limit=0)
	selected = _top_level_user_permissions(user, "Warehouse")
	warehouses = frappe.get_all(
		"Warehouse",
		or_filters={"disabled": 0, **({"name": ("in", sorted(selected))} if selected else {})},
		fields=["name", "warehouse_name", "company", "is_group", "parent_warehouse", "lft", "rgt", "disabled"],
		order_by="company, lft, name",
		limit=0,
	)
	employees = frappe.db.sql(
		"""
		select name, employee_name, company, user_id
		from `tabEmployee`
		where status = 'Active'
		  and (ifnull(user_id, '') = '' or user_id = %(user)s)
		order by company, employee_name, name
		""",
		{"user": user},
		as_dict=True,
	)
	return {
		"companies": [row.name for row in companies],
		"warehouses": warehouses,
		"employees": employees,
	}


def _managed_role_payload(definition, assigned_profiles: set[str], assigned_roles: set[str]):
	capabilities = list(definition["capabilities"])
	return {
		**definition,
		"capabilities": capabilities,
		"capability_labels": [CAPABILITY_LABELS[capability] for capability in capabilities],
		"assigned": definition["profile"] in assigned_profiles or definition["role"] in assigned_roles,
	}


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
	user_doc = frappe.get_doc("User", user)
	assigned_profiles = {row.role_profile for row in user_doc.role_profiles if row.role_profile}
	assigned_roles = set(frappe.get_roles(user))
	linked_employee = _linked_employee(user)
	return {
		"user": user_details,
		"roles": [
			_managed_role_payload(definition, assigned_profiles, assigned_roles)
			for definition in ROLE_DEFINITIONS
		],
		"assigned_profiles": sorted(assigned_profiles.intersection(APP_MANAGED_ROLE_PROFILES)),
		"retained_profiles": sorted(
			assigned_profiles.difference(APP_MANAGED_ROLE_PROFILES, {EMPLOYEE_ROLE_PROFILE})
		),
		"companies": sorted(_top_level_user_permissions(user, "Company")),
		"warehouses": sorted(_top_level_user_permissions(user, "Warehouse")),
		"warehouse_hide_descendants": sorted(_warehouse_hide_descendants(user)),
		"employee": linked_employee,
		"scope_options": _scope_options(user),
		"can_manage_sensitive": can_manage_sensitive_roles(),
		"source": "Role Profile",
	}


def _validate_scopes(requested_roles: set[str], companies: set[str], warehouses: set[str], employee):
	unknown_companies = companies.difference(frappe.get_all("Company", pluck="name", limit=0))
	if unknown_companies:
		frappe.throw(_("Unknown Company: {0}").format(", ".join(sorted(unknown_companies))))

	warehouse_rows = (
		frappe.get_all(
			"Warehouse",
			filters={"name": ("in", sorted(warehouses))},
			fields=["name", "company", "disabled", "is_group"],
			limit=0,
		)
		if warehouses
		else []
	)
	warehouse_companies = {
		row.name: row.company
		for row in warehouse_rows
		if not row.disabled
	}
	unknown_warehouses = warehouses.difference(warehouse_companies)
	if unknown_warehouses:
		frappe.throw(_("Unknown or unavailable Warehouse: {0}").format(", ".join(sorted(unknown_warehouses))))
	outside_scope = {warehouse for warehouse, company in warehouse_companies.items() if company not in companies}
	if outside_scope:
		frappe.throw(
			_("Every selected Warehouse must belong to a selected Company: {0}").format(
				", ".join(sorted(outside_scope))
			)
		)
	if requested_roles.intersection(SCOPED_BUSINESS_ROLES) and not companies:
		frappe.throw(_("Select at least one Company for this business岗位."))
	if WAREHOUSE_OPERATOR_ROLE in requested_roles and not warehouses:
		frappe.throw(_("库房岗位至少需要选择一个仓库或仓库组。"))
	if WAGE_MANAGER_ROLE in requested_roles and not companies:
		frappe.throw(_("The wage-management岗位 requires an explicit Company."))
	if WORKER_ROLE in requested_roles and not employee:
		frappe.throw(_("The worker岗位 requires an active Employee linked to this user."))


def _normalize_warehouse_scopes(warehouses: set[str], hide_descendants: set[str]) -> set[str]:
	"""Store group grants, not a snapshot of their current descendants."""
	if not warehouses:
		return set()
	rows = frappe.get_all(
		"Warehouse", filters={"name": ("in", sorted(warehouses))},
		fields=["name", "company", "is_group", "lft", "rgt"], limit=0,
	)
	groups = [row for row in rows if row.is_group and row.name not in hide_descendants]
	return {
		row.name for row in rows
		if not any(
			group.company == row.company and group.lft < row.lft and row.rgt < group.rgt
			for group in groups
		)
	}


def _resolve_employee(user: str, employee: str | None):
	current = _linked_employee(user)
	if current:
		if employee and employee != current.name:
			frappe.throw(_("This user is already linked to active Employee {0}.").format(current.name))
		return current
	if not employee:
		return None
	row = frappe.db.get_value(
		"Employee",
		employee,
		["name", "employee_name", "company", "status", "user_id"],
		as_dict=True,
	)
	if not row or row.status != "Active":
		frappe.throw(_("Select an active Employee."))
	if row.user_id and row.user_id != user:
		frappe.throw(_("Employee {0} is already linked to another user.").format(employee))
	return row


def _bind_employee(user: str, employee):
	if not employee:
		return None
	if employee.user_id != user:
		doc = frappe.get_doc("Employee", employee.name)
		doc.user_id = user
		doc.create_user_permission = 1
		doc.save(ignore_permissions=True)
	ensure_employee_role_profile_for_user(user)
	return employee


def _sync_scope_permissions(user: str, allow: str, values: set[str], hide_descendants=None):
	from frappe.permissions import add_user_permission

	rows = frappe.get_all(
		"User Permission",
		filters={"user": user, "allow": allow},
		fields=["name", "applicable_for"],
		limit=0,
	)
	for row in rows:
		if not row.applicable_for:
			frappe.delete_doc("User Permission", row.name, force=True, ignore_permissions=True)
	for value in sorted(values):
		add_user_permission(
			allow, value, user, ignore_permissions=True,
			hide_descendants=int(value in (hide_descendants or set())),
		)


@frappe.whitelist(methods=["POST"])
def set_user_access(
	user: str,
	profiles=None,
	roles=None,
	companies=None,
	warehouses=None,
	employee: str | None = None,
	warehouse_hide_descendants=None,
):
	require_access_management()
	user_details = _validate_target_user(user)
	ensure_management_role_profiles()

	requested_profiles = _parse_set(profiles)
	requested_roles = _parse_set(roles)
	if requested_profiles and requested_roles:
		frappe.throw(_("Choose岗位 templates or legacy APP roles, not both."))
	# Compatibility for callers that passed roles as the second positional
	# argument before this API switched to Role Profile names.
	if requested_profiles and requested_profiles.issubset(APP_MANAGED_ROLES):
		requested_roles = requested_profiles
		requested_profiles = set()
	if requested_profiles:
		unknown_profiles = requested_profiles.difference(APP_MANAGED_ROLE_PROFILES)
		if unknown_profiles:
			frappe.throw(_("Unsupported岗位模板: {0}").format(", ".join(sorted(unknown_profiles))))
		requested_roles = {ROLE_DEFINITION_BY_PROFILE[profile]["role"] for profile in requested_profiles}
	else:
		unknown_roles = requested_roles.difference(APP_MANAGED_ROLES)
		if unknown_roles:
			frappe.throw(_("Unsupported APP role: {0}").format(", ".join(sorted(unknown_roles))))

	company_scope = _parse_set(companies)
	warehouse_scope = _parse_set(warehouses)
	# Legacy/native exact-node grants must not expand during an unrelated role edit.
	hide_descendants = (
		_warehouse_hide_descendants(user).intersection(warehouse_scope)
		if warehouse_hide_descendants is None
		else _parse_set(warehouse_hide_descendants)
	)
	if hide_descendants.difference(warehouse_scope):
		frappe.throw(_("仅组本身的授权必须属于已选择的仓库范围。"))
	employee_row = _resolve_employee(user, employee)
	if WORKER_ROLE in requested_roles and employee_row:
		if company_scope.difference({employee_row.company}):
			frappe.throw(_("工人账号只能选择关联员工所属公司：{0}。").format(employee_row.company))
		company_scope.add(employee_row.company)

	current_roles = set(frappe.get_roles(user))
	resulting_roles = current_roles.difference(ALL_APP_MANAGED_ROLES).union(requested_roles)
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
				_("Only a System Manager can grant or remove owner, wage-manager, and APP access-manager岗位."),
				frappe.PermissionError,
			)
	_validate_scopes(requested_roles, company_scope, warehouse_scope, employee_row)
	warehouse_scope = _normalize_warehouse_scopes(warehouse_scope, hide_descendants)
	hide_descendants.intersection_update(warehouse_scope)

	assigned_profiles = migrate_user_to_management_role_profiles(user, requested_roles)
	employee_row = _bind_employee(user, employee_row)
	_sync_scope_permissions(user, "Company", company_scope)
	_sync_scope_permissions(user, "Warehouse", warehouse_scope, hide_descendants)
	frappe.clear_cache(user=user)

	return {
		"user": user_details,
		"assigned_profiles": [
			profile for profile in APP_MANAGED_ROLE_PROFILES if profile in assigned_profiles
		],
		"assigned_roles": [role for role in APP_MANAGED_ROLES if role in requested_roles],
		"companies": sorted(company_scope),
		"warehouses": sorted(warehouse_scope),
		"warehouse_hide_descendants": sorted(hide_descendants),
		"employee": employee_row,
		"message": _("Process Simplification岗位 and data scope updated."),
	}
