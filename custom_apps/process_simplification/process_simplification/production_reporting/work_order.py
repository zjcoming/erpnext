from __future__ import annotations

from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime


def managed_draft_job_cards(work_order: str, *, for_update: bool = True) -> list[str]:
	if (
		not frappe.db.table_exists("Job Card Worker Assignment")
		or not frappe.db.has_column("Job Card", "custom_worker_reporting_enabled")
	):
		return []
	lock_clause = " for update" if for_update else ""
	return frappe.db.sql(
		f"""
		select job_card.name
		from `tabJob Card` job_card
		where job_card.work_order = %(work_order)s
		  and job_card.docstatus = 0
		  and (
			ifnull(job_card.custom_worker_reporting_enabled, 0) = 1
			or exists (
				select 1 from `tabJob Card Worker Assignment` assignment
				where assignment.job_card = job_card.name
			)
		  )
		order by job_card.name{lock_clause}
		""",
		{"work_order": work_order},
		pluck=True,
	)


def assert_no_managed_draft_job_cards(work_order: str):
	job_cards = managed_draft_job_cards(work_order, for_update=True)
	if job_cards:
		frappe.throw(
			_(
				"Work Order cannot finish, close, or cancel while worker-reporting Job Cards are still Draft: {0}. "
				"Finish their approved quantity, or remove a report-free assignment first."
			).format(", ".join(job_cards))
		)


def _pause_work_order_sessions(work_order: str):
	"""Freeze open timers before Stop, following the reporting lock order."""
	from process_simplification.production_reporting.service import _close_time_segment, _hash_key

	job_cards = managed_draft_job_cards(work_order, for_update=True)
	stored = frappe.db.get_value("Work Order", work_order, ["docstatus", "status"],
		as_dict=True, for_update=True)
	if not stored or cint(stored.docstatus) != 1 or stored.status == "Closed":
		frappe.throw(_("生产工单状态已变化，请刷新工单后再停止。"))
	if stored.status == "Stopped" or not job_cards:
		return stored.status
	filters = {"work_order": work_order, "job_card": ["in", job_cards]}
	fields = ["name", "job_card", "work_order", "employee", "employee_user"]
	candidates = frappe.get_all("Job Card Worker Assignment", filters=filters,
		fields=fields, order_by="name", limit=0)
	if any(not row.employee or not row.employee_user for row in candidates):
		frappe.throw(_("派工信息不完整，请刷新工单后再停止。"))
	identities = {(row.employee, row.employee_user) for row in candidates}
	for employee in sorted({employee for employee, _user in identities}):
		frappe.db.get_value("Employee", employee, "name", for_update=True)
	for user in sorted({user for _employee, user in identities}):
		frappe.db.get_value("User", user, "name", for_update=True)
	# These locking reads must see newly committed sessions under REPEATABLE READ.
	assignments = frappe.db.get_values("Job Card Worker Assignment", filters, fields,
		as_dict=True, order_by="name", for_update=True)
	if any((row.employee, row.employee_user) not in identities for row in assignments):
		frappe.throw(_("派工信息已变化，请刷新工单后再停止。"))
	assignments = {row.name: row for row in assignments}
	rows = frappe.db.get_values("Job Card Work Report", {**filters, "status": "In Progress"},
		["name", "job_card", "assignment", "employee", "employee_user"],
		as_dict=True, order_by="name", for_update=True)
	documents = []
	for row in rows:
		doc = frappe.get_doc("Job Card Work Report", row.name, for_update=True)
		assignment = assignments.get(doc.assignment)
		if (doc.status != "In Progress" or doc.work_order != work_order or doc.job_card not in job_cards
			or not assignment or any(doc.get(field) != assignment.get(field)
				for field in ("job_card", "work_order", "employee", "employee_user"))
			or any(doc.get(field) != row.get(field)
				for field in ("job_card", "assignment", "employee", "employee_user"))):
			frappe.throw(_("当前计时与工单不一致，请刷新后联系主管。"))
		if not doc.timer_paused_at:
			documents.append(doc)
	stopped_at = now_datetime()
	for doc in documents:
		_close_time_segment(doc, stopped_at,
			_hash_key("work_order_stop", work_order, doc.name, stopped_at, frappe.session.user))
		doc.timer_paused_at = stopped_at
		doc.flags.worker_reporting_action = True
		doc.save(ignore_permissions=True)
	return stored.status


_CLOSE_CONTINUATION_FLAG = "worker_reporting_unstarted_continuation_close"


@contextmanager
def _allow_unstarted_continuation_close(work_order: str, company: str):
	"""Used only after the close RPC has authorized and locked JC -> WO."""
	previous = getattr(frappe.flags, _CLOSE_CONTINUATION_FLAG, None)
	scope = frappe._dict(work_order=work_order, company=company,
		user=frappe.session.user, assignment_names=[])
	setattr(frappe.flags, _CLOSE_CONTINUATION_FLAG, scope)
	try:
		yield scope
	finally:
		if previous is None:
			frappe.flags.pop(_CLOSE_CONTINUATION_FLAG, None)
		else:
			setattr(frappe.flags, _CLOSE_CONTINUATION_FLAG, previous)


def _continuation_close_scope(work_order: str):
	scope = getattr(frappe.flags, _CLOSE_CONTINUATION_FLAG, None)
	if scope is None:
		return None
	stored = frappe.db.get_value("Work Order", work_order,
		["company", "docstatus"], as_dict=True, for_update=True)
	if (scope.work_order != work_order or scope.user != frappe.session.user
		or not stored or stored.company != scope.company or cint(stored.docstatus) != 1):
		frappe.throw(_("只能在已授权的工单关闭操作中处理该工单的未开工续产任务。"), frappe.PermissionError)
	return scope


def assert_work_order_close_allowed(work_order: str) -> list[str]:
	"""Only explicit close may retain untouched continuation cards as history."""
	scope = _continuation_close_scope(work_order)
	if scope is None:
		assert_no_managed_draft_job_cards(work_order)
		return []
	from process_simplification.production_reporting import assignment_movement as movements
	from process_simplification.production_reporting.domain import job_card_qty_precision

	precision = job_card_qty_precision()
	retiring = []
	for name in managed_draft_job_cards(work_order, for_update=True):
		card = frappe.db.get_value("Job Card", name, ["name", "work_order", "company",
			"operation", "operation_id", "docstatus", "for_quantity", "total_completed_qty",
			"process_loss_qty", "pending_qty", "actual_start_date", "actual_end_date"],
			as_dict=True, for_update=True)
		assignments = frappe.db.sql("""select name, job_card, work_order, company, operation,
			operation_id, assigned_qty, status from `tabJob Card Worker Assignment`
			where job_card=%s order by name for update""", (name,), as_dict=True)
		names = [row.name for row in assignments]
		rows = movements._movement_rows(assignments=names, for_update=True) if names else []
		for row in rows:
			# The movement helper returns linkage and quantity only; verify stored identity too.
			stored = frappe.db.get_value("Job Card Assignment Movement", row.name,
				["work_order", "company", "operation", "operation_id"], as_dict=True, for_update=True)
			if not stored:
				frappe.throw(_("续产分配记录已变化，本次工单不能关闭。"))
			row.update(stored)
		by_name = {row.name: row for row in assignments}
		identity = (work_order, scope.company, card.operation, card.operation_id)
		untouched = (card.docstatus == 0 and card.work_order == work_order
			and card.company == scope.company and flt(card.for_quantity, precision) > 0
			and flt(card.pending_qty, precision) == flt(card.for_quantity, precision)
			and not flt(card.total_completed_qty) and not flt(card.process_loss_qty)
			and not card.actual_start_date and not card.actual_end_date
			and not frappe.db.get_value("Job Card Work Report", {"job_card": name}, "name", for_update=True)
			and not frappe.db.get_value("Job Card Time Log", {"parent": name, "parenttype": "Job Card"}, "name", for_update=True)
			and bool(names) and not frappe.db.get_value("Job Card Work Report", {"assignment": ["in", names]}, "name", for_update=True))
		complete_continuation = (len(rows) == len(assignments) > 0
			and {row.target_assignment for row in rows} == set(names)
			and flt(sum(flt(row.assigned_qty) for row in assignments), precision) == flt(card.for_quantity, precision)
			and all(row.status == "Active" and row.job_card == name
				and (row.work_order, row.company, row.operation, row.operation_id) == identity for row in assignments)
			and all(row.movement_type == movements.CONTINUATION and row.job_card != name
				and (row.work_order, row.company, row.operation, row.operation_id) == identity
				and row.reference_doctype == "Job Card" and row.reference_name == name
				and flt(row.qty, precision) == flt(by_name[row.target_assignment].assigned_qty, precision)
				for row in rows))
		if not untouched or not complete_continuation:
			frappe.throw(_("工单仍有已开工、报工或非完整分批承接的草稿任务，不能永久关闭：{0}").format(name))
		retiring.extend(names)
	scope.assignment_names = retiring
	return retiring


def cancel_unstarted_continuation_assignments(work_order: str, names: list[str]):
	"""Retire only validated targets; keep every quantity and history link."""
	scope = _continuation_close_scope(work_order)
	if (scope is None or list(names) != scope.assignment_names
		or frappe.db.get_value("Work Order", work_order, "status", for_update=True) != "Closed"):
		frappe.throw(_("未开工续产任务只能随已授权工单的永久关闭一同退场。"), frappe.PermissionError)
	for name in names:
		doc = frappe.get_doc("Job Card Worker Assignment", name, for_update=True)
		if doc.work_order != work_order or doc.company != scope.company or doc.status != "Active":
			frappe.throw(_("续产分配状态已变化，本次工单关闭已回滚。"))
		had_action = "worker_reporting_action" in doc.flags
		previous = doc.flags.get("worker_reporting_action")
		try:
			doc.flags.worker_reporting_action = True
			doc.status = "Cancelled"
			doc.save(ignore_permissions=True)
		finally:
			if had_action:
				doc.flags.worker_reporting_action = previous
			else:
				doc.flags.pop("worker_reporting_action", None)


def assert_no_worker_reporting_history(work_order: str):
	"""Read only the Work Order row; safe after Frappe has locked that row."""
	if not frappe.db.has_column("Work Order", "custom_worker_reporting_enabled"):
		return
	if cint(
		frappe.db.get_value(
			"Work Order",
			work_order,
			"custom_worker_reporting_enabled",
			for_update=True,
		)
	):
		frappe.throw(
			_(
				"Work Order cannot be cancelled while worker-reporting Job Cards are assigned or have "
				"report history. Remove every report-free assignment first; reported history is permanent."
			)
		)


class WorkerReportingWorkOrderMixin:
	"""Protect the native Work Order status service from stranding wage history."""

	def get_status(self, status=None):
		# Reservation callbacks must preserve a submitted Work Order's permanent closure.
		if cint(self.docstatus) == 1 and (status or self.status) == "Closed":
			return "Closed"
		return super().get_status(status)

	def create_job_card(self):
		# Frappe checks the draft -> submitted transition and submit permission
		# before on_submit calls this internal (non-whitelisted) native method.
		if getattr(self, "_action", None) != "submit" or cint(self.docstatus) != 1 or not self.name:
			return super().create_job_card()
		# App managers also have a company read boundary, even when their scope
		# comes from Employee rather than a native Company User Permission.
		self.check_permission("read")
		from process_simplification.api.production_plan_adapter import _allow_generated_job_cards_for

		with _allow_generated_job_cards_for(self.name):
			return super().create_job_card()

	def save(self, *args, **kwargs):
		# Job Card submission loads a fresh Work Order and therefore cannot inherit
		# the Job Card's ignore_permissions flag. Approval sets this request-local,
		# name-scoped marker so only the exact native parent update is permitted.
		if (
			getattr(frappe.flags, "worker_reporting_approval_work_order", None)
			== self.name
		):
			kwargs["ignore_permissions"] = True
		return super().save(*args, **kwargs)

	def before_update_after_submit(self):
		# The marker is maintained only by the locked assignment services via
		# db.set_value. Never accept it from a generic Work Order save payload.
		if frappe.db.has_column("Work Order", "custom_worker_reporting_enabled"):
			stored = cint(
				frappe.db.get_value(
					"Work Order",
					self.name,
					"custom_worker_reporting_enabled",
					for_update=True,
				)
			)
			if cint(self.get("custom_worker_reporting_enabled")) != stored:
				frappe.throw(_("Worker-reporting history is maintained only by the assignment service."))
		parent = getattr(super(), "before_update_after_submit", None)
		if parent:
			return parent()

	def update_status(self, status=None):
		# Explicit close reaches this method before the native status service writes
		# Work Order. Acquire Job Card locks first to match approval's JC -> WO order.
		if status == "Closed":
			assert_work_order_close_allowed(self.name)
		elif status == "Stopped":
			self.check_permission("read")
			self.check_permission("write")
			# Native status updates compare against self.status; use the current
			# locked value when the caller loaded an older transaction snapshot.
			self.status = _pause_work_order_sessions(self.name)
		return super().update_status(status)

	def cancel(self):
		assert_no_worker_reporting_history(self.name)
		return super().cancel()

	def before_cancel(self):
		# savedocs(action="Cancel") calls _cancel()/save() directly and never reaches
		# cancel(). Frappe already owns the Work Order row here, so query no children.
		assert_no_worker_reporting_history(self.name)
		parent = getattr(super(), "before_cancel", None)
		if parent:
			return parent()

	def discard(self):
		assert_no_worker_reporting_history(self.name)
		return super().discard()

	def before_discard(self):
		assert_no_worker_reporting_history(self.name)
		parent = getattr(super(), "before_discard", None)
		if parent:
			return parent()
