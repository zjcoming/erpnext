from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def ensure_production_workflow_fields():
	"""Install the small amount of traceability needed by the guided workflow."""
	create_custom_fields(
		{
			"Company": [
				{
					"fieldname": "custom_material_quarantine_warehouse",
					"label": "生产异常待检隔离仓",
					"fieldtype": "Link", "options": "Warehouse",
					"insert_after": "default_scrap_warehouse",
					"description": "质量异常退料暂存处。请使用独立仓库，不要用作 BOM 的正常发料仓。检验放行后再办理转仓。",
				},
			],
			"Stock Entry": [
				{
					"fieldname": "custom_return_source_warehouse",
					"label": "退料对应的原发料仓",
					"fieldtype": "Link", "options": "Warehouse",
					"read_only": 1, "no_copy": 1,
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
	for doctype in ("Stock Entry", "Material Request", "Work Order"):
		frappe.clear_cache(doctype=doctype)
