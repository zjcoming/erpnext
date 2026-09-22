from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint
from erpnext import get_default_company


SOURCE_WAREHOUSE_NAMES = (
	"仓库",
	"原料",
	"原材料",
	"原材料仓",
	"Stores",
	"Raw Material",
	"Raw Materials",
	"PS RM",
)
WIP_WAREHOUSE_NAMES = ("进行中", "在制品", "生产中", "Work In Progress", "WIP", "PS WIP")
FG_WAREHOUSE_NAMES = ("成品", "Finished Goods", "FG", "PS FG")
SEMI_FINISHED_WAREHOUSE_FIELD = "custom_default_semi_finished_warehouse"
SEMI_FINISHED_CONFLICT_FIELDS = (
	"default_wip_warehouse",
	"default_scrap_warehouse",
	"custom_material_quarantine_warehouse",
	"custom_material_rework_warehouse",
)


def warehouse_configuration_error(warehouse: str | None, company: str | None) -> str | None:
	"""Validate an exact configured warehouse without finding a substitute."""
	row = (
		frappe.db.get_value("Warehouse", warehouse, ["company", "is_group", "disabled"], as_dict=True)
		if warehouse else None
	)
	if not row:
		return "warehouse_missing"
	if row.company != company:
		return "warehouse_company_mismatch"
	if cint(row.is_group):
		return "warehouse_is_group"
	if cint(row.disabled):
		return "warehouse_disabled"
	return None


def semi_finished_warehouse_configuration_error(warehouse, company, *, company_defaults=None):
	reason = warehouse_configuration_error(warehouse, company)
	if reason:
		return reason
	company_defaults = (
		company_defaults if company_defaults is not None else frappe.get_cached_value(
			"Company", company, list(SEMI_FINISHED_CONFLICT_FIELDS), as_dict=True
		)
	)
	company_defaults = company_defaults or {}
	wip_warehouse = company_defaults.get("default_wip_warehouse")
	if not wip_warehouse or not warehouse_belongs_to_company(wip_warehouse, company):
		wip_warehouse = find_company_warehouse(company, WIP_WAREHOUSE_NAMES)
	conflicting_warehouses = [company_defaults.get(field) for field in SEMI_FINISHED_CONFLICT_FIELDS]
	conflicting_warehouses.append(wip_warehouse)
	if warehouse in conflicting_warehouses:
		return "warehouse_role_conflict"
	return None


def resolve_semi_finished_warehouse(company, source_warehouse=None, *, defaults=None):
	"""Blank company configuration shares this order's source warehouse."""
	defaults = defaults if defaults is not None else get_company_manufacturing_defaults(company)
	configured = defaults.get("configured_semi_finished_warehouse")
	warehouse = configured or source_warehouse or defaults.get("source_warehouse")
	reason = warehouse_configuration_error(warehouse, company)
	if configured:
		reason = reason or defaults.get("semi_finished_warehouse_error")
	return frappe._dict(
		warehouse=warehouse,
		configured_warehouse=configured,
		can_use=not reason,
		reason=reason,
	)


def semi_finished_warehouse_error_message(warehouse, reason):
	details = {
		"warehouse_missing": _("仓库不存在"),
		"warehouse_company_mismatch": _("仓库不属于本公司"),
		"warehouse_is_group": _("不能使用仓库组"),
		"warehouse_disabled": _("仓库已禁用"),
		"warehouse_role_conflict": _("不能与在制品仓、隔离仓、报废仓或待返工仓共用"),
	}
	return _("默认半成品仓 {0} 无效：{1}。请在公司中选择本公司启用的末级仓库，或清空后沿用原料来源仓。").format(
		warehouse or "", details.get(reason, _("仓库配置有误"))
	)


def validate_company_manufacturing_warehouses(doc, method=None):
	warehouse = doc.get(SEMI_FINISHED_WAREHOUSE_FIELD)
	if not warehouse:
		return
	reason = semi_finished_warehouse_configuration_error(warehouse, doc.name, company_defaults=doc)
	if reason:
		frappe.throw(semi_finished_warehouse_error_message(warehouse, reason))


def warehouse_belongs_to_company(warehouse: str | None, company: str | None) -> bool:
	if not warehouse or not company:
		return False
	return frappe.db.get_value("Warehouse", warehouse, "company") == company


def find_company_warehouse(company: str | None, warehouse_names: tuple[str, ...]) -> str | None:
	if not company:
		return None

	rows = frappe.get_all(
		"Warehouse",
		filters={"company": company, "is_group": 0, "warehouse_name": ["in", warehouse_names]},
		fields=["name", "warehouse_name"],
		ignore_permissions=True,
	)
	by_warehouse_name = {row.warehouse_name: row.name for row in rows}
	for warehouse_name in warehouse_names:
		if by_warehouse_name.get(warehouse_name):
			return by_warehouse_name[warehouse_name]
	return None


def get_company_manufacturing_defaults(company: str | None = None):
	company = company or get_default_company()
	if not company:
		return frappe._dict(
			{
				"company": None,
				"source_warehouse": None,
				"wip_warehouse": None,
				"fg_warehouse": None,
				"configured_semi_finished_warehouse": None,
				"semi_finished_warehouse": None,
				"semi_finished_warehouse_error": None,
			}
		)

	company_defaults = frappe.get_cached_value(
		"Company",
		company,
		[
			"default_wip_warehouse", "default_fg_warehouse", SEMI_FINISHED_WAREHOUSE_FIELD,
			"default_scrap_warehouse", "custom_material_quarantine_warehouse", "custom_material_rework_warehouse",
		],
		as_dict=True,
	)
	# ERPNext v16 keeps the global source warehouse on Stock Settings. Company
	# has WIP/FG warehouse fields, but no default_warehouse field.
	source_warehouse = frappe.db.get_single_value("Stock Settings", "default_warehouse")
	if not warehouse_belongs_to_company(source_warehouse, company):
		source_warehouse = find_company_warehouse(company, SOURCE_WAREHOUSE_NAMES)

	wip_warehouse = company_defaults.default_wip_warehouse if company_defaults else None
	if not warehouse_belongs_to_company(wip_warehouse, company):
		wip_warehouse = find_company_warehouse(company, WIP_WAREHOUSE_NAMES)

	fg_warehouse = company_defaults.default_fg_warehouse if company_defaults else None
	if not warehouse_belongs_to_company(fg_warehouse, company):
		fg_warehouse = find_company_warehouse(company, FG_WAREHOUSE_NAMES)

	configured_semi_finished_warehouse = (
		company_defaults.get(SEMI_FINISHED_WAREHOUSE_FIELD) if company_defaults else None
	)
	return frappe._dict(
		{
			"company": company,
			"source_warehouse": source_warehouse,
			"wip_warehouse": wip_warehouse,
			"fg_warehouse": fg_warehouse,
			"configured_semi_finished_warehouse": configured_semi_finished_warehouse,
			"semi_finished_warehouse": configured_semi_finished_warehouse or source_warehouse,
			"semi_finished_warehouse_error": semi_finished_warehouse_configuration_error(
				configured_semi_finished_warehouse, company, company_defaults=company_defaults
			)
			if configured_semi_finished_warehouse else None,
		}
	)


def configure_company_manufacturing_defaults(company: str | None = None):
	defaults = get_company_manufacturing_defaults(company)
	if not defaults.company:
		return defaults

	current_company_defaults = frappe.get_cached_value(
		"Company",
		defaults.company,
		["default_wip_warehouse", "default_fg_warehouse"],
		as_dict=True,
	)
	current_source_warehouse = frappe.db.get_single_value("Stock Settings", "default_warehouse")
	updates = {}
	if defaults.source_warehouse and not warehouse_belongs_to_company(
		current_source_warehouse,
		defaults.company,
	):
		frappe.db.set_single_value(
			"Stock Settings",
			"default_warehouse",
			defaults.source_warehouse,
			update_modified=False,
		)
	if defaults.wip_warehouse and not warehouse_belongs_to_company(
		current_company_defaults.default_wip_warehouse if current_company_defaults else None,
		defaults.company,
	):
		updates["default_wip_warehouse"] = defaults.wip_warehouse
	if defaults.fg_warehouse and not warehouse_belongs_to_company(
		current_company_defaults.default_fg_warehouse if current_company_defaults else None,
		defaults.company,
	):
		updates["default_fg_warehouse"] = defaults.fg_warehouse

	if updates:
		frappe.db.set_value("Company", defaults.company, updates, update_modified=False)

	frappe.clear_cache()
	return get_company_manufacturing_defaults(defaults.company)
