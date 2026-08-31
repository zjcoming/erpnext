from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import random_string

from process_simplification.management_access import (
	OWNER_ROLE,
	PRODUCTION_MANAGER_ROLE,
	WAGE_MANAGER_ROLE,
	WAREHOUSE_OPERATOR_ROLE,
)
from process_simplification.notifications import (
	PROCUREMENT_RESPONSIBILITY,
	PRODUCTION_DISPATCH_RESPONSIBILITY,
	WAREHOUSE_RESPONSIBILITY,
	notify_exception_approved,
	notify_operation_completed,
	notify_quick_order_shortage,
	notify_users,
	notify_work_report_decision,
	notify_work_report_submitted,
	notify_worker_assignment,
	responsibility_recipients,
)
from process_simplification.production_exceptions.constants import (
	AWAITING_STOCK_ENTRY,
	MATERIAL_RETURN,
)


class TestProcessNotifications(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.company = frappe.db.get_value("Company", {}, "name")
		self.assertTrue(self.company)
		settings = frappe.get_single("Process Simplification Settings")
		settings.set("notification_recipients", [])
		settings.save(ignore_permissions=True)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		super().tearDown()

	def _make_user(self, role: str) -> str:
		email = "ps-notify-{0}@example.com".format(random_string(12).lower())
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Notification Test",
				"send_welcome_email": 0,
				"roles": [{"role": role}],
			}
		).insert(ignore_permissions=True)
		return email

	def _configure(self, responsibility: str, user: str) -> None:
		settings = frappe.get_single("Process Simplification Settings")
		settings.append(
			"notification_recipients",
			{
				"company": self.company,
				"responsibility": responsibility,
				"user": user,
			},
		)
		settings.save(ignore_permissions=True)

	def test_alert_is_persistent_realtime_and_deduplicated(self):
		worker = self._make_user("Production Worker")
		with patch("frappe.publish_realtime") as publish_realtime:
			for _ in range(2):
				notify_users(
					[worker],
					subject="测试通知",
					description="提交后实时显示。",
					document_type="Job Card Work Report",
					document_name="JCWR-NOTIFY-TEST",
					link="/app/production-report-history",
				)

		logs = frappe.get_all(
			"Notification Log",
			filters={
				"for_user": worker,
				"document_type": "Job Card Work Report",
				"document_name": "JCWR-NOTIFY-TEST",
				"subject": "测试通知",
			},
			fields=["type", "app", "link", "read"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].type, "Alert")
		self.assertEqual(logs[0].app, "process_simplification")
		self.assertEqual(logs[0].link, "/app/production-report-history")
		self.assertFalse(logs[0].read)
		publish_realtime.assert_any_call(
			"notification",
			after_commit=True,
			user=worker,
		)
		self.assertEqual(
			len(
				[
					call
					for call in publish_realtime.call_args_list
					if call.args and call.args[0] == "notification"
				]
			),
			1,
		)

	def test_configured_recipient_overrides_role_fallback(self):
		configured = self._make_user(WAREHOUSE_OPERATOR_ROLE)
		self._make_user(WAREHOUSE_OPERATOR_ROLE)
		self._configure(WAREHOUSE_RESPONSIBILITY, configured)

		self.assertEqual(
			responsibility_recipients(self.company, WAREHOUSE_RESPONSIBILITY),
			[configured],
		)

	def test_route_rejects_user_without_responsible_role(self):
		production_manager = self._make_user("Process Simplification Production Manager")
		settings = frappe.get_single("Process Simplification Settings")
		settings.append(
			"notification_recipients",
			{
				"company": self.company,
				"responsibility": PROCUREMENT_RESPONSIBILITY,
				"user": production_manager,
			},
		)
		with self.assertRaises(frappe.ValidationError):
			settings.save(ignore_permissions=True)

	def test_wage_manager_cannot_change_notification_responsibility(self):
		warehouse = self._make_user(WAREHOUSE_OPERATOR_ROLE)
		wage_manager = self._make_user(WAGE_MANAGER_ROLE)
		self._configure(WAREHOUSE_RESPONSIBILITY, warehouse)

		frappe.set_user(wage_manager)
		settings = frappe.get_single("Process Simplification Settings")
		settings.set("notification_recipients", [])
		settings.save()

		frappe.set_user("Administrator")
		self.assertEqual(
			responsibility_recipients(self.company, WAREHOUSE_RESPONSIBILITY),
			[warehouse],
		)

	def test_workflow_helpers_notify_exact_people_and_routes(self):
		worker = self._make_user("Production Worker")
		warehouse = self._make_user(WAREHOUSE_OPERATOR_ROLE)
		production_manager = self._make_user(PRODUCTION_MANAGER_ROLE)
		self._configure(WAREHOUSE_RESPONSIBILITY, warehouse)
		self._configure(PROCUREMENT_RESPONSIBILITY, warehouse)
		self._configure(PRODUCTION_DISPATCH_RESPONSIBILITY, production_manager)

		assignment = frappe._dict(
			name="JCWA-NOTIFY-1",
			employee="EMP-NOTIFY-1",
			employee_user=worker,
			operation="切割",
			job_card="JC-NOTIFY-1",
			work_order="WO-NOTIFY-1",
		)
		notify_worker_assignment(assignment)
		notify_worker_assignment(assignment)

		report = frappe._dict(
			name="JCWR-NOTIFY-1",
			employee="EMP-NOTIFY-1",
			employee_name="张三",
			employee_user=worker,
			supervisor=production_manager,
			operation="切割",
			completed_qty=8,
			status="Pending Approval",
		)
		notify_work_report_submitted(report)
		report.status = "Approved"
		notify_work_report_decision(report)

		completed_job_card = frappe._dict(
			name="JC-NOTIFY-1",
			company=self.company,
			work_order="WO-NOTIFY-1",
			operation="切割",
		)
		with patch(
			"process_simplification.notifications._next_job_card",
			return_value=frappe._dict(name="JC-NOTIFY-2", operation="焊接"),
		):
			notify_operation_completed(completed_job_card)
			notify_operation_completed(completed_job_card)

		exception = frappe._dict(
			name="PER-NOTIFY-1",
			request_type=MATERIAL_RETURN,
			status=AWAITING_STOCK_ENTRY,
			company=self.company,
			employee_user=worker,
			stock_entry="MAT-STE-NOTIFY-1",
		)
		notify_exception_approved(exception)
		notify_quick_order_shortage("SAL-ORD-NOTIFY-1", self.company, [{"item_code": "RM-1"}])

		worker_subjects = set(
			frappe.get_all(
				"Notification Log",
				filters={"for_user": worker},
				pluck="subject",
			)
		)
		self.assertIn("收到新派工：切割", worker_subjects)
		self.assertIn("报工已通过", worker_subjects)
		self.assertIn("生产异常已通过：余料退库", worker_subjects)
		self.assertEqual(
			frappe.db.count(
				"Notification Log",
				{"for_user": worker, "document_name": assignment.name},
			),
			1,
		)
		self.assertTrue(
			frappe.db.exists(
				"Notification Log",
				{
					"for_user": production_manager,
					"document_name": report.name,
					"link": "/app/production-report-review",
				},
			)
		)
		warehouse_subjects = set(
			frappe.get_all(
				"Notification Log",
				filters={"for_user": warehouse},
				pluck="subject",
			)
		)
		self.assertIn("待库存处理：余料退库", warehouse_subjects)
		self.assertIn("销售订单有缺料待采购：SAL-ORD-NOTIFY-1", warehouse_subjects)
		self.assertTrue(
			frappe.db.exists(
				"Notification Log",
				{
					"for_user": production_manager,
					"document_type": "Job Card",
					"document_name": completed_job_card.name,
					"subject": "上一工序已完成，待派工：焊接",
					"link": "/app/production-workbench",
				},
			)
		)
		self.assertEqual(
			frappe.db.count(
				"Notification Log",
				{
					"for_user": production_manager,
					"document_name": completed_job_card.name,
				},
			),
			1,
		)


class TestProcessNotificationRouting(UnitTestCase):
	def test_administrator_is_not_an_implicit_operational_recipient(self):
		from process_simplification.notifications import _enabled_system_user

		with patch("process_simplification.notifications.frappe.db.get_value") as get_value:
			self.assertFalse(_enabled_system_user("Administrator"))

		get_value.assert_not_called()

	def test_fallback_filters_candidates_by_company_scope(self):
		with (
			patch("process_simplification.notifications._configured_recipients", return_value=[]),
			patch(
				"process_simplification.notifications.frappe.get_all",
				return_value=[
					"Administrator",
					"warehouse-a@example.com",
					"warehouse-b@example.com",
					"owner@example.com",
				],
			),
			patch(
				"process_simplification.notifications.frappe.get_roles",
				side_effect=lambda user: [
					"Process Simplification Owner"
					if user == "owner@example.com"
					else WAREHOUSE_OPERATOR_ROLE
				],
			),
			patch(
				"process_simplification.notifications._user_matches_company",
				side_effect=lambda user, company: user
				in {"Administrator", "warehouse-a@example.com", "owner@example.com"},
			),
		):
			self.assertEqual(
				responsibility_recipients("Company A", WAREHOUSE_RESPONSIBILITY),
				["warehouse-a@example.com"],
			)

	def test_fallback_uses_owner_only_when_no_warehouse_operator_matches(self):
		with (
			patch("process_simplification.notifications._configured_recipients", return_value=[]),
			patch(
				"process_simplification.notifications.frappe.get_all",
				return_value=["owner@example.com"],
			),
			patch(
				"process_simplification.notifications.frappe.get_roles",
				return_value=[OWNER_ROLE],
			),
			patch(
				"process_simplification.notifications._user_matches_company",
				return_value=True,
			),
		):
			self.assertEqual(
				responsibility_recipients("Company A", PROCUREMENT_RESPONSIBILITY),
				["owner@example.com"],
			)

	def test_dispatch_fallback_prefers_production_manager(self):
		with (
			patch("process_simplification.notifications._configured_recipients", return_value=[]),
			patch(
				"process_simplification.notifications.frappe.get_all",
				return_value=["production@example.com", "owner@example.com"],
			),
			patch(
				"process_simplification.notifications.frappe.get_roles",
				side_effect=lambda user: [
					PRODUCTION_MANAGER_ROLE if user == "production@example.com" else OWNER_ROLE
				],
			),
			patch(
				"process_simplification.notifications._user_matches_company",
				return_value=True,
			),
		):
			self.assertEqual(
				responsibility_recipients("Company A", PRODUCTION_DISPATCH_RESPONSIBILITY),
				["production@example.com"],
			)

	def test_managed_job_card_submit_triggers_dispatch_notification(self):
		from process_simplification.production_reporting import job_card as job_card_hooks

		doc = frappe._dict(name="JC-DISPATCH-TRIGGER")
		with (
			patch.object(job_card_hooks, "_tables_ready", return_value=True),
			patch.object(job_card_hooks, "_is_managed", return_value=True),
			patch.object(job_card_hooks.frappe.db, "set_value") as set_value,
			patch(
				"process_simplification.notifications.notify_operation_completed"
			) as notify_completed,
		):
			job_card_hooks.on_submit(doc)

		set_value.assert_called_once_with(
			"Job Card Worker Assignment",
			{"job_card": doc.name, "status": "Active"},
			"status",
			"Completed",
			update_modified=False,
		)
		notify_completed.assert_called_once_with(doc)

	def test_production_notification_failure_is_non_blocking_and_logged(self):
		doc = frappe._dict(
			name="JCWA-FAILURE-TEST",
			employee_user="worker@example.com",
			operation="切割",
			job_card="JC-1",
			work_order="WO-1",
		)
		with (
			patch.object(frappe, "in_test", False),
			patch("process_simplification.notifications.notify_users", side_effect=RuntimeError("queue down")),
			patch(
				"process_simplification.notifications.frappe.log_error",
				side_effect=RuntimeError("error log unavailable"),
			) as log_error,
		):
			self.assertEqual(notify_worker_assignment(doc), [])

		log_error.assert_called_once()
