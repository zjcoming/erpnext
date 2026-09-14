from __future__ import annotations

import frappe

from process_simplification.production_exceptions import service
from process_simplification.request_transaction import retry_request_transaction


@frappe.whitelist()
def get_exception_options(assignment):
	return service.get_exception_options(assignment)


@frappe.whitelist()
def get_my_requests(limit=100):
	return service.get_my_requests(limit=limit)


@frappe.whitelist(methods=["POST"])
@retry_request_transaction
def submit_exception(
	assignment, request_type, qty, cause, reason, request_key, material_key=None, material_action="Continue"
):
	return service.submit_exception(
		assignment=assignment,
		request_type=request_type,
		qty=qty,
		cause=cause,
		reason=reason,
		request_key=request_key,
		material_key=material_key,
		material_action=material_action,
	)


@frappe.whitelist()
def get_review_dashboard(limit=200):
	return service.get_review_dashboard(limit=limit)


@frappe.whitelist()
def get_review_history(
	page=1,
	page_length=20,
	status=None,
	request_type=None,
	employee=None,
	work_order=None,
	job_card=None,
	from_date=None,
	to_date=None,
):
	return service.get_review_history(
		page=page,
		page_length=page_length,
		status=status,
		request_type=request_type,
		employee=employee,
		work_order=work_order,
		job_card=job_card,
		from_date=from_date,
		to_date=to_date,
	)


@frappe.whitelist(methods=["POST"])
@retry_request_transaction
def approve_exception(request, material_action=None):
	return service.approve_exception(request, material_action=material_action)


@frappe.whitelist(methods=["POST"])
@retry_request_transaction
def reject_exception(request, reason):
	return service.reject_exception(request, reason)


@frappe.whitelist(methods=["POST"])
@retry_request_transaction
def withdraw_exception(request, reason):
	return service.withdraw_exception(request, reason)


@frappe.whitelist()
def get_material_handling_dashboard():
	from process_simplification.production_exceptions.handling import dashboard

	return dashboard()


@frappe.whitelist()
def get_closeout_options(work_order):
	from process_simplification.production_exceptions.handling import closeout_options

	return closeout_options(work_order)


@frappe.whitelist()
def get_disposition_options(source_stock_entry):
	from process_simplification.production_exceptions.handling import disposition_options

	return disposition_options(source_stock_entry)


@frappe.whitelist(methods=["POST"])
@retry_request_transaction
def create_material_handling(
	action,
	items,
	request_key,
	work_order=None,
	source_stock_entry=None,
	reason=None,
	purchase_receipt=None,
	rework_bom=None,
):
	from process_simplification.production_exceptions.handling import create_request

	return create_request(
		action, items, request_key, work_order, source_stock_entry, reason, purchase_receipt, rework_bom
	)


@frappe.whitelist(methods=["POST"])
@retry_request_transaction
def approve_material_handling(request):
	from process_simplification.production_exceptions.handling import approve

	return approve(request)


@frappe.whitelist(methods=["POST"])
@retry_request_transaction
def withdraw_material_handling(request, reason, reject=False):
	from process_simplification.production_exceptions.handling import withdraw
	from frappe.utils import cint

	return withdraw(request, reason, bool(cint(reject)))


@frappe.whitelist(methods=["POST"])
@retry_request_transaction
def post_material_handling(request):
	from process_simplification.production_exceptions.handling import post

	return post(request)


@frappe.whitelist()
def get_handling_receipt_options(receipt, company):
	from process_simplification.production_exceptions.handling_followup import receipt_options

	source = receipt_options(receipt, company)
	return [dict(name=r.name, idx=r.idx, item_code=r.item_code, qty=r.qty, uom=r.uom) for r in source.items]


@frappe.whitelist()
def get_handling_posting_preview(request):
	from process_simplification.production_exceptions.handling import posting_preview

	return posting_preview(request)
