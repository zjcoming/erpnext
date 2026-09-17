"""Choose a role's entry page without changing its permissions or sidebar order."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from process_simplification.management_access import (
	ACCESS_MANAGER_ROLE,
	OWNER_ROLE,
	PAGE_CAPABILITIES,
	PRODUCTION_MANAGER_ROLE,
	SALES_OPERATOR_ROLE,
	SYSTEM_MANAGER_ROLE,
	WAGE_MANAGER_ROLE,
	WAREHOUSE_OPERATOR_ROLE,
	WORKER_ROLE,
	user_has_capability,
)

SETTINGS_DOCTYPE = "Process Simplification Settings"
LANDING_FIELDS = ("enable_role_landing", "role_landing_pages")
ROLE_LABELS = {
	"老板": OWNER_ROLE,
	"生产主管": PRODUCTION_MANAGER_ROLE,
	"库房人员": WAREHOUSE_OPERATOR_ROLE,
	"销售人员": SALES_OPERATOR_ROLE,
	"工资核算人员": WAGE_MANAGER_ROLE,
	"流水线工人": WORKER_ROLE,
	"APP 权限管理员": ACCESS_MANAGER_ROLE,
	"系统管理员": SYSTEM_MANAGER_ROLE,
}
# Only existing internal destinations can be selected; never accept an arbitrary URL.
DESTINATIONS = {
	"经营总览": ("Page", "executive-dashboard", "executive-dashboard"),
	"订单工作台": ("Page", "order-workbench", "order-workbench"),
	"快速开单": ("Page", "quick-sales-order", "quick-sales-order"),
	"生产计划中心": ("Page", "production-workbench", "production-workbench"),
	"报工审核": ("Page", "production-report-review", "production-report-review"),
	"异常审核": ("Page", "production-exception-review", "production-exception-review"),
	"库房工作台": ("Page", "warehouse-workbench", "warehouse-workbench"),
	"缺料采购": ("Page", "shortage-purchase-planning", "shortage-purchase-planning"),
	"供应商分配": ("Page", "purchase-supplier-allocation", "purchase-supplier-allocation"),
	"我的任务": ("Page", "my-production-reporting", "my-production-reporting"),
	"正在做": ("Page", "active-production-work", "active-production-work"),
	"我的记录": ("Page", "production-report-history", "production-report-history"),
	"工资总览": ("DocType", "Monthly Worker Wage Summary", "monthly-worker-wage-summary"),
	"计价规则": ("DocType", "Operation Wage Rate", "operation-wage-rate"),
	"权限管理": ("Page", "process-access-management", "process-access-management"),
	"系统设置": ("DocType", SETTINGS_DOCTYPE, "process-simplification-settings"),
	"工作台": ("Workspace", "process-simplification", "process-simplification"),
}
DEFAULT_PAGES = (
	("老板", "经营总览"),
	("生产主管", "生产计划中心"),
	("库房人员", "库房工作台"),
	("销售人员", "订单工作台"),
	("工资核算人员", "工资总览"),
	("流水线工人", "我的任务"),
	("APP 权限管理员", "权限管理"),
	("系统管理员", "系统设置"),
)


def ensure_defaults(*, new_install=False):
	"""Seed fresh installs; upgrades retain deliberately empty or disabled choices.

	Frappe can persist Single defaults before after_install, so field existence
	alone cannot distinguish a fresh installation from an existing configuration.
	"""
	if not new_install and "enable_role_landing" in frappe.db.get_singles_dict(SETTINGS_DOCTYPE):
		return
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	settings.enable_role_landing = 1
	if not settings.get("role_landing_pages"):
		for role, page in DEFAULT_PAGES:
			settings.append("role_landing_pages", {"role": role, "landing_page": page, "enabled": 1})
	settings.save(ignore_permissions=True)


def validate_settings(settings):
	seen = set()
	for index, row in enumerate(settings.get("role_landing_pages") or [], 1):
		row.idx = index
		if row.role not in ROLE_LABELS or row.landing_page not in DESTINATIONS:
			frappe.throw(_("第 {0} 行：请选择有效的角色和默认首页。").format(row.idx))
		if row.role in seen:
			frappe.throw(_("角色“{0}”只能配置一条默认首页，请通过拖动行调整优先级。").format(row.role))
		seen.add(row.role)


def resolve_landing(settings, roles, allowed_links):
	"""The first enabled, matching, permitted row wins; no match preserves native entry."""
	if not cint(settings.get("enable_role_landing")):
		return None
	roles = set(roles)
	for row in settings.get("role_landing_pages") or []:
		if not cint(row.get("enabled")) or ROLE_LABELS.get(row.get("role")) not in roles:
			continue
		destination = DESTINATIONS.get(row.get("landing_page"))
		if not destination:
			continue
		link_type, name, route = destination
		if (link_type, name) not in allowed_links:
			continue
		# Page visibility can also include legacy native roles; the business API's
		# capability remains authoritative, especially for the executive dashboard.
		if name in PAGE_CAPABILITIES and not user_has_capability(PAGE_CAPABILITIES[name]):
			continue
		return {"route": route, "page": row.get("landing_page")}
	return None


def boot_session(bootinfo):
	if frappe.session.user == "Guest":
		return
	settings = frappe.get_cached_doc(SETTINGS_DOCTYPE)
	allowed_links = {
		(item.get("link_type"), item.get("link_to"))
		for sidebar in (bootinfo.get("workspace_sidebar_item") or {}).values()
		if sidebar.get("app") == "process_simplification"
		for item in sidebar.get("items") or []
		if item.get("type") == "Link"
	}
	landing = resolve_landing(settings, frappe.get_roles(), allowed_links)
	bootinfo.process_role_landing = landing
	if not landing:
		return
	# Keep the shared Workspace Sidebar untouched, including the manual 工作台 link.
	# Native Desktop resolves External links directly, including same-origin paths.
	for icon in bootinfo.get("desktop_icons") or []:
		if icon.get("app") == "process_simplification" and icon.get("icon_type") == "App":
			icon.update(link_type="External", link=f"/desk/{landing['route']}?sidebar=Process%20Simplification")
