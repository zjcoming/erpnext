from __future__ import annotations

import hashlib

import frappe
from frappe import _

OWNER_ROLE = "Process Simplification Owner"
SALES_OPERATOR_ROLE = "Process Simplification Sales Operator"
WAREHOUSE_OPERATOR_ROLE = "Process Simplification Warehouse Operator"
PRODUCTION_MANAGER_ROLE = "Process Simplification Production Manager"
ACCESS_MANAGER_ROLE = "Process Simplification Access Manager"
WORKER_ROLE = "Production Worker"
SUPERVISOR_ROLE = "Production Supervisor"
WAGE_MANAGER_ROLE = "Production Wage Manager"
SYSTEM_MANAGER_ROLE = "System Manager"

CAPABILITY_EXECUTIVE_DASHBOARD = "executive_dashboard"
CAPABILITY_SALES_ORDER = "sales_order"
CAPABILITY_ORDER_WORKBENCH = "order_workbench"
CAPABILITY_PRODUCTION_PLANNING = "production_planning"
CAPABILITY_WORKER_REPORTING = "worker_reporting"
CAPABILITY_PRODUCTION_REVIEW = "production_review"
CAPABILITY_EXCEPTION_STOCK = "exception_stock"
CAPABILITY_SHORTAGE_PURCHASE = "shortage_purchase"
CAPABILITY_WAGE_MANAGEMENT = "wage_management"
CAPABILITY_ACCESS_MANAGEMENT = "access_management"

CAPABILITY_LABELS = {
	CAPABILITY_EXECUTIVE_DASHBOARD: "经营总览",
	CAPABILITY_SALES_ORDER: "快速开单",
	CAPABILITY_ORDER_WORKBENCH: "订单工作台",
	CAPABILITY_PRODUCTION_PLANNING: "生产计划",
	CAPABILITY_WORKER_REPORTING: "本人派工与报工",
	CAPABILITY_PRODUCTION_REVIEW: "派工、报工与异常审核",
	CAPABILITY_EXCEPTION_STOCK: "生产异常库存落账",
	CAPABILITY_SHORTAGE_PURCHASE: "缺料采购",
	CAPABILITY_WAGE_MANAGEMENT: "计价与工资核算",
	CAPABILITY_ACCESS_MANAGEMENT: "岗位与数据范围管理",
}

# Kept only so older sites can migrate without losing access. New assignments
# use PRODUCTION_MANAGER_ROLE as the single production-management role.
LEGACY_APP_MANAGED_ROLES = (SUPERVISOR_ROLE,)

ROLE_DEFINITIONS = (
	{
		"role": OWNER_ROLE,
		"profile": "流程简化 - 老板",
		"label": "老板",
		"description": "使用全部业务管理入口和工资管理；不包含工人自助入口或系统管理员能力。",
		"sensitive": True,
		"capabilities": (
			CAPABILITY_EXECUTIVE_DASHBOARD,
			CAPABILITY_SALES_ORDER,
			CAPABILITY_ORDER_WORKBENCH,
			CAPABILITY_PRODUCTION_PLANNING,
			CAPABILITY_PRODUCTION_REVIEW,
			CAPABILITY_EXCEPTION_STOCK,
			CAPABILITY_SHORTAGE_PURCHASE,
			CAPABILITY_WAGE_MANAGEMENT,
		),
	},
	{
		"role": SALES_OPERATOR_ROLE,
		"profile": "流程简化 - 销售",
		"label": "销售人员",
		"description": "使用快速开单和订单工作台；仍需相应的 ERPNext 销售单据权限。",
		"sensitive": False,
		"capabilities": (CAPABILITY_SALES_ORDER, CAPABILITY_ORDER_WORKBENCH),
	},
	{
		"role": WAREHOUSE_OPERATOR_ROLE,
		"profile": "流程简化 - 库房",
		"label": "库房人员",
		"description": "处理库存、收发料、缺料采购和已批准异常的库存落账；不审批工人报工。",
		"sensitive": False,
		"capabilities": (
			CAPABILITY_ORDER_WORKBENCH,
			CAPABILITY_PRODUCTION_PLANNING,
			CAPABILITY_EXCEPTION_STOCK,
			CAPABILITY_SHORTAGE_PURCHASE,
		),
	},
	{
		"role": PRODUCTION_MANAGER_ROLE,
		"profile": "流程简化 - 生产主管",
		"label": "生产主管/生产管理员",
		"description": "统一负责生产计划、派工，以及本公司范围内的报工和生产异常审核。",
		"sensitive": False,
		"capabilities": (
			CAPABILITY_ORDER_WORKBENCH,
			CAPABILITY_PRODUCTION_PLANNING,
			CAPABILITY_PRODUCTION_REVIEW,
			CAPABILITY_EXCEPTION_STOCK,
			CAPABILITY_SHORTAGE_PURCHASE,
		),
	},
	{
		"role": WORKER_ROLE,
		"profile": "流程简化 - 流水线工人",
		"label": "流水线工人",
		"description": "仅使用本人的派工和报工入口。",
		"sensitive": False,
		"capabilities": (CAPABILITY_WORKER_REPORTING,),
	},
	{
		"role": WAGE_MANAGER_ROLE,
		"profile": "流程简化 - 工资核算",
		"label": "工资核算人员",
		"description": "维护计价规则并核算、确认月度工资；必须另外限制可访问公司。",
		"sensitive": True,
		"capabilities": (CAPABILITY_WAGE_MANAGEMENT,),
	},
	{
		"role": ACCESS_MANAGER_ROLE,
		"profile": "流程简化 - 权限管理员",
		"label": "APP 权限管理员",
		"description": "分配普通 APP 岗位；只有系统管理员可以授予老板和权限管理员角色。",
		"sensitive": True,
		"capabilities": (CAPABILITY_ACCESS_MANAGEMENT,),
	},
)

APP_MANAGED_ROLES = tuple(definition["role"] for definition in ROLE_DEFINITIONS)
APP_MANAGED_ROLE_PROFILES = tuple(definition["profile"] for definition in ROLE_DEFINITIONS)
ROLE_DEFINITION_BY_ROLE = {definition["role"]: definition for definition in ROLE_DEFINITIONS}
ROLE_DEFINITION_BY_PROFILE = {definition["profile"]: definition for definition in ROLE_DEFINITIONS}
ALL_APP_MANAGED_ROLES = tuple(dict.fromkeys((*APP_MANAGED_ROLES, *LEGACY_APP_MANAGED_ROLES)))
SENSITIVE_ROLES = {OWNER_ROLE, WAGE_MANAGER_ROLE, ACCESS_MANAGER_ROLE}
APP_NON_WORKER_ROLES = set(ALL_APP_MANAGED_ROLES).difference({WORKER_ROLE})
PRESERVED_PROFILE_PREFIX = "流程简化 - 迁移保留 - "
EMPLOYEE_ROLE_PROFILE = "流程简化 - 员工自助"
WORKER_INCOMPATIBLE_ROLES = {
	"Manufacturing User",
	"Manufacturing Manager",
	"Shop Floor User",
	"Shop Floor Manager",
	"System Manager",
	*APP_NON_WORKER_ROLES,
}

SYSTEM_MANAGER_CAPABILITIES = {
	CAPABILITY_SALES_ORDER,
	CAPABILITY_ORDER_WORKBENCH,
	CAPABILITY_PRODUCTION_PLANNING,
	CAPABILITY_PRODUCTION_REVIEW,
	CAPABILITY_EXCEPTION_STOCK,
	CAPABILITY_SHORTAGE_PURCHASE,
	CAPABILITY_WAGE_MANAGEMENT,
	CAPABILITY_ACCESS_MANAGEMENT,
}


def roles_for_capability(capability: str, *, include_system_manager: bool = True) -> set[str]:
	roles = {
		definition["role"]
		for definition in ROLE_DEFINITIONS
		if capability in definition["capabilities"]
	}
	if include_system_manager and capability in SYSTEM_MANAGER_CAPABILITIES:
		roles.add(SYSTEM_MANAGER_ROLE)
	return roles


def user_has_capability(capability: str, user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	return bool(set(frappe.get_roles(user)).intersection(roles_for_capability(capability)))


def user_company_scope(user: str | None = None, *, include_employee: bool = True) -> set[str] | None:
	"""Return the user's explicit company boundary; System Manager is unrestricted."""
	user = user or frappe.session.user
	if user == "Administrator" or SYSTEM_MANAGER_ROLE in set(frappe.get_roles(user)):
		return None
	companies = set(
		frappe.db.sql(
			"""
			select for_value
			from `tabUser Permission`
			where user = %s
			  and allow = 'Company'
			  and ifnull(applicable_for, '') = ''
			""",
			user,
			pluck=True,
		)
	)
	if include_employee:
		companies.update(
			frappe.get_all(
				"Employee",
				filters={"user_id": user, "status": "Active"},
				pluck="company",
				limit=0,
			)
		)
	return {company for company in companies if company}


REFERENCE_READ_DOCTYPES = {
	"BOM",
	"Company",
	"Customer",
	"Item",
	"Supplier",
	"UOM",
	"Warehouse",
}

SALES_OPERATOR_PERMISSIONS = {
	**{doctype: {"read", "select"} for doctype in REFERENCE_READ_DOCTYPES},
	"Sales Order": {"read", "select", "create", "write", "submit"},
	"Production Plan": {"read", "select"},
	"Work Order": {"read", "select"},
	"Stock Reservation Entry": {"read", "select"},
	"Delivery Note": {"read", "select"},
}

WAREHOUSE_OPERATOR_PERMISSIONS = {
	**{doctype: {"read", "select"} for doctype in REFERENCE_READ_DOCTYPES},
	"Sales Order": {"read", "select"},
	"Production Plan": {"read", "select"},
	"Work Order": {"read", "select"},
	"Job Card": {"read", "select"},
	"Stock Reservation Entry": {"read", "select", "create", "write", "submit"},
	"Stock Entry": {"read", "select", "create", "write", "submit"},
	"Delivery Note": {"read", "select", "create", "write", "submit"},
	"Material Request": {"read", "select", "create", "write", "submit"},
	"Purchase Order": {"read", "select", "create", "write"},
	"Purchase Receipt": {"read", "select", "create", "write", "submit"},
}

PRODUCTION_MANAGER_PERMISSIONS = {
	**{doctype: {"read", "select"} for doctype in REFERENCE_READ_DOCTYPES},
	"Sales Order": {"read", "select"},
	"Production Plan": {"read", "select", "create", "write", "submit"},
	"Work Order": {"read", "select", "create", "write", "submit"},
	"Job Card": {"read", "select"},
	"Stock Reservation Entry": {"read", "select"},
	"Stock Entry": {"read", "select"},
	"Delivery Note": {"read", "select"},
	"Material Request": {"read", "select"},
	"Purchase Order": {"read", "select"},
}


def _merge_permissions(*permission_maps):
	merged = {}
	for permission_map in permission_maps:
		for doctype, permission_types in permission_map.items():
			merged.setdefault(doctype, set()).update(permission_types)
	return merged


OWNER_PERMISSIONS = _merge_permissions(
	SALES_OPERATOR_PERMISSIONS,
	WAREHOUSE_OPERATOR_PERMISSIONS,
	PRODUCTION_MANAGER_PERMISSIONS,
	{"Purchase Order": {"read", "select", "create", "write", "submit"}},
)

MANAGED_DOCUMENT_PERMISSIONS = {
	OWNER_ROLE: OWNER_PERMISSIONS,
	SALES_OPERATOR_ROLE: SALES_OPERATOR_PERMISSIONS,
	WAREHOUSE_OPERATOR_ROLE: WAREHOUSE_OPERATOR_PERMISSIONS,
	PRODUCTION_MANAGER_ROLE: PRODUCTION_MANAGER_PERMISSIONS,
}

PAGE_CAPABILITIES = {
	"executive-dashboard": CAPABILITY_EXECUTIVE_DASHBOARD,
	"quick-sales-order": CAPABILITY_SALES_ORDER,
	"order-workbench": CAPABILITY_ORDER_WORKBENCH,
	"production-workbench": CAPABILITY_PRODUCTION_PLANNING,
	"active-production-work": CAPABILITY_WORKER_REPORTING,
	"my-production-reporting": CAPABILITY_WORKER_REPORTING,
	"production-report-history": CAPABILITY_WORKER_REPORTING,
	"production-report-review": CAPABILITY_PRODUCTION_REVIEW,
	"production-exception-review": CAPABILITY_EXCEPTION_STOCK,
	"shortage-purchase-planning": CAPABILITY_SHORTAGE_PURCHASE,
	"process-access-management": CAPABILITY_ACCESS_MANAGEMENT,
}

NATIVE_PAGE_ROLES = {
	"quick-sales-order": {"Sales User", "Manufacturing User", SYSTEM_MANAGER_ROLE},
	"order-workbench": {"Sales User", "Manufacturing User", "Stock User", SYSTEM_MANAGER_ROLE},
	"production-workbench": {"Manufacturing User", "Stock User", SYSTEM_MANAGER_ROLE},
	"production-report-review": {SYSTEM_MANAGER_ROLE},
	"production-exception-review": {"Stock User", "Stock Manager", SYSTEM_MANAGER_ROLE},
	"shortage-purchase-planning": {"Manufacturing User", "Purchase User", SYSTEM_MANAGER_ROLE},
	"process-access-management": {SYSTEM_MANAGER_ROLE},
}

MANAGED_PAGE_ROLES = {
	page_name: roles_for_capability(capability).union(NATIVE_PAGE_ROLES.get(page_name, set()))
	for page_name, capability in PAGE_CAPABILITIES.items()
}

DOCUMENT_PERMISSION_FIELDS = {
	"read",
	"write",
	"create",
	"delete",
	"submit",
	"cancel",
	"amend",
	"report",
	"export",
	"import",
	"share",
	"print",
	"email",
	"select",
}


def ensure_management_roles():
	for role_name in APP_MANAGED_ROLES:
		if frappe.db.exists("Role", role_name):
			continue
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": role_name,
				"desk_access": 1,
			}
		).insert(ignore_permissions=True)


def _ensure_role_profile(profile_name: str, roles: set[str]) -> str:
	if frappe.db.exists("Role Profile", profile_name):
		profile = frappe.get_doc("Role Profile", profile_name)
	else:
		profile = frappe.new_doc("Role Profile")
		profile.role_profile = profile_name
	current_roles = {row.role for row in profile.roles}
	roles_changed = current_roles != roles
	if roles_changed:
		profile.set("roles", [{"role": role} for role in sorted(roles)])
	if profile.is_new():
		profile.insert(ignore_permissions=True)
	elif roles_changed:
		profile.save(ignore_permissions=True)
	return profile.name


def ensure_management_role_profiles():
	"""Keep the business-facing job templates as the only source of APP roles."""
	ensure_management_roles()
	for definition in ROLE_DEFINITIONS:
		_ensure_role_profile(definition["profile"], {definition["role"]})
	_ensure_role_profile(EMPLOYEE_ROLE_PROFILE, {"Employee"})


def _profile_roles(profile_names) -> set[str]:
	profile_names = {name for name in profile_names if name}
	if not profile_names:
		return set()
	return set(
		frappe.get_all(
			"Has Role",
			filters={"parenttype": "Role Profile", "parent": ("in", sorted(profile_names))},
			pluck="role",
			limit=0,
		)
	)


def _preserved_role_profile(roles: set[str]) -> str | None:
	roles = set(roles).difference(ALL_APP_MANAGED_ROLES)
	if not roles:
		return None
	digest = hashlib.sha256("\n".join(sorted(roles)).encode()).hexdigest()[:10]
	return _ensure_role_profile(f"{PRESERVED_PROFILE_PREFIX}{digest}", roles)


def managed_profiles_for_roles(roles) -> list[str]:
	normalized = set(roles or [])
	if SUPERVISOR_ROLE in normalized:
		normalized.remove(SUPERVISOR_ROLE)
		normalized.add(PRODUCTION_MANAGER_ROLE)
	return [
		definition["profile"]
		for definition in ROLE_DEFINITIONS
		if definition["role"] in normalized
	]


def migrate_user_to_management_role_profiles(
	user: str,
	roles=None,
) -> list[str]:
	"""Move APP roles to managed Role Profiles while retaining unrelated native roles."""
	ensure_management_role_profiles()
	user_doc = frappe.get_doc("User", user)
	existing_profiles = [row.role_profile for row in user_doc.role_profiles if row.role_profile]
	existing_profile_roles = _profile_roles(existing_profiles)
	direct_roles = {row.role for row in user_doc.roles}
	requested_roles = set(roles) if roles is not None else direct_roles.intersection(ALL_APP_MANAGED_ROLES)
	managed_profiles = managed_profiles_for_roles(requested_roles)

	retained_profiles = [
		profile
		for profile in existing_profiles
		if profile not in APP_MANAGED_ROLE_PROFILES
	]
	has_linked_employee = bool(frappe.db.exists("Employee", {"user_id": user, "status": "Active"}))
	if has_linked_employee or "Employee" in direct_roles:
		retained_profiles.append(EMPLOYEE_ROLE_PROFILE)
	uncovered_native_roles = direct_roles.difference(
		ALL_APP_MANAGED_ROLES,
		existing_profile_roles,
		{"Employee"},
	)
	if preserved_profile := _preserved_role_profile(uncovered_native_roles):
		retained_profiles.append(preserved_profile)

	resulting_profiles = list(dict.fromkeys((*retained_profiles, *managed_profiles)))
	user_doc.set("role_profiles", [{"role_profile": profile} for profile in resulting_profiles])
	for role_row in list(user_doc.roles):
		if role_row.role in ALL_APP_MANAGED_ROLES:
			user_doc.roles.remove(role_row)
	user_doc.save(ignore_permissions=True)
	frappe.clear_cache(user=user)
	return resulting_profiles


def ensure_employee_role_profile_for_user(user: str) -> list[str]:
	"""Keep ERPNext's Employee role profile-backed after an Employee is linked."""
	ensure_management_role_profiles()
	user_doc = frappe.get_doc("User", user)
	profiles = [row.role_profile for row in user_doc.role_profiles if row.role_profile]
	if EMPLOYEE_ROLE_PROFILE not in profiles:
		profiles.append(EMPLOYEE_ROLE_PROFILE)
		user_doc.set("role_profiles", [{"role_profile": profile} for profile in profiles])
		user_doc.save(ignore_permissions=True)
		frappe.clear_cache(user=user)
	return profiles


def migrate_management_users_to_role_profiles() -> list[str]:
	"""Idempotently migrate every user still carrying a direct APP role."""
	users = frappe.get_all(
		"Has Role",
		filters={"parenttype": "User", "role": ("in", ALL_APP_MANAGED_ROLES)},
		pluck="parent",
		distinct=True,
		limit=0,
	)
	migrated = []
	for user in sorted(set(users)):
		if frappe.db.exists("User", user):
			migrate_user_to_management_role_profiles(user)
			migrated.append(user)
	return migrated


def retire_legacy_management_roles():
	"""Remove residual native rights and disable roles replaced by managed岗位 templates."""
	affected_doctypes = set(
		frappe.get_all(
			"Custom DocPerm",
			filters={"role": ("in", LEGACY_APP_MANAGED_ROLES)},
			pluck="parent",
			limit=0,
		)
	)
	frappe.db.delete("Custom DocPerm", {"role": ("in", LEGACY_APP_MANAGED_ROLES)})
	for doctype in affected_doctypes:
		frappe.clear_cache(doctype=doctype)
	for role in LEGACY_APP_MANAGED_ROLES:
		if frappe.db.exists("Role", role) and not frappe.db.exists("Has Role", {"role": role}):
			frappe.db.set_value("Role", role, "disabled", 1, update_modified=False)


def ensure_management_document_permissions():
	"""Install exact native document rights for the simplified business roles."""
	from frappe.permissions import add_permission

	for role, doctype_permissions in MANAGED_DOCUMENT_PERMISSIONS.items():
		for doctype, allowed_permissions in doctype_permissions.items():
			if not frappe.db.exists("DocType", doctype):
				continue
			filters = {
				"parent": doctype,
				"role": role,
				"permlevel": 0,
				"if_owner": 0,
			}
			permission = frappe.db.get_value("Custom DocPerm", filters)
			if not permission:
				add_permission(doctype, role, permlevel=0, ptype="read")
				permission = frappe.db.get_value("Custom DocPerm", filters)
			if not permission:
				continue
			updates = {
				permission_type: int(permission_type in allowed_permissions)
				for permission_type in DOCUMENT_PERMISSION_FIELDS
			}
			frappe.db.set_value(
				"Custom DocPerm",
				permission,
				updates,
				update_modified=False,
			)
			frappe.clear_cache(doctype=doctype)


def ensure_management_page_roles():
	"""Make code-derived capabilities the exact source of every APP Page role list."""
	for page_name, required_roles in MANAGED_PAGE_ROLES.items():
		if not frappe.db.exists("Page", page_name):
			continue
		page = frappe.get_doc("Page", page_name)
		if {row.role for row in page.roles} != required_roles:
			page.set("roles", [{"role": role} for role in sorted(required_roles)])
			page.save(ignore_permissions=True)


def ensure_management_access():
	ensure_management_roles()
	ensure_management_role_profiles()
	ensure_management_document_permissions()
	ensure_management_page_roles()


def migrate_legacy_production_supervisor_roles() -> list[str]:
	"""Replace the retired split supervisor role on users and role profiles."""
	ensure_management_roles()
	if not frappe.db.table_exists("Has Role"):
		return []

	targets = frappe.get_all(
		"Has Role",
		filters={
			"parenttype": ("in", ("User", "Role Profile")),
			"role": SUPERVISOR_ROLE,
		},
		fields=["parent", "parenttype"],
		limit=0,
	)
	migrated = []
	for target in sorted({(row.parenttype, row.parent) for row in targets}):
		parenttype, parent = target
		if not frappe.db.exists(parenttype, parent):
			continue
		doc = frappe.get_doc(parenttype, parent)
		roles = {row.role for row in doc.get("roles") or []}
		if PRODUCTION_MANAGER_ROLE not in roles:
			doc.append("roles", {"role": PRODUCTION_MANAGER_ROLE})
		for role_row in list(doc.get("roles") or []):
			if role_row.role == SUPERVISOR_ROLE:
				doc.roles.remove(role_row)
		doc.save(ignore_permissions=True)
		if parenttype == "User":
			frappe.clear_cache(user=parent)
		migrated.append(parent)
	return migrated


def is_administrator(user: str | None = None) -> bool:
	return (user or frappe.session.user) == "Administrator"


def has_owner_access(user: str | None = None) -> bool:
	return user_has_capability(CAPABILITY_EXECUTIVE_DASHBOARD, user)


def require_owner_access():
	if not has_owner_access():
		frappe.throw(_("Only the business-owner role can view the executive dashboard."), frappe.PermissionError)


def has_access_management(user: str | None = None) -> bool:
	return user_has_capability(CAPABILITY_ACCESS_MANAGEMENT, user)


def can_manage_sensitive_roles(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return is_administrator(user) or SYSTEM_MANAGER_ROLE in set(frappe.get_roles(user))


def require_access_management():
	if not has_access_management():
		frappe.throw(_("You are not permitted to manage Process Simplification access."), frappe.PermissionError)
