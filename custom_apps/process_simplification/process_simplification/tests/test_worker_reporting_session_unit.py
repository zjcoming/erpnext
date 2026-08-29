from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from process_simplification.production_reporting.service import (
	_allow_work_order_update_for_approval,
)
from process_simplification.production_reporting.work_order import (
	WorkerReportingWorkOrderMixin,
)


class _NativeWorkOrder:
	def __init__(self, name):
		self.name = name

	def save(self, *args, **kwargs):
		return args, kwargs


class _ExtendedWorkOrder(WorkerReportingWorkOrderMixin, _NativeWorkOrder):
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
