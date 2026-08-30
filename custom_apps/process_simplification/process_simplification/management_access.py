from __future__ import annotations

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

ROLE_DEFINITIONS = (
	{
		"role": OWNER_ROLE,
		"label": "老板",
		"description": "使用全部业务管理入口和工资管理；不包含工人自助入口或系统管理员能力。",
		"sensitive": True,
	},
	{
		"role": SALES_OPERATOR_ROLE,
		"label": "销售人员",
		"description": "使用快速开单和订单工作台；仍需相应的 ERPNext 销售单据权限。",
		"sensitive": False,
	},
	{
		"role": WAREHOUSE_OPERATOR_ROLE,
		"label": "库房人员",
		"description": "处理库存、收发料、缺料采购和已批准异常的库存落账；不审批工人报工。",
		"sensitive": False,
	},
	{
		"role": PRODUCTION_MANAGER_ROLE,
		"label": "生产车间管理",
		"description": "负责生产计划、派工，并复核本公司范围内的报工和生产异常。",
		"sensitive": False,
	},
	{
		"role": SUPERVISOR_ROLE,
		"label": "生产审核员/班组长",
		"description": "仅审核明确分配给自己的派工、报工和生产异常，可与库房岗位按人员实际职责叠加。",
		"sensitive": False,
	},
	{
		"role": WORKER_ROLE,
		"label": "流水线工人",
		"description": "仅使用本人的派工和报工入口。",
		"sensitive": False,
	},
	{
		"role": WAGE_MANAGER_ROLE,
		"label": "工资核算人员",
		"description": "维护计价规则并核算、确认月度工资；必须另外限制可访问公司。",
		"sensitive": True,
	},
	{
		"role": ACCESS_MANAGER_ROLE,
		"label": "APP 权限管理员",
		"description": "分配普通 APP 岗位；只有系统管理员可以授予老板和权限管理员角色。",
		"sensitive": True,
	},
)

APP_MANAGED_ROLES = tuple(definition["role"] for definition in ROLE_DEFINITIONS)
SENSITIVE_ROLES = {OWNER_ROLE, WAGE_MANAGER_ROLE, ACCESS_MANAGER_ROLE}
APP_NON_WORKER_ROLES = set(APP_MANAGED_ROLES).difference({WORKER_ROLE})
WORKER_INCOMPATIBLE_ROLES = {
	"Manufacturing User",
	"Manufacturing Manager",
	"Shop Floor User",
	"Shop Floor Manager",
	"System Manager",
	*APP_NON_WORKER_ROLES,
}


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


def ensure_management_access():
	ensure_management_roles()
	ensure_management_document_permissions()


def is_administrator(user: str | None = None) -> bool:
	return (user or frappe.session.user) == "Administrator"


def has_owner_access(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return is_administrator(user) or OWNER_ROLE in set(frappe.get_roles(user))


def require_owner_access():
	if not has_owner_access():
		frappe.throw(_("Only the business-owner role can view the executive dashboard."), frappe.PermissionError)


def has_access_management(user: str | None = None) -> bool:
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	return is_administrator(user) or bool({SYSTEM_MANAGER_ROLE, ACCESS_MANAGER_ROLE}.intersection(roles))


def can_manage_sensitive_roles(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return is_administrator(user) or SYSTEM_MANAGER_ROLE in set(frappe.get_roles(user))


def require_access_management():
	if not has_access_management():
		frappe.throw(_("You are not permitted to manage Process Simplification access."), frappe.PermissionError)
