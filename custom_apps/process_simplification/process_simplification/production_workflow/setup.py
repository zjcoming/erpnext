from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def ensure_production_workflow_fields():
	"""Install the small amount of traceability needed by the guided workflow."""
	create_custom_fields(
		{
			"Company": [
				{
					"fieldname": "custom_default_semi_finished_warehouse",
					"label": "默认半成品仓",
					"fieldtype": "Link",
					"options": "Warehouse",
					"ignore_user_permissions": 1,
					"insert_after": "default_fg_warehouse",
					"description": "可选。留空时半成品与原料共用来源仓；设置后，新建快捷生产计划的半成品完工入库和领用使用此仓。修改不改变已有工单。",
				},
				{
					"fieldname": "custom_material_rework_warehouse",
					"label": "待返工仓",
					"fieldtype": "Link",
					"options": "Warehouse",
					"ignore_user_permissions": 1,
					"insert_after": "custom_material_quarantine_warehouse",
					"description": "待返工物料独立存放；返工必须关联有效 BOM 与返工工单。",
				},
				{
					"fieldname": "custom_material_quarantine_warehouse",
					"label": "生产异常待检隔离仓",
					"fieldtype": "Link",
					"options": "Warehouse",
					"ignore_user_permissions": 1,
					"insert_after": "default_scrap_warehouse",
					"description": "质量异常退料暂存处。请使用独立仓库，不要用作 BOM 的正常发料仓。检验放行后再办理转仓。",
				},
			],
			"Purchase Receipt": [
				{
					"fieldname": "custom_material_handling_request",
					"label": "物料处理申请",
					"fieldtype": "Link",
					"options": "Material Handling Request",
					"read_only": 1,
					"no_copy": 1,
				},
			],
			"Purchase Receipt Item": [
				{
					"fieldname": "custom_material_source_detail",
					"label": "异常来源明细",
					"fieldtype": "Data",
					"read_only": 1,
					"no_copy": 1,
				},
			],
			"Stock Entry Detail": [
				{
					"fieldname": "custom_material_source_detail",
					"label": "异常来源明细",
					"fieldtype": "Data",
					"read_only": 1,
					"no_copy": 1,
				},
				{
					"fieldname": "custom_return_source_warehouse",
					"label": "原发料仓",
					"fieldtype": "Link",
					"options": "Warehouse",
					"read_only": 1,
					"no_copy": 1,
				},
			],
			"Stock Entry": [
				{
					"fieldname": "custom_material_handling_request",
					"label": "物料处理申请",
					"fieldtype": "Link",
					"options": "Material Handling Request",
					"read_only": 1,
					"no_copy": 1,
				},
				{
					"fieldname": "custom_return_source_warehouse",
					"label": "退料对应的原发料仓",
					"fieldtype": "Link",
					"options": "Warehouse",
					"read_only": 1,
					"no_copy": 1,
					"insert_after": "work_order",
				},
				{
					"fieldname": "custom_process_workflow_action",
					"label": "Process Workflow Action",
					"fieldtype": "Select",
					"options": "\nReceipt Request\nMaterial Issue Request\nReplenishment Receipt",
					"read_only": 1,
					"search_index": 1,
					"no_copy": 1,
					"insert_after": "work_order",
				},
				{
					"fieldname": "custom_process_requested_by",
					"label": "Process Requested By",
					"fieldtype": "Link",
					"options": "User",
					"read_only": 1,
					"no_copy": 1,
					"insert_after": "custom_process_workflow_action",
				},
				{
					"fieldname": "custom_process_coverage_qty",
					"label": "Process Requested Work Order Coverage Qty",
					"fieldtype": "Float",
					"precision": "6",
					"read_only": 1,
					"no_copy": 1,
					"insert_after": "custom_process_requested_by",
				},
			],
			"Material Request": [
				{
					"fieldname": "custom_replenishes_work_order",
					"label": "Replenishes Work Order",
					"fieldtype": "Link",
					"options": "Work Order",
					"read_only": 1,
					"search_index": 1,
					"no_copy": 1,
					"insert_after": "material_request_type",
				},
				{
					"fieldname": "custom_replenishes_work_order_item",
					"label": "Replenishes Work Order Item",
					"fieldtype": "Data",
					"read_only": 1,
					"search_index": 1,
					"no_copy": 1,
					"insert_after": "custom_replenishes_work_order",
				},
			],
			"Work Order": [
				{
					"fieldname": "custom_semi_finished_warehouse",
					"label": "计划半成品仓",
					"fieldtype": "Link",
					"options": "Warehouse",
					"read_only": 1,
					"no_copy": 1,
					"insert_after": "fg_warehouse",
					"description": "创建快捷生产计划时确定的半成品仓。留空配置时记录当次来源仓，后续公司配置变化不改变本工单。",
				},
				{
					"fieldname": "custom_replenishes_work_order",
					"label": "Replenishes Work Order",
					"fieldtype": "Link",
					"options": "Work Order",
					"read_only": 1,
					"search_index": 1,
					"no_copy": 1,
					"insert_after": "production_plan",
				},
				{
					"fieldname": "custom_replenishes_work_order_item",
					"label": "Replenishes Work Order Item",
					"fieldtype": "Data",
					"read_only": 1,
					"search_index": 1,
					"no_copy": 1,
					"insert_after": "custom_replenishes_work_order",
				},
			],
		},
		update=True,
	)
	for doctype in ("Company", "Stock Entry", "Material Request", "Work Order"):
		frappe.clear_cache(doctype=doctype)
