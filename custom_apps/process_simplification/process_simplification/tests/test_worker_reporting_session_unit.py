from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

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

	def save(self, *args, **kwargs):
		return args, kwargs


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
