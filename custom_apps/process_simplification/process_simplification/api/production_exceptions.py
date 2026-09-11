from __future__ import annotations

import frappe

from process_simplification.production_exceptions import service


@frappe.whitelist()
def get_exception_options(assignment):
	return service.get_exception_options(assignment)


@frappe.whitelist()
def get_my_requests(limit=100):
	return service.get_my_requests(limit=limit)


@frappe.whitelist(methods=["POST"])
def submit_exception(assignment, request_type, qty, cause, reason, request_key, material_key=None, material_action="Continue"):
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
def approve_exception(request):
	return service.approve_exception(request)


@frappe.whitelist(methods=["POST"])
def reject_exception(request, reason):
	return service.reject_exception(request, reason)


@frappe.whitelist(methods=["POST"])
def withdraw_exception(request, reason):
	return service.withdraw_exception(request, reason)
