"""Permission-scoped RPCs for concentrated material issue and confirmation."""

import frappe

from process_simplification.production_workflow import bulk_materials
from process_simplification.request_transaction import retry_request_transaction


@frappe.whitelist()
def get_material_workbench(view=None, company=None, search=None, start=0):
	return bulk_materials.get_material_workbench(view, company, search, start)


@frappe.whitelist()
def preview_material_requests(work_orders):
	return bulk_materials.preview_material_requests(work_orders)


@frappe.whitelist(methods=["POST"])
@retry_request_transaction
def request_material(work_order, expected_state, qty, allow_partial=0):
	return bulk_materials.request_material(work_order, expected_state, qty, allow_partial)


@frappe.whitelist()
def preview_material_submissions(stock_entries):
	return bulk_materials.preview_material_submissions(stock_entries)


@frappe.whitelist(methods=["POST"])
@retry_request_transaction
def submit_material(stock_entry, expected_state):
	return bulk_materials.submit_material(stock_entry, expected_state)


@frappe.whitelist()
def get_material_outcome(work_order=None, stock_entry=None):
	return bulk_materials.get_material_outcome(work_order, stock_entry)
