from __future__ import annotations

import frappe
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
			}
		)

	company_defaults = frappe.get_cached_value(
		"Company",
		company,
		["default_wip_warehouse", "default_fg_warehouse"],
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

	return frappe._dict(
		{
			"company": company,
			"source_warehouse": source_warehouse,
			"wip_warehouse": wip_warehouse,
			"fg_warehouse": fg_warehouse,
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
