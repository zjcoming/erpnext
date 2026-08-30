from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, get_datetime


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
			report.actual_start_time,
			report.actual_end_time,
			report.actual_minutes,
			report.job_card_time_log,
			report.status,
		)
		.where(condition)
		.for_update()
	).run(as_dict=True)


def _approved_reports(job_card: str):
	return _report_rows(job_card, "Approved")


def _report_time_segments(report) -> list:
	segments = frappe.get_all(
		"Job Card Work Report Time Segment",
		filters={"parent": report.name, "parenttype": "Job Card Work Report"},
		fields=["started_at", "ended_at", "duration_minutes", "idx"],
		order_by="idx asc",
		limit=0,
	)
	if segments:
		return segments
	if report.actual_start_time or report.actual_end_time:
		return [
			frappe._dict(
				started_at=report.actual_start_time,
				ended_at=report.actual_end_time,
				duration_minutes=flt(report.actual_minutes),
			)
		]
	return []


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
			actual_start_time=report.actual_start_time,
			actual_end_time=report.actual_end_time,
			actual_minutes=flt(report.actual_minutes),
			job_card_time_log=report.job_card_time_log,
			time_segments=_report_time_segments(report),
			in_flight=False,
		)
		for report in _approved_reports(doc.name)
	}
	in_flight = getattr(doc.flags, "worker_reporting_approval", None)
	if in_flight:
		report = frappe.db.get_value(
			"Job Card Work Report",
			in_flight,
			[
				"name",
				"job_card",
				"employee",
				"completed_qty",
				"actual_start_time",
				"actual_end_time",
				"actual_minutes",
				"status",
			],
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
			actual_start_time=report.actual_start_time,
			actual_end_time=report.actual_end_time,
			actual_minutes=flt(report.actual_minutes),
			job_card_time_log=None,
			time_segments=_report_time_segments(report),
			in_flight=True,
		)
	return expected, in_flight


def _validate_time_log_backlinks(doc):
	expected, in_flight = _expected_reports(doc)
	actual = {}
	for row in doc.get("time_logs") or []:
		report_name = row.get("custom_job_card_work_report_segment") or row.get(
			"custom_job_card_work_report"
		)
		if not report_name:
			frappe.throw(
				_("Every quantity row on a worker-reporting Job Card must come from an approved work report.")
			)
		actual.setdefault(report_name, []).append(row)

	if set(actual) != set(expected):
		frappe.throw(_("Job Card time rows and approved work reports no longer match."))
	for report_name, report in expected.items():
		rows = actual[report_name]
		if flt(sum(flt(row.completed_qty) for row in rows), 6) != flt(report.completed_qty, 6):
			frappe.throw(_("Job Card quantity for report {0} was changed.").format(report_name))
		if any(
			row.employee != report.employee or row.get("custom_reported_employee") != report.employee
			for row in rows
		):
			frappe.throw(_("Worker attribution changed for report {0}.").format(report_name))

		expected_segments = report.time_segments
		if expected_segments:
			if len(rows) != len(expected_segments):
				frappe.throw(_("Captured production time segments changed for report {0}.").format(report_name))
			for row, segment in zip(rows, expected_segments):
				if not (row.get("from_time") and row.get("to_time")):
					frappe.throw(_("Captured production time is missing from the Job Card row."))
				if (
					get_datetime(row.from_time) != get_datetime(segment.started_at)
					or get_datetime(row.to_time) != get_datetime(segment.ended_at)
					or flt(row.time_in_mins, 6) != flt(segment.duration_minutes, 6)
				):
					frappe.throw(_("Captured production time changed for report {0}.").format(report_name))
		elif len(rows) != 1 or rows[0].get("from_time") or rows[0].get("to_time") or flt(
			rows[0].get("time_in_mins")
		):
			frappe.throw(_("Legacy untimed reports cannot acquire synthetic Job Card time."))

		quantity_rows = [row for row in rows if flt(row.completed_qty)]
		if len(quantity_rows) != 1:
			frappe.throw(_("Each work report must retain exactly one Job Card quantity row."))
		if report.job_card_time_log and quantity_rows[0].name != report.job_card_time_log:
			frappe.throw(_("The stored Job Card row link changed for report {0}.").format(report_name))
	return expected, in_flight


def _validate_managed_job_card(doc, *, reset_process_loss: bool):
	_assert_configuration(doc)
	doc.custom_worker_reporting_enabled = 1
	expected, in_flight = _validate_time_log_backlinks(doc)
	precision = doc.precision("total_completed_qty")
	expected_qty = sum(flt(report.completed_qty) for report in expected.values())
	if flt(doc.total_completed_qty, precision) != flt(expected_qty, precision):
		frappe.throw(
			_("Job Card completed quantity must equal approved work reports ({0}).").format(
				flt(expected_qty, precision)
			)
		)
	from process_simplification.production_exceptions.service import expected_process_loss

	expected_loss = expected_process_loss(
		doc.name,
		in_flight_request=getattr(doc.flags, "production_exception_approval", None),
		for_update=True,
	)
	if reset_process_loss:
		# Native validation derives every unfinished unit as process loss. On a
		# managed Job Card only supervisor-approved exception requests are loss;
		# the rest remains pending on this same card.
		doc.process_loss_qty = expected_loss
	elif flt(doc.process_loss_qty, precision) != flt(expected_loss, precision):
		frappe.throw(
			_("Job Card process loss must equal approved production exception requests ({0}).").format(
				flt(expected_loss, precision)
			)
		)
	expected_pending = max(
		flt(doc.for_quantity, precision)
		- flt(expected_qty, precision)
		- flt(expected_loss, precision),
		0,
	)
	if doc.docstatus == 0:
		doc.pending_qty = expected_pending
	elif flt(doc.pending_qty, precision) != flt(expected_pending, precision):
		frappe.throw(
			_("Job Card pending quantity must equal the unreported balance ({0}).").format(
				flt(expected_pending, precision)
			)
		)
	reserved = _pending_qty(doc.name, exclude_report=in_flight)
	if (
		flt(doc.total_completed_qty, precision)
		+ flt(doc.process_loss_qty, precision)
		+ flt(reserved, precision)
	) > flt(
		doc.for_quantity, precision
	):
		frappe.throw(
			_("Approved production, process loss, and pending work reports exceed the Job Card quantity.")
		)
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
	if flt(
		flt(doc.total_completed_qty, precision) + flt(doc.process_loss_qty, precision),
		precision,
	) != flt(doc.for_quantity, precision):
		frappe.throw(_("Approve completed quantity and process loss for the full Job Card quantity."))
	if flt(doc.pending_qty, precision):
		frappe.throw(_("A worker-reporting Job Card cannot submit with pending quantity."))


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
	from process_simplification.notifications import notify_operation_completed

	notify_operation_completed(doc)


def before_cancel(doc, method=None):
	if not _tables_ready() or not _is_managed(doc):
		return
	if any(
		row.status in {"In Progress", "Pending Approval", "Approved"}
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
