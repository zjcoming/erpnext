from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from process_simplification.production_reporting.service import (
	_allow_work_order_update_for_approval,
)
from process_simplification.api.production_plan_adapter import (
	_allow_generated_job_cards_for,
)
from process_simplification.production_reporting.job_card import (
	SimplifiedFlowJobCardMixin,
)
from process_simplification.production_reporting.work_order import (
	WorkerReportingWorkOrderMixin,
)


class _NativeWorkOrder:
	def __init__(self, name):
		self.name = name
		self.status = "In Process"

	def save(self, *args, **kwargs):
		return args, kwargs

	def update_status(self, status=None):
		return status

	def check_permission(self, permission_type):
		pass


class _ExtendedWorkOrder(WorkerReportingWorkOrderMixin, _NativeWorkOrder):
	pass


class _NativeJobCard:
	def __init__(self, work_order):
		self.work_order = work_order

	def get(self, fieldname):
		return getattr(self, fieldname, None)

	def insert(self, *args, **kwargs):
		return args, kwargs


class _ExtendedJobCard(SimplifiedFlowJobCardMixin, _NativeJobCard):
	pass


class TestWorkerReportingSessionUnit(IntegrationTestCase):
	def test_scoped_approval_flag_preserves_web_session_identity(self):
		original_user = frappe.session.user
		original_sid = frappe.session.sid
		previous_flag = getattr(
			frappe.flags, "worker_reporting_approval_work_order", None
		)
		try:
			frappe.session.user = "reviewer@example.com"
			frappe.session.sid = "real-web-session-id"
			with _allow_work_order_update_for_approval("WO-1"):
				self.assertEqual(
					frappe.flags.worker_reporting_approval_work_order, "WO-1"
				)
				self.assertEqual(frappe.session.user, "reviewer@example.com")
				self.assertEqual(frappe.session.sid, "real-web-session-id")

			self.assertEqual(frappe.session.user, "reviewer@example.com")
			self.assertEqual(frappe.session.sid, "real-web-session-id")
			self.assertIsNone(
				getattr(frappe.flags, "worker_reporting_approval_work_order", None)
			)
		finally:
			frappe.session.user = original_user
			frappe.session.sid = original_sid
			if previous_flag is None:
				frappe.flags.pop("worker_reporting_approval_work_order", None)
			else:
				frappe.flags.worker_reporting_approval_work_order = previous_flag

	def test_work_order_permission_bypass_is_limited_to_exact_parent(self):
		previous_flag = getattr(
			frappe.flags, "worker_reporting_approval_work_order", None
		)
		try:
			frappe.flags.worker_reporting_approval_work_order = "WO-1"
			_args, matching_kwargs = _ExtendedWorkOrder("WO-1").save()
			_args, other_kwargs = _ExtendedWorkOrder("WO-2").save()
			self.assertTrue(matching_kwargs["ignore_permissions"])
			self.assertNotIn("ignore_permissions", other_kwargs)
		finally:
			if previous_flag is None:
				frappe.flags.pop("worker_reporting_approval_work_order", None)
			else:
				frappe.flags.worker_reporting_approval_work_order = previous_flag

	def test_generated_job_card_permission_bypass_is_limited_to_exact_work_order(self):
		previous_flag = getattr(
			frappe.flags, "simplified_flow_job_card_work_order", None
		)
		try:
			frappe.flags.simplified_flow_job_card_work_order = "WO-1"
			_args, matching_kwargs = _ExtendedJobCard("WO-1").insert()
			_args, other_kwargs = _ExtendedJobCard("WO-2").insert()
			self.assertTrue(matching_kwargs["ignore_permissions"])
			self.assertNotIn("ignore_permissions", other_kwargs)
		finally:
			if previous_flag is None:
				frappe.flags.pop("simplified_flow_job_card_work_order", None)
			else:
				frappe.flags.simplified_flow_job_card_work_order = previous_flag

	def test_generated_job_card_scope_preserves_session_and_restores_previous_flag(self):
		original_user = frappe.session.user
		original_sid = frappe.session.sid
		previous_flag = getattr(
			frappe.flags, "simplified_flow_job_card_work_order", None
		)
		try:
			frappe.session.user = "production@example.com"
			frappe.session.sid = "production-web-session"
			frappe.flags.simplified_flow_job_card_work_order = "WO-OUTER"
			with _allow_generated_job_cards_for("WO-1"):
				self.assertEqual(
					frappe.flags.simplified_flow_job_card_work_order, "WO-1"
				)
				self.assertEqual(frappe.session.user, "production@example.com")
				self.assertEqual(frappe.session.sid, "production-web-session")
			self.assertEqual(
				frappe.flags.simplified_flow_job_card_work_order, "WO-OUTER"
			)
		finally:
			frappe.session.user = original_user
			frappe.session.sid = original_sid
			if previous_flag is None:
				frappe.flags.pop("simplified_flow_job_card_work_order", None)
			else:
				frappe.flags.simplified_flow_job_card_work_order = previous_flag


class TestWorkOrderStopTimers(UnitTestCase):
	def _pause_fixture(self, *, paused=False, stored_status="In Process", assignment_employee="EMP-1"):
		from process_simplification.production_reporting.work_order import _pause_work_order_sessions

		started = datetime(2026, 10, 14, 9)
		stopped = started + timedelta(minutes=10)
		segment = frappe._dict(started_at=started, ended_at=None, stop_request_key=None)
		if paused:
			segment.update(ended_at=started + timedelta(minutes=5), duration_minutes=5,
				stop_request_key="worker-pause")
		events = []
		doc = frappe._dict(name="REPORT-1", status="In Progress", assignment="ASSIGN-1",
			work_order="WO-1", job_card="JC-1", employee="EMP-1", employee_user="worker@example.com",
			actual_start_time=started, timer_paused_at=segment.ended_at,
			time_segments=[segment], flags=frappe._dict(), rate=30, hourly_rate_snapshot=30,
			actual_minutes=0, reported_minutes=0, wage_amount=0)
		doc.save = Mock(side_effect=lambda **kwargs: events.append("Save"))
		row = frappe._dict({key: doc[key] for key in ("name", "job_card", "assignment", "employee", "employee_user")})
		candidate = frappe._dict(name="ASSIGN-1", job_card="JC-1", work_order="WO-1",
			employee="EMP-1", employee_user="worker@example.com")

		def get_value(doctype, name, fields, **kwargs):
			self.assertTrue(kwargs.get("for_update"))
			events.append(doctype)
			if doctype == "Work Order":
				return frappe._dict(docstatus=1, status=stored_status)
			return name

		def get_values(doctype, filters, fields, **kwargs):
			self.assertTrue(kwargs.get("for_update"))
			self.assertEqual(kwargs.get("order_by"), "name")
			events.append(doctype)
			if doctype == "Job Card Worker Assignment":
				return [frappe._dict(candidate, employee=assignment_employee)]
			return [row]

		def get_all(doctype, **kwargs):
			self.assertEqual(doctype, "Job Card Worker Assignment")
			return [candidate]

		def get_doc(doctype, name, **kwargs):
			self.assertTrue(kwargs.get("for_update"))
			events.append(doctype)
			return doc

		with patch("process_simplification.production_reporting.work_order.managed_draft_job_cards",
			side_effect=lambda *args, **kwargs: events.append("Job Card") or ["JC-1"]), \
			patch.object(frappe.db, "get_value", side_effect=get_value), \
			patch.object(frappe.db, "get_values", side_effect=get_values), \
			patch.object(frappe, "get_all", side_effect=get_all), \
			patch.object(frappe, "get_doc", side_effect=get_doc), \
			patch("process_simplification.production_reporting.work_order.now_datetime", return_value=stopped):
			if assignment_employee != doc.employee:
				with self.assertRaises(frappe.ValidationError):
					_pause_work_order_sessions("WO-1")
			else:
				_pause_work_order_sessions("WO-1")
		return doc, events, stopped

	def test_stop_includes_current_report_only_after_canonical_locks(self):
		doc, events, stopped = self._pause_fixture()
		self.assertEqual(events, ["Job Card", "Work Order", "Employee", "User",
			"Job Card Worker Assignment", "Job Card Work Report", "Job Card Work Report", "Save"])
		self.assertEqual(doc.timer_paused_at, stopped)
		self.assertEqual(doc.time_segments[0].ended_at, stopped)
		self.assertEqual(doc.time_segments[0].duration_minutes, 10)
		self.assertTrue(doc.time_segments[0].stop_request_key)
		self.assertEqual((doc.rate, doc.hourly_rate_snapshot, doc.status), (30, 30, "In Progress"))
		self.assertEqual((doc.actual_minutes, doc.reported_minutes, doc.wage_amount), (0, 0, 0))
		doc.save.assert_called_once_with(ignore_permissions=True)

	def test_stop_preserves_worker_paused_segment_and_repeated_stopped_is_noop(self):
		doc, _events, _stopped = self._pause_fixture(paused=True)
		self.assertEqual(doc.time_segments[0].stop_request_key, "worker-pause")
		self.assertEqual(doc.time_segments[0].duration_minutes, 5)
		doc.save.assert_not_called()
		doc, events, _stopped = self._pause_fixture(stored_status="Stopped")
		self.assertEqual(events, ["Job Card", "Work Order"])
		self.assertIsNone(doc.time_segments[0].ended_at)
		doc.save.assert_not_called()

	def test_current_assignment_identity_change_fails_before_report_lock_or_write(self):
		doc, events, _stopped = self._pause_fixture(assignment_employee="OTHER")
		self.assertEqual(events[-1], "Job Card Worker Assignment")
		self.assertIsNone(doc.time_segments[0].ended_at)
		doc.save.assert_not_called()

	def test_stop_uses_current_locked_status_after_stale_stopped_snapshot(self):
		order = _ExtendedWorkOrder("WO-1")
		order.status = "Stopped"
		seen = []
		with patch("process_simplification.production_reporting.work_order._pause_work_order_sessions",
			return_value="In Process"), \
			patch.object(_NativeWorkOrder, "update_status",
				side_effect=lambda status: seen.append(order.status) or status):
			self.assertEqual(order.update_status("Stopped"), "Stopped")
		self.assertEqual(seen, ["In Process"])

	def test_stop_requires_document_read_and_write_before_internal_timer_save(self):
		order = _ExtendedWorkOrder("WO-1")
		events = []
		order.check_permission = Mock(side_effect=lambda permission: events.append(permission))
		with patch("process_simplification.production_reporting.work_order._pause_work_order_sessions",
			side_effect=lambda *args: events.append("pause")):
			self.assertEqual(order.update_status("Stopped"), "Stopped")
		self.assertEqual(events, ["read", "write", "pause"])
		order.check_permission = Mock(side_effect=frappe.PermissionError)
		with patch("process_simplification.production_reporting.work_order._pause_work_order_sessions") as pause:
			with self.assertRaises(frappe.PermissionError):
				order.update_status("Stopped")
			pause.assert_not_called()

	def test_resume_keeps_timer_paused_and_native_failure_is_not_swallowed(self):
		order = _ExtendedWorkOrder("WO-1")
		with patch("process_simplification.production_reporting.work_order._pause_work_order_sessions") as pause:
			self.assertEqual(order.update_status("In Process"), "In Process")
			pause.assert_not_called()
			with patch.object(_NativeWorkOrder, "update_status", side_effect=frappe.ValidationError):
				with self.assertRaises(frappe.ValidationError):
					order.update_status("Stopped")
			pause.assert_called_once_with("WO-1")
