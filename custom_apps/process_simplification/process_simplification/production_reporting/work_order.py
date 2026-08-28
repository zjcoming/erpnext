from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint


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
			assert_no_managed_draft_job_cards(self.name)
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
