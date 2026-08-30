from __future__ import annotations

import frappe

from process_simplification.production_reporting import service, summary


@frappe.whitelist()
def get_my_dashboard():
	return service.get_worker_dashboard()


@frappe.whitelist()
def get_my_report_history(
	page=1,
	page_length=20,
	status=None,
	operation=None,
	work_order=None,
	job_card=None,
	from_date=None,
	to_date=None,
):
	return service.get_worker_report_history(
		page=page,
		page_length=page_length,
		status=status,
		operation=operation,
		work_order=work_order,
		job_card=job_card,
		from_date=from_date,
		to_date=to_date,
	)


@frappe.whitelist(methods=["POST"])
def start_work_session(assignment, request_id=None, wage_type=None):
	# Keep the old argument temporarily so a cached browser can still start; the
	# wage method is intentionally chosen only when the worker submits the report.
	return service.start_work_session(assignment, request_id)


@frappe.whitelist(methods=["POST"])
def pause_work_session(report, request_id=None):
	return service.pause_work_session(report, request_id)


@frappe.whitelist(methods=["POST"])
def resume_work_session(report, request_id=None):
	return service.resume_work_session(report, request_id)


@frappe.whitelist(methods=["POST"])
def finish_work_session(
	report,
	completed_qty,
	request_id=None,
	reported_minutes=None,
	wage_type=None,
):
	return service.finish_work_session(
		report,
		completed_qty,
		request_id,
		reported_minutes=reported_minutes,
		wage_type=wage_type,
	)


@frappe.whitelist(methods=["POST"])
def cancel_work_session(report):
	return service.cancel_work_session(report)


@frappe.whitelist()
def get_review_dashboard(pending_page=1, assignment_page=1, processed_page=1, page_length=20):
	return service.get_review_dashboard(
		pending_page=pending_page,
		assignment_page=assignment_page,
		processed_page=processed_page,
		page_length=page_length,
	)


@frappe.whitelist()
def get_review_history(
	page=1,
	page_length=20,
	status=None,
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
		employee=employee,
		work_order=work_order,
		job_card=job_card,
		from_date=from_date,
		to_date=to_date,
	)


@frappe.whitelist()
def get_work_order_assignment_context(work_order):
	return service.get_work_order_assignment_context(work_order)


@frappe.whitelist(methods=["POST"])
def assign_worker(job_card, employee, supervisor=None, notes=None):
	return service.assign_worker(job_card, employee, supervisor, notes)


@frappe.whitelist(methods=["POST"])
def unassign_worker(assignment):
	return service.unassign_worker(assignment)


@frappe.whitelist(methods=["POST"])
def approve_work_report(report):
	return service.approve_work_report(report)


@frappe.whitelist(methods=["POST"])
def reject_work_report(report, reason):
	return service.reject_work_report(report, reason)


@frappe.whitelist()
def search_draft_job_cards(doctype=None, txt=None, searchfield=None, start=0, page_len=20, filters=None):
	filters = frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})
	return service.search_draft_job_cards(
		txt=txt,
		start=start,
		page_len=page_len,
		work_order=filters.get("work_order"),
	)


@frappe.whitelist()
def search_assignment_supervisors(
	doctype=None, txt=None, searchfield=None, start=0, page_len=20, filters=None
):
	filters = frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})
	return service.search_assignment_supervisors(
		work_order=filters.get("work_order"),
		txt=txt,
		start=start,
		page_len=page_len,
	)


@frappe.whitelist()
def search_workers(doctype=None, txt=None, searchfield=None, start=0, page_len=20, filters=None):
	filters = frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})
	return service.search_workers(
		job_card=filters.get("job_card"),
		txt=txt,
		start=start,
		page_len=page_len,
	)


@frappe.whitelist()
def search_wage_employees(doctype=None, txt=None, searchfield=None, start=0, page_len=20, filters=None):
	filters = frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})
	return service.search_wage_employees(
		company=filters.get("company"),
		txt=txt,
		start=start,
		page_len=page_len,
	)


@frappe.whitelist(methods=["POST"])
def build_monthly_summaries(company, month_start, employee=None):
	return summary.build_monthly_summaries(company, month_start, employee)


@frappe.whitelist()
def get_wage_management_context():
	return summary.get_wage_management_context()


@frappe.whitelist(methods=["POST"])
def confirm_monthly_summary(summary_name):
	return summary.confirm_monthly_summary(summary_name)
