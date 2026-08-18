from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt


def _tables_ready() -> bool:
	return frappe.db.table_exists("Job Card Worker Assignment") and frappe.db.table_exists(
		"Job Card Work Report"
	)


def _assignment_count(job_card: str) -> int:
	return len(_assignment_rows(job_card, fields=["name"]))


def _assignment_rows(job_card: str, *, fields: list[str]):
	assignment = frappe.qb.DocType("Job Card Worker Assignment")
	query = frappe.qb.from_(assignment).select(*(assignment[field] for field in fields)).where(
		assignment.job_card == job_card
	)
	return query.for_update().run(as_dict=True)


def _report_rows(job_card: str, status: str | None = None, *, exclude_report: str | None = None):
	report = frappe.qb.DocType("Job Card Work Report")
	condition = report.job_card == job_card
	if status:
		condition &= report.status == status
	if exclude_report:
		condition &= report.name != exclude_report
	return (
		frappe.qb.from_(report)
		.select(
			report.name,
			report.job_card,
			report.employee,
			report.completed_qty,
			report.job_card_time_log,
			report.status,
		)
		.where(condition)
		.for_update()
	).run(as_dict=True)


def _approved_reports(job_card: str):
	return _report_rows(job_card, "Approved")


def _pending_qty(job_card: str, exclude_report: str | None = None) -> float:
	return sum(
		flt(row.completed_qty)
		for row in _report_rows(job_card, "Pending Approval", exclude_report=exclude_report)
	)


def _is_managed(doc) -> bool:
	return bool(doc.get("custom_worker_reporting_enabled") or _assignment_count(doc.name))


def _assert_configuration(doc):
	if doc.get("is_corrective_job_card"):
		frappe.throw(_("Corrective Job Cards are not supported by simplified worker reporting."))
	if doc.get("sub_operations"):
		frappe.throw(_("Job Cards with sub-operations are not supported by simplified worker reporting."))
	if doc.get("track_semi_finished_goods") or doc.get("is_subcontracted"):
		frappe.throw(_("Semi-finished or subcontracted Job Cards are not supported in worker reporting v1."))
	if frappe.db.get_single_value(
		"Manufacturing Settings", "enforce_time_logs", for_update=True
	):
		frappe.throw(
			_(
				"Manufacturing Settings requires From/To Time. "
				"It is incompatible with worker-entered wage minutes."
			)
		)


def _expected_reports(doc):
	expected = {
		report.name: frappe._dict(
			name=report.name,
			job_card=report.job_card,
			employee=report.employee,
			completed_qty=flt(report.completed_qty),
			job_card_time_log=report.job_card_time_log,
			in_flight=False,
		)
		for report in _approved_reports(doc.name)
	}
	in_flight = getattr(doc.flags, "worker_reporting_approval", None)
	if in_flight:
		report = frappe.db.get_value(
			"Job Card Work Report",
			in_flight,
			["name", "job_card", "employee", "completed_qty", "status"],
			as_dict=True,
			for_update=True,
		)
		if not report or report.status != "Pending Approval" or report.job_card != doc.name:
			frappe.throw(_("The in-flight approval report is no longer valid."))
		expected[report.name] = frappe._dict(
			name=report.name,
			job_card=report.job_card,
			employee=report.employee,
			completed_qty=flt(report.completed_qty),
			job_card_time_log=None,
			in_flight=True,
		)
	return expected, in_flight


def _validate_time_log_backlinks(doc):
	expected, in_flight = _expected_reports(doc)
	actual = {}
	for row in doc.get("time_logs") or []:
		report_name = row.get("custom_job_card_work_report")
		if not report_name:
			frappe.throw(
				_("Every quantity row on a worker-reporting Job Card must come from an approved work report.")
			)
		if report_name in actual:
			frappe.throw(_("Work report {0} is linked to more than one Job Card row.").format(report_name))
		actual[report_name] = row

	if set(actual) != set(expected):
		frappe.throw(_("Job Card quantity rows and approved work reports no longer match one-to-one."))
	for report_name, report in expected.items():
		row = actual[report_name]
		if flt(row.completed_qty, 6) != flt(report.completed_qty, 6):
			frappe.throw(_("Job Card quantity for report {0} was changed.").format(report_name))
		if row.employee != report.employee or row.get("custom_reported_employee") != report.employee:
			frappe.throw(_("Worker attribution changed for report {0}.").format(report_name))
		if row.get("from_time") or row.get("to_time") or flt(row.get("time_in_mins")):
			frappe.throw(_("Worker-reported wage minutes cannot be stored as synthetic Job Card time."))
		if report.job_card_time_log and row.name != report.job_card_time_log:
			frappe.throw(_("The stored Job Card row link changed for report {0}.").format(report_name))
	return expected, in_flight


def _validate_managed_job_card(doc, *, reset_process_loss: bool):
	_assert_configuration(doc)
	doc.custom_worker_reporting_enabled = 1
	if reset_process_loss:
		# ERPNext's controller has just derived the incomplete balance as process
		# loss. This flow continues on the same Job Card, so that balance is not loss.
		doc.process_loss_qty = 0
	expected, in_flight = _validate_time_log_backlinks(doc)
	precision = doc.precision("total_completed_qty")
	expected_qty = sum(flt(report.completed_qty) for report in expected.values())
	if flt(doc.total_completed_qty, precision) != flt(expected_qty, precision):
		frappe.throw(
			_("Job Card completed quantity must equal approved work reports ({0}).").format(
				flt(expected_qty, precision)
			)
		)
	if flt(doc.pending_qty, precision):
		frappe.throw(
			_(
				"Job Card pending quantity must stay zero while reports continue on the same card."
			)
		)
	if flt(doc.process_loss_qty, precision):
		frappe.throw(_("Process loss is not supported on a worker-reporting Job Card."))
	reserved = _pending_qty(doc.name, exclude_report=in_flight)
	if flt(doc.total_completed_qty, precision) + flt(reserved, precision) > flt(
		doc.for_quantity, precision
	):
		frappe.throw(_("Approved production plus pending work reports exceeds the Job Card quantity."))
	for snapshot in _assignment_rows(
		doc.name,
		fields=["work_order", "company", "operation", "operation_id", "job_card_qty"],
	):
		for fieldname in ("work_order", "company", "operation", "operation_id"):
			if snapshot.get(fieldname) != doc.get(fieldname):
				frappe.throw(_("Job Card identity cannot change after worker assignment."))
		if flt(snapshot.job_card_qty, precision) != flt(doc.for_quantity, precision):
			frappe.throw(_("Job Card quantity cannot change after worker assignment."))
	return expected


def before_save(doc, method=None):
	if doc.is_new() and doc.get("custom_worker_reporting_enabled") and not _assignment_count(
		doc.name or ""
	):
		doc.custom_worker_reporting_enabled = 0
		doc.custom_worker_reporting_supervisor = None
		return
	if not _tables_ready() or not _is_managed(doc):
		return
	_validate_managed_job_card(doc, reset_process_loss=True)


def before_submit(doc, method=None):
	if not _tables_ready() or not _is_managed(doc):
		return
	_validate_managed_job_card(doc, reset_process_loss=False)
	in_flight = getattr(doc.flags, "worker_reporting_approval", None)
	pending = len(_report_rows(doc.name, "Pending Approval", exclude_report=in_flight))
	if pending:
		frappe.throw(_("Cannot submit Job Card while {0} work reports await review.").format(pending))
	precision = doc.precision("total_completed_qty")
	if flt(doc.total_completed_qty, precision) != flt(doc.for_quantity, precision):
		frappe.throw(_("Approve the full Job Card quantity before submission."))
	if flt(doc.pending_qty, precision) or flt(doc.process_loss_qty, precision):
		frappe.throw(_("Worker reporting requires zero pending quantity and zero process loss."))


def on_submit(doc, method=None):
	if not _tables_ready() or not _is_managed(doc):
		return
	frappe.db.set_value(
		"Job Card Worker Assignment",
		{"job_card": doc.name, "status": "Active"},
		"status",
		"Completed",
		update_modified=False,
	)


def before_cancel(doc, method=None):
	if not _tables_ready() or not _is_managed(doc):
		return
	if any(
		row.status in {"Pending Approval", "Approved"}
		for row in _report_rows(doc.name)
	):
		frappe.throw(
			_(
				"This Job Card has pending or approved work reports. "
				"Cancellation is blocked to protect production and wage history."
			)
		)


def before_discard(doc, method=None):
	if not _tables_ready() or not _is_managed(doc):
		return
	if _assignment_count(doc.name) or _report_rows(doc.name):
		frappe.throw(
			_(
				"Remove report-free worker assignments through Report Review before discarding this Job Card. "
				"A Job Card with any report history cannot be discarded."
			)
		)


def before_update_after_submit(doc, method=None):
	if not _tables_ready() or not _is_managed(doc):
		return
	_validate_managed_job_card(doc, reset_process_loss=False)
