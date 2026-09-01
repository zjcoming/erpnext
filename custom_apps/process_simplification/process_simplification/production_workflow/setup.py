from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def ensure_production_workflow_fields():
	"""Install the small amount of traceability needed by the guided workflow."""
	create_custom_fields(
		{
			"Stock Entry": [
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
