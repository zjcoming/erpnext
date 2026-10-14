from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from process_simplification.production_reporting import service


class TestWorkReportRejection(TestCase):
	def test_blank_reason_is_a_validation_error_before_any_report_is_locked(self):
		for reason in (None, "", " \t\r\n"):
			with (
				self.subTest(reason=reason),
				patch.object(service, "_", side_effect=lambda message: message),
				patch.object(frappe, "throw", side_effect=frappe.ValidationError) as reject,
				patch.object(service, "_lock_report_for_review") as lock,
			):
				with self.assertRaises(frappe.ValidationError):
					service.reject_work_report("JCWR-1", reason)
				reject.assert_called_once_with("A rejection reason is required.")
				lock.assert_not_called()

	def test_valid_rejection_keeps_report_amount_and_records_trimmed_reason(self):
		doc = SimpleNamespace(
			status="Pending Approval", wage_amount=15, flags=SimpleNamespace(), save=Mock()
		)
		with (
			patch.object(service, "_lock_report_for_review", return_value=(None, None, doc)) as lock,
			patch.object(service, "request_audit", return_value=SimpleNamespace(ip="127.0.0.1", user_agent="test")),
			patch.object(service, "now_datetime", return_value="2026-10-10 10:00:00"),
			patch.object(frappe, "session", SimpleNamespace(user="supervisor@example.com")),
			patch("process_simplification.notifications.notify_work_report_decision") as notify,
		):
			self.assertIs(service.reject_work_report("JCWR-1", "  数量不符  "), doc)
		lock.assert_called_once_with(
			"JCWR-1", {"Pending Approval", "Rejected"}, require_job_card_consistency=False
		)
		self.assertEqual(doc.status, "Rejected")
		self.assertEqual(doc.rejection_reason, "数量不符")
		self.assertEqual(doc.reviewed_by, "supervisor@example.com")
		self.assertEqual(doc.wage_amount, 15)
		doc.save.assert_called_once_with(ignore_permissions=True)
		notify.assert_called_once_with(doc)

	def test_repeated_rejection_keeps_the_original_decision(self):
		doc = SimpleNamespace(status="Rejected", rejection_reason="原审核原因", save=Mock())
		with (
			patch.object(service, "_lock_report_for_review", return_value=(None, None, doc)),
			patch.object(service, "request_audit") as audit,
			patch("process_simplification.notifications.notify_work_report_decision") as notify,
		):
			self.assertIs(service.reject_work_report("JCWR-1", "迟到的重复审核"), doc)
		self.assertEqual(doc.rejection_reason, "原审核原因")
		doc.save.assert_not_called()
		audit.assert_not_called()
		notify.assert_not_called()
