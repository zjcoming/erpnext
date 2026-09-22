"""Read-only first-use checks using stored settings, without seeding test data."""

import frappe
from frappe import _
from frappe.utils import cint
from erpnext import get_default_company

from process_simplification.api.setup import validate_setup
from process_simplification.defaults import (
	SEMI_FINISHED_WAREHOUSE_FIELD,
	semi_finished_warehouse_error_message,
	semi_finished_warehouse_configuration_error,
)


@frappe.whitelist()
def get_status(company=None):
	frappe.has_permission("Process Simplification Settings", "read", throw=True)
	if frappe.session.user != "Administrator" and not set(frappe.get_roles()).intersection(
		{"System Manager", "Process Simplification Owner"}
	):
		frappe.throw(_("请由系统管理员或工厂负责人检查开用前配置。"), frappe.PermissionError)
	company = company or get_default_company()
	if company:
		frappe.has_permission("Company", "read", doc=company, throw=True)
	base = validate_setup(company=company)
	checks = []

	def add(key, label, value, status, detail, doctype, name=None):
		checks.append(dict(key=key, label=label, value=value, status=status, detail=detail,
			doctype=doctype, name=name,
			can_open=bool(frappe.has_permission(doctype, "read", doc=name)),
			can_configure=bool(frappe.has_permission(doctype, "write", doc=name))))

	add("company", "核对公司", company or "未选择", "ok" if company else "error",
		"核对本公司的币种、会计年度和默认设置。" if company else "请先完成原生初始化向导并设置默认公司。",
		"Company", company)
	if company:
		company_doc = frappe.get_doc("Company", company)
		warehouses = (
			("source_warehouse", "原料来源仓", frappe.db.get_single_value("Stock Settings", "default_warehouse"), "Stock Settings", "Stock Settings"),
			("wip_warehouse", "在制品仓", company_doc.default_wip_warehouse, "Company", company),
			("fg_warehouse", "成品仓", company_doc.default_fg_warehouse, "Company", company),
		)
		for key, label, warehouse, doctype, name in warehouses:
			row = frappe.db.get_value("Warehouse", warehouse,
				["company", "is_group", "disabled"], as_dict=True) if warehouse else None
			valid = bool(row and row.company == company and not row.is_group and not row.disabled)
			if key == "source_warehouse" and (not warehouse or (row and row.company != company)):
				resolved = (base.get("defaults") or {}).get("source_warehouse")
				resolved_row = frappe.db.get_value("Warehouse", resolved,
					["company", "is_group", "disabled"], as_dict=True) if resolved else None
				if resolved_row and resolved_row.company == company and not resolved_row.is_group and not resolved_row.disabled:
					add(key, label, resolved, "warning",
						"当前使用按本公司匹配的来源仓，请管理员核对并在工单确认。库存设置的默认仓是全局值，不要为不同公司反复切换。",
						doctype, name)
					continue
			add(key, label, warehouse or "未明确设置", "ok" if valid else "error",
				"已明确设置为本公司启用的末级仓库。" if valid else
				"请明确选择本公司启用的末级仓库；自动按仓库名称查找不计为配置完成。",
				doctype, name)
		semi_warehouse = company_doc.get(SEMI_FINISHED_WAREHOUSE_FIELD)
		semi_error = semi_finished_warehouse_configuration_error(
			semi_warehouse, company, company_defaults=company_doc
		) if semi_warehouse else None
		semi_error = semi_error or (base.get("defaults") or {}).get("semi_finished_warehouse_error")
		add("semi_finished_warehouse", "默认半成品仓（可选）", semi_warehouse or "与原料共用来源仓",
			"error" if semi_error else "ok" if semi_warehouse else "info",
			semi_finished_warehouse_error_message(semi_warehouse, semi_error) if semi_error else
			"在公司 → 库存与生产 → 默认半成品仓调整。新建计划的半成品入库和领用使用此仓；已有工单保持原仓库。请给生产和库房账号配置相应仓库范围。" if semi_warehouse else
			"留空沿用现有共仓流程；需要独立盘点、保管或收发时，打开公司 → 库存与生产 → 默认半成品仓设置。",
			"Company", company)

	stock = frappe.get_single("Stock Settings")
	add("enable_stock_reservation", "启用库存预留", "已开启" if stock.enable_stock_reservation else "未开启",
		"ok" if stock.enable_stock_reservation else "error",
		"订单分配、生产领料和补产需要启用库存预留；保存后重新检查。", "Stock Settings", "Stock Settings")
	add("auto_reserve_stock", "自动预留库存", "已开启" if stock.auto_reserve_stock else "已关闭", "info",
		"初期建议关闭，由工作台按业务动作预留；与启用库存预留是两个独立开关。", "Stock Settings", "Stock Settings")
	add("allow_negative_stock", "允许负库存", "已开启" if stock.allow_negative_stock else "已关闭",
		"error" if stock.allow_negative_stock else "ok",
		"使用库存预留时应关闭允许负库存。", "Stock Settings", "Stock Settings")
	enforce_logs = cint(frappe.db.get_single_value("Manufacturing Settings", "enforce_time_logs"))
	add("enforce_time_logs", "强制工时日志", "已开启" if enforce_logs else "已关闭",
		"error" if enforce_logs else "ok", "使用恒算简化报工时保持关闭。", "Manufacturing Settings", "Manufacturing Settings")
	phone = cint(frappe.db.get_single_value("System Settings", "allow_login_using_mobile_number"))
	username = cint(frappe.db.get_single_value("System Settings", "allow_login_using_user_name"))
	add("login", "手机号和用户名登录", f"手机号：{'开' if phone else '关'}；用户名：{'开' if username else '关'}", "info",
		"需要时开启，再在用户档案填写唯一手机号和用户名；员工姓名不会自动成为登录名。", "System Settings", "System Settings")
	result = {
		"company": company,
		"checks": checks,
		"configuration_ready": not any(row["status"] == "error" for row in checks),
		"business_setup_ok": base["ok"],
		"next_steps": [
			{"label": "仓库名称和用途", "detail": "沿用默认仓时核对用途；修改名称使用仓库表单入口。", "doctype": "Warehouse"},
			{"label": "物料、单位和期初库存", "detail": "先建一个正确样例，再批量导入；期初按盘点数量和估值录入。", "doctype": "Item"},
			{"label": "工序、工作站和 BOM", "detail": "为待生产产品建立已提交、启用的默认 BOM，并核对工序和用量。", "doctype": "BOM"},
			{"label": "用户、员工和岗位权限", "detail": "每人独立账号，关联员工并确认公司、仓库范围；用普通密码登录验证。", "doctype": "User"},
			{"label": "工价与报工", "detail": "设置有效工价，再用工人账号检查任务、报工和主管审核。", "doctype": "Operation Wage Rate"},
		],
	}
	for row in result["next_steps"]:
		row["can_open"] = bool(frappe.has_permission(row["doctype"], "read"))
		row["can_configure"] = bool(frappe.has_permission(row["doctype"], "create"))
	return result
