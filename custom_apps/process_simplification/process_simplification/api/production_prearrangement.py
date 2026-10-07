"""Small permission-checked RPC surface for inert personnel plans."""

import frappe

from process_simplification.production_reporting import prearrangement
from process_simplification.request_transaction import retry_request_transaction


@frappe.whitelist()
def get_plan_context(job_cards):
	return prearrangement.get_plan_context(job_cards)


@frappe.whitelist(methods=["POST"])
@retry_request_transaction
def save_plan(job_card, allocations, expected_qty, expected_revision=0, supervisor=None,
	expected_work_order=None, expected_operation_id=None):
	return prearrangement.save_plan(job_card, allocations, expected_qty, expected_revision, supervisor,
		expected_work_order=expected_work_order, expected_operation_id=expected_operation_id)


@frappe.whitelist(methods=["POST"])
@retry_request_transaction
def cancel_plan(job_card, expected_revision):
	return prearrangement.cancel_plan(job_card, expected_revision)


@frappe.whitelist()
def get_my_planned_tasks():
	return prearrangement.get_my_planned_tasks()


@frappe.whitelist(methods=["POST"])
@retry_request_transaction
def start_planned_task(plan, request_id, expected_revision):
	return prearrangement.start_planned_task(plan, request_id, expected_revision)


@frappe.whitelist()
def get_worker_defaults(company, operation, supervisor=None):
	return prearrangement.get_worker_defaults(company, operation, supervisor)


@frappe.whitelist(methods=["POST"])
@retry_request_transaction
def save_worker_defaults(company, operation, workers, expected_revision=0, supervisor=None):
	return prearrangement.save_worker_defaults(company, operation, workers, expected_revision, supervisor)
