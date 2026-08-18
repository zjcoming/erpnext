from __future__ import annotations

from datetime import datetime, time
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_months, flt, get_first_day, nowdate, random_string

from erpnext.manufacturing.doctype.work_order.test_work_order import make_wo_order_test_record
from erpnext.setup.doctype.employee.test_employee import make_employee
from erpnext.tests.utils import load_test_records_for

from process_simplification.production_reporting import service, summary
from process_simplification.process_simplification.doctype.job_card_work_report.job_card_work_report import (
	JobCardWorkReport,
)


class TestWorkerReporting(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		# secondary_connection() intentionally leaves its connection active after
		# first initialization; every test fixture belongs on the primary transaction.
		frappe.local.db = self._primary_connection
		self.globalTestRecords = load_test_records_for("BOM")
		self.worker_user = self._make_worker()
		self.worker = frappe.db.get_value("Employee", {"user_id": self.worker_user}, "name")
		self.supervisor = self._make_supervisor()
		self.wage_manager = self._make_user("Production Wage Manager")
		self._grant_company(self.wage_manager, "_Test Company")

	def tearDown(self):
		try:
			frappe.local.db = self._primary_connection
			frappe.set_user("Administrator")
			frappe.db.rollback()
		finally:
			super().tearDown()

	def _email(self, prefix):
		return f"{prefix}-{random_string(10).lower()}@example.com"

	def _make_user(self, role):
		email = self._email(role.lower().replace(" ", "-"))
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": role,
				"send_welcome_email": 0,
				"roles": [{"role": role}],
			}
		).insert(ignore_permissions=True)
		return email

	def _make_worker(self):
		email = self._email("production-worker")
		make_employee(email, company="_Test Company")
		frappe.get_doc("User", email).add_roles("Production Worker")
		return email

	def _grant_company(self, user, company):
		if not frappe.db.exists(
			"User Permission",
			{"user": user, "allow": "Company", "for_value": company},
		):
			frappe.get_doc(
				{
					"doctype": "User Permission",
					"user": user,
					"allow": "Company",
					"for_value": company,
					"apply_to_all_doctypes": 1,
				}
			).insert(ignore_permissions=True)

	def _make_supervisor(self):
		email = self._make_user("Production Supervisor")
		make_employee(email, company="_Test Company")
		return email

	def _make_job_card(self, qty=100):
		original_default_bom = frappe.db.get_value("Item", "_Test FG Item 2", "default_bom")
		planned_qty_before = flt(
			frappe.db.get_value(
				"Bin",
				{"item_code": "_Test FG Item 2", "warehouse": "_Test Warehouse 1 - _TC"},
				"planned_qty",
			)
		)
		bom = frappe.copy_doc(self.globalTestRecords["BOM"][2])
		bom.set_rate_of_sub_assembly_item_based_on_bom = 0
		bom.rm_cost_as_per = "Valuation Rate"
		bom.is_default = 0
		bom.items[0].uom = "_Test UOM 1"
		bom.items[0].conversion_factor = 5
		bom.insert()
		# Capacity scheduling is unrelated to worker-reporting invariants and can
		# exhaust the shared test workstation after committed concurrency fixtures.
		with self.change_settings("Manufacturing Settings", {"disable_capacity_planning": 1}):
			work_order = make_wo_order_test_record(
				item="_Test FG Item 2",
				bom_no=bom.name,
				qty=qty,
				skip_transfer=1,
			)
		job_card_name = frappe.get_all(
			"Job Card",
			filters={"work_order": work_order.name, "docstatus": 0},
			pluck="name",
			order_by="sequence_id, creation",
			limit=1,
		)[0]
		job_card = frappe.get_doc("Job Card", job_card_name)
		job_card.flags.worker_reporting_test_bom = bom.name
		job_card.flags.worker_reporting_original_default_bom = original_default_bom
		job_card.flags.worker_reporting_planned_qty_before = planned_qty_before
		job_card.flags.worker_reporting_fg_warehouse = work_order.fg_warehouse
		return job_card

	def _make_rate(self, job_card, wage_type="Piecework", rate=5, valid_from=None):
		with self.set_user(self.wage_manager):
			return frappe.get_doc(
				{
					"doctype": "Operation Wage Rate",
					"company": job_card.company,
					"operation": job_card.operation,
					"wage_type": wage_type,
					"rate": rate,
					"valid_from": valid_from or nowdate(),
					"enabled": 1,
				}
			).insert()

	def _assign(self, job_card):
		with self.set_user(self.supervisor):
			return service.assign_worker(job_card.name, self.worker)

	def _setup_flow(self, qty=100, wage_type="Piecework", rate=5, valid_from=None):
		job_card = self._make_job_card(qty)
		self._make_rate(job_card, wage_type=wage_type, rate=rate, valid_from=valid_from)
		assignment = self._assign(job_card)
		return job_card, assignment

	def _submit(self, assignment, qty, minutes=0, request_id=None):
		with self.set_user(self.worker_user):
			return service.submit_work_report(
				assignment.name,
				qty,
				minutes,
				request_id or random_string(16),
			)

	def _approve(self, report):
		with self.set_user(self.supervisor):
			return service.approve_work_report(report.name)

	def _insert_raw_pending_report(
		self,
		name,
		*,
		job_card,
		assignment,
		employee,
		qty,
		minutes=0,
		wage_type="Piecework",
	):
		frappe.db.sql(
			"""
			insert into `tabJob Card Work Report`
				(name, owner, modified_by, creation, modified, docstatus,
				 request_key, assignment, job_card, company, operation, operation_id,
				 employee, employee_user, labor_date, wage_type, status,
				 completed_qty, reported_minutes, rate, wage_amount)
			values
				(%(name)s, 'Administrator', 'Administrator', now(6), now(6), 0,
				 %(request_key)s, %(assignment)s, %(job_card)s, '_Test Company', '_Test Operation 1', 'raw-operation-row',
				 %(employee)s, %(employee_user)s, current_date(), %(wage_type)s, 'Pending Approval',
				 %(qty)s, %(minutes)s, 1, 1)
			""",
			{
				"name": name,
				"request_key": f"raw-{name}",
				"assignment": assignment,
				"job_card": job_card,
				"employee": employee,
				"employee_user": self.worker_user,
				"wage_type": wage_type,
				"qty": qty,
				"minutes": minutes,
			},
		)

	def _delete_test_rows_and_children(self, doctype, names):
		names = list(dict.fromkeys(name for name in names if name))
		if not names:
			return
		for field in frappe.get_meta(doctype).get_table_fields():
			frappe.db.delete(field.options, {"parent": ["in", names]})
		frappe.db.delete(doctype, {"name": ["in", names]})

	def _cleanup_committed_concurrency_fixture(self, job_card, rate_name, raw_name):
		"""Remove every row made durable solely for a two-connection test."""
		frappe.set_user("Administrator")
		work_order = job_card.work_order
		job_cards = frappe.get_all("Job Card", {"work_order": work_order}, pluck="name")
		assignments = frappe.get_all(
			"Job Card Worker Assignment", {"work_order": work_order}, pluck="name"
		)
		reports = [raw_name]
		if assignments:
			reports.extend(
				frappe.get_all(
					"Job Card Work Report",
					{"assignment": ["in", assignments]},
					pluck="name",
				)
			)
		self._delete_test_rows_and_children("Job Card Work Report", reports)
		self._delete_test_rows_and_children("Job Card Worker Assignment", assignments)
		frappe.db.set_value(
			"Work Order",
			work_order,
			"custom_worker_reporting_enabled",
			0,
			update_modified=False,
		)
		for job_card_name in job_cards:
			frappe.db.set_value(
				"Job Card",
				job_card_name,
				{
					"custom_worker_reporting_enabled": 0,
					"custom_worker_reporting_supervisor": None,
				},
				update_modified=False,
			)
		work_order_doc = frappe.get_doc("Work Order", work_order, for_update=True)
		if work_order_doc.docstatus == 1:
			# Preserve ERPNext's planned/reserved quantity rollbacks. Directly
			# deleting a submitted Work Order leaves shared Bin aggregates stale.
			work_order_doc.cancel()
		self._delete_test_rows_and_children("Job Card", job_cards)
		self._delete_test_rows_and_children("Work Order", [work_order])
		self._delete_test_rows_and_children(
			"BOM", [job_card.flags.get("worker_reporting_test_bom")]
		)
		frappe.db.delete("Operation Wage Rate", {"name": rate_name})

		users = [self.worker_user, self.supervisor, self.wage_manager]
		employees = frappe.get_all("Employee", {"user_id": ["in", users]}, pluck="name")
		contacts = frappe.get_all("Contact", {"user": ["in", users]}, pluck="name")
		frappe.db.delete("User Permission", {"user": ["in", users]})
		for employee in employees:
			if frappe.db.exists("Employee", employee):
				frappe.delete_doc(
					"Employee",
					employee,
					ignore_permissions=True,
					force=True,
					delete_permanently=True,
				)
		for user in users:
			if frappe.db.exists("User", user):
				frappe.delete_doc(
					"User",
					user,
					ignore_permissions=True,
					force=True,
					delete_permanently=True,
				)
		for contact in contacts:
			if frappe.db.exists("Contact", contact):
				frappe.delete_doc(
					"Contact",
					contact,
					ignore_permissions=True,
					force=True,
					delete_permanently=True,
				)
		self.assertFalse(any(frappe.db.exists("Contact", contact) for contact in contacts))
		original_default_bom = job_card.flags.get("worker_reporting_original_default_bom")
		self.assertEqual(
			frappe.db.get_value("Item", "_Test FG Item 2", "default_bom"),
			original_default_bom,
		)
		self.assertTrue(frappe.db.exists("BOM", original_default_bom))
		self.assertTrue(frappe.db.get_value("BOM", original_default_bom, "is_default"))
		self.assertEqual(
			flt(
				frappe.db.get_value(
					"Bin",
					{
						"item_code": "_Test FG Item 2",
						"warehouse": job_card.flags.get("worker_reporting_fg_warehouse"),
					},
					"planned_qty",
				)
			),
			flt(job_card.flags.get("worker_reporting_planned_qty_before")),
		)
		frappe.db.commit()

	def test_pending_does_not_touch_job_card_and_approval_posts_once(self):
		job_card, assignment = self._setup_flow(qty=100)
		report = self._submit(assignment, 30, request_id="pending-then-approve")

		job_card.reload()
		self.assertEqual(report.status, "Pending Approval")
		self.assertEqual(job_card.total_completed_qty, 0)
		self.assertEqual(len(job_card.time_logs), 0)

		approved = self._approve(report)
		job_card.reload()
		approved.reload()
		self.assertEqual(approved.status, "Approved")
		self.assertEqual(job_card.total_completed_qty, 30)
		self.assertEqual(job_card.pending_qty, 0)
		self.assertEqual(job_card.process_loss_qty, 0)
		self.assertEqual(len(job_card.time_logs), 1)
		row = job_card.time_logs[0]
		self.assertEqual(row.employee, self.worker)
		self.assertEqual(row.custom_reported_employee, self.worker)
		self.assertEqual(row.custom_job_card_work_report, approved.name)
		self.assertFalse(row.from_time)
		self.assertFalse(row.to_time)
		self.assertEqual(row.time_in_mins, 0)

		self._approve(report)
		job_card.reload()
		self.assertEqual(job_card.total_completed_qty, 30)
		self.assertEqual(len(job_card.time_logs), 1)

	def test_fixture_uses_an_explicit_nondefault_bom(self):
		original_default = frappe.db.get_value("Item", "_Test FG Item 2", "default_bom")
		job_card = self._make_job_card(qty=10)
		test_bom = job_card.flags.worker_reporting_test_bom
		self.assertEqual(job_card.bom_no, test_bom)
		self.assertNotEqual(test_bom, original_default)
		self.assertFalse(frappe.db.get_value("BOM", test_bom, "is_default"))
		self.assertEqual(
			frappe.db.get_value("Item", "_Test FG Item 2", "default_bom"), original_default
		)
		self.assertTrue(frappe.db.exists("BOM", original_default))
		self.assertTrue(frappe.db.get_value("BOM", original_default, "is_default"))

	def test_metadata_keeps_worker_writes_api_only_and_uses_unique_backlink(self):
		from process_simplification import hooks

		report_meta = frappe.get_meta("Job Card Work Report")
		worker_permissions = [row for row in report_meta.permissions if row.role == "Production Worker"]
		self.assertEqual(worker_permissions, [])
		for role in ("Production Supervisor", "Production Wage Manager", "System Manager"):
			permission = next(row for row in report_meta.permissions if row.role == role)
			self.assertEqual(permission.read, 1)
			self.assertFalse(permission.create)
			self.assertFalse(permission.write)
		self.assertFalse(hasattr(hooks, "override_whitelisted_methods"))
		self.assertFalse(hasattr(hooks, "page_js"))
		backlink = frappe.get_meta("Job Card Time Log").get_field("custom_job_card_work_report")
		self.assertTrue(backlink.unique)
		self.assertEqual(backlink.options, "Job Card Work Report")
		work_order_marker = frappe.get_meta("Work Order").get_field(
			"custom_worker_reporting_enabled"
		)
		self.assertTrue(work_order_marker.read_only)
		self.assertFalse(work_order_marker.allow_on_submit)
		self.assertTrue(work_order_marker.no_copy)
		self.assertTrue(frappe.get_meta("Job Card Worker Assignment").get_field("job_card").unique)
		for role in ("Production Wage Manager", "System Manager"):
			rate_permission = next(
				row for row in frappe.get_meta("Operation Wage Rate").permissions if row.role == role
			)
			self.assertFalse(rate_permission.delete)
		report_indexes = {
			row.Key_name
			for row in frappe.db.sql("show index from `tabJob Card Work Report`", as_dict=True)
		}
		self.assertTrue(
			{"jcwr_job_status_name", "jcwr_employee_day_status", "jcwr_assignment_status"}.issubset(
				report_indexes
			)
		)
		assignment_indexes = {
			row.Key_name
			for row in frappe.db.sql("show index from `tabJob Card Worker Assignment`", as_dict=True)
		}
		self.assertIn("jcwa_work_order_status", assignment_indexes)

	def test_worker_cannot_insert_report_directly(self):
		_, assignment = self._setup_flow(qty=100)
		with self.set_user(self.worker_user):
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(
					{
						"doctype": "Job Card Work Report",
						"assignment": assignment.name,
						"completed_qty": 1,
					}
				).insert()

	def test_request_id_is_idempotent_and_cannot_be_reused_with_different_values(self):
		_, assignment = self._setup_flow(qty=20)
		first = self._submit(assignment, 20, request_id="same-request")
		second = self._submit(assignment, 20, request_id="same-request")
		self.assertEqual(first.name, second.name)
		self._approve(first)
		after_final_approval = self._submit(assignment, 20, request_id="same-request")
		self.assertEqual(first.name, after_final_approval.name)
		with self.assertRaises(frappe.ValidationError):
			self._submit(assignment, 21, request_id="same-request")
		self.assertEqual(
			frappe.db.count("Job Card Work Report", {"assignment": assignment.name}),
			1,
		)

	def test_one_job_card_has_exactly_one_worker_assignment(self):
		job_card, first_assignment = self._setup_flow(qty=100)
		second_user = self._make_worker()
		second_employee = frappe.db.get_value("Employee", {"user_id": second_user}, "name")
		with self.set_user(self.supervisor):
			with self.assertRaises(frappe.ValidationError):
				service.assign_worker(job_card.name, second_employee)
		self.assertEqual(
			frappe.db.get_value("Job Card Worker Assignment", {"job_card": job_card.name}, "name"),
			first_assignment.name,
		)

	def test_fractional_quantity_and_piecework_retry_use_normalized_values(self):
		job_card, assignment = self._setup_flow(qty=1)
		first = self._submit(assignment, 0.1, minutes=99, request_id="fractional-piecework")
		retry = self._submit(assignment, 0.1001, minutes=123, request_id="fractional-piecework")
		self.assertEqual(first.name, retry.name)
		self.assertEqual(first.reported_minutes, 0)
		self._approve(first)
		self._approve(self._submit(assignment, 0.2))
		self._approve(self._submit(assignment, 0.7))
		job_card.reload()
		self.assertEqual(job_card.total_completed_qty, 1)
		self.assertEqual(job_card.docstatus, 1)

	def test_repeatable_read_pending_conflict_fails_closed(self):
		job_card, assignment = self._setup_flow(qty=100)
		rate_name = frappe.db.get_value(
			"Operation Wage Rate",
			{"company": job_card.company, "operation": job_card.operation, "enabled": 1},
			"name",
		)
		raw_name = f"RAW-{random_string(12)}"
		try:
			# Commit only this isolated concurrency fixture so the second connection
			# can add the winning Pending row before the primary request locks the card.
			frappe.db.commit()
			# Establish a repeatable-read snapshot before the other connection commits.
			frappe.db.get_value("Job Card Work Report", raw_name, "name")
			with self.secondary_connection():
				self._insert_raw_pending_report(
					raw_name,
					job_card=job_card.name,
					assignment=assignment.name,
					employee=self.worker,
					qty=60,
				)
				frappe.db.commit()
			with self.primary_connection():
				# MariaDB with innodb_snapshot_isolation may surface a retryable
				# serialization conflict; otherwise the current locking read validates it.
				with self.assertRaises((frappe.ValidationError, frappe.QueryDeadlockError)):
					self._submit(assignment, 60, request_id="rr-primary-capacity")
		finally:
			with self.primary_connection():
				frappe.db.rollback()
			with self.secondary_connection():
				self._cleanup_committed_concurrency_fixture(job_card, rate_name, raw_name)

	def test_repeatable_read_daily_minutes_conflict_fails_closed(self):
		job_card, assignment = self._setup_flow(qty=100, wage_type="Time", rate=20)
		rate_name = frappe.db.get_value(
			"Operation Wage Rate",
			{"company": job_card.company, "operation": job_card.operation, "enabled": 1},
			"name",
		)
		raw_name = f"RAW-{random_string(12)}"
		try:
			frappe.db.commit()
			frappe.db.get_value("Job Card Work Report", raw_name, "name")
			with self.secondary_connection():
				self._insert_raw_pending_report(
					raw_name,
					job_card="CONCURRENT-OTHER-JOB-CARD",
					assignment="CONCURRENT-OTHER-ASSIGNMENT",
					employee=self.worker,
					qty=1,
					minutes=800,
					wage_type="Time",
				)
				frappe.db.commit()
			with self.primary_connection():
				with self.assertRaises((frappe.ValidationError, frappe.QueryDeadlockError)):
					self._submit(assignment, 1, minutes=800, request_id="rr-primary-minutes")
		finally:
			with self.primary_connection():
				frappe.db.rollback()
			with self.secondary_connection():
				self._cleanup_committed_concurrency_fixture(job_card, rate_name, raw_name)

	def test_rejection_releases_quantity_and_requires_a_new_report(self):
		job_card, assignment = self._setup_flow(qty=100)
		report = self._submit(assignment, 60)
		with self.set_user(self.supervisor):
			service.reject_work_report(report.name, "数量填写错误")
		job_card.reload()
		report.reload()
		self.assertEqual(report.status, "Rejected")
		self.assertEqual(job_card.total_completed_qty, 0)
		self.assertEqual(len(job_card.time_logs), 0)

		replacement = self._submit(assignment, 100)
		self.assertNotEqual(replacement.name, report.name)
		self._approve(replacement)
		job_card.reload()
		self.assertEqual(job_card.total_completed_qty, 100)
		self.assertEqual(job_card.docstatus, 1)
		with self.set_user(self.supervisor):
			self.assertEqual(service.reject_work_report(report.name, "数量填写错误").name, report.name)

	def test_job_card_snapshot_conflict_blocks_approval_but_still_allows_rejection(self):
		job_card, assignment = self._setup_flow(qty=100)
		report = self._submit(assignment, 10)
		frappe.db.set_value("Job Card", job_card.name, "for_quantity", 101, update_modified=False)

		with self.set_user(self.supervisor):
			dashboard_row = next(
				row for row in service.get_review_dashboard()["reports"] if row.name == report.name
			)
			self.assertFalse(dashboard_row.can_approve)
			self.assertEqual(dashboard_row.approve_block_code, "ASSIGNMENT_SNAPSHOT_CHANGED")
			self.assertTrue(dashboard_row.can_reject)
			service.reject_work_report(report.name, "任务量已变化，退回重建派工")

		report.reload()
		self.assertEqual(report.status, "Rejected")

	def test_partial_job_card_cannot_submit_but_full_approved_quantity_can(self):
		job_card, assignment = self._setup_flow(qty=100)
		first = self._submit(assignment, 30)
		self._approve(first)
		job_card.reload()
		with self.set_user("Administrator"):
			with self.assertRaises(frappe.ValidationError):
				job_card.submit()

		second = self._submit(assignment, 70)
		self._approve(second)
		job_card.reload()
		assignment.reload()
		self.assertEqual(job_card.docstatus, 1)
		self.assertEqual(assignment.status, "Completed")
		self._approve(second)
		with self.set_user("Administrator"):
			with self.assertRaises(frappe.ValidationError):
				job_card.cancel()

	def test_native_unlinked_time_log_cannot_bypass_approval(self):
		job_card, _ = self._setup_flow(qty=100)
		job_card.append("time_logs", {"employee": self.worker, "completed_qty": 1})
		with self.set_user("Administrator"):
			with self.assertRaises(frappe.ValidationError):
				job_card.save()
		self.assertEqual(frappe.db.get_value("Job Card", job_card.name, "total_completed_qty"), 0)

	def test_managed_job_card_cannot_be_discarded_with_assignment_or_report_history(self):
		assigned_job_card, assignment = self._setup_flow(qty=100)
		with self.set_user("Administrator"):
			with self.assertRaises(frappe.ValidationError):
				assigned_job_card.discard()
		with self.set_user(self.supervisor):
			service.unassign_worker(assignment.name)
		self.assertFalse(
			frappe.db.get_value(
				"Work Order", assigned_job_card.work_order, "custom_worker_reporting_enabled"
			)
		)
		with self.set_user("Administrator"):
			assigned_job_card.reload()
			assigned_job_card.discard()
		self.assertEqual(assigned_job_card.docstatus, 2)

		pending_job_card = self._make_job_card(qty=100)
		pending_assignment = self._assign(pending_job_card)
		self._submit(pending_assignment, 10)
		with self.set_user("Administrator"):
			with self.assertRaises(frappe.ValidationError):
				pending_job_card.discard()

		approved_job_card = self._make_job_card(qty=100)
		approved_assignment = self._assign(approved_job_card)
		self._approve(self._submit(approved_assignment, 10))
		with self.set_user("Administrator"):
			with self.assertRaises(frappe.ValidationError):
				approved_job_card.discard()

	def test_worker_account_with_standard_manufacturing_role_is_rejected(self):
		job_card = self._make_job_card(100)
		self._make_rate(job_card)
		frappe.get_doc("User", self.worker_user).add_roles("Manufacturing User")
		with self.set_user(self.supervisor):
			with self.assertRaises(frappe.ValidationError):
				service.assign_worker(job_card.name, self.worker)

	def test_worker_role_drift_after_assignment_blocks_new_reports(self):
		_, assignment = self._setup_flow(qty=100)
		frappe.get_doc("User", self.worker_user).add_roles("Production Supervisor")
		with self.assertRaises(frappe.ValidationError):
			self._submit(assignment, 1)

	def test_account_rebinding_keeps_employee_assignment_and_report_audit_valid(self):
		first_job_card, first_assignment = self._setup_flow(qty=10)
		second_job_card = self._make_job_card(qty=10)
		second_assignment = self._assign(second_job_card)
		approved_report = self._submit(first_assignment, 3)
		rejected_report = self._submit(second_assignment, 4)

		frappe.db.set_value("Employee", self.worker, "user_id", None)
		frappe.db.set_value("User", self.worker_user, "enabled", 0)
		self._approve(approved_report)
		with self.set_user(self.supervisor):
			service.reject_work_report(rejected_report.name, "离职前记录仍需审核")
		approved_report.reload()
		rejected_report.reload()
		self.assertEqual(approved_report.status, "Approved")
		self.assertEqual(approved_report.employee_user, self.worker_user)
		self.assertEqual(rejected_report.status, "Rejected")

		new_user = self._make_user("Production Worker")
		frappe.db.set_value("Employee", self.worker, "user_id", new_user)
		with self.set_user(new_user):
			new_report = service.submit_work_report(
				first_assignment.name,
				7,
				0,
				"rebound-user-report",
			)
		self.assertEqual(new_report.employee_user, new_user)
		self._approve(new_report)
		first_job_card.reload()
		self.assertEqual(first_job_card.docstatus, 1)

	def test_job_card_identity_and_quantity_are_frozen_after_assignment(self):
		job_card, _ = self._setup_flow(qty=100)
		with self.set_user("Administrator"):
			job_card.operation_id = f"{job_card.operation_id}-tampered"
			with self.assertRaises(frappe.ValidationError):
				job_card.save()
			job_card.reload()
			job_card.for_quantity = 101
			with self.assertRaises(frappe.ValidationError):
				job_card.save()

	def test_reports_are_append_only_even_for_pending_and_rejected_states(self):
		_, assignment = self._setup_flow(qty=100)
		pending = self._submit(assignment, 10)
		with self.set_user("Administrator"):
			with self.assertRaises(frappe.ValidationError):
				pending.delete(ignore_permissions=True)
		with self.set_user(self.supervisor):
			service.reject_work_report(pending.name, "审计保留")
		pending.reload()
		with self.set_user("Administrator"):
			with self.assertRaises(frappe.ValidationError):
				pending.delete(ignore_permissions=True)

	def test_permission_hooks_deny_standard_write_and_delete(self):
		_, assignment = self._setup_flow(qty=100)
		report = self._submit(assignment, 10)
		with self.set_user(self.supervisor):
			self.assertTrue(
				frappe.has_permission("Job Card Worker Assignment", "read", doc=assignment)
			)
			self.assertTrue(frappe.has_permission("Job Card Work Report", "read", doc=report))
			self.assertFalse(frappe.has_permission("Job Card Work Report", "write", doc=report))
			self.assertFalse(frappe.has_permission("Job Card Work Report", "delete", doc=report))

	def test_final_approval_rolls_back_if_report_state_cannot_be_saved(self):
		job_card, assignment = self._setup_flow(qty=10)
		report = self._submit(assignment, 10)
		frappe.db.savepoint("before_atomic_approval")
		try:
			with patch.object(JobCardWorkReport, "validate", side_effect=frappe.ValidationError("injected")):
				with self.set_user(self.supervisor):
					with self.assertRaises(frappe.ValidationError):
						service.approve_work_report(report.name)
		finally:
			frappe.db.rollback(save_point="before_atomic_approval")
		job_card.reload()
		report.reload()
		assignment.reload()
		self.assertEqual(job_card.docstatus, 0)
		self.assertEqual(job_card.total_completed_qty, 0)
		self.assertEqual(len(job_card.time_logs), 0)
		self.assertEqual(report.status, "Pending Approval")
		self.assertEqual(assignment.status, "Active")

	def test_wage_rate_period_is_unique_and_immutable(self):
		job_card, _ = self._setup_flow(qty=100)
		with self.set_user(self.wage_manager):
			with self.assertRaises(frappe.ValidationError):
				frappe.get_doc(
					{
						"doctype": "Operation Wage Rate",
						"company": job_card.company,
						"operation": job_card.operation,
						"wage_type": "Piecework",
						"rate": 9,
						"valid_from": nowdate(),
						"enabled": 1,
					}
				).insert()
			rate = frappe.get_doc(
				"Operation Wage Rate",
				frappe.db.get_value("Operation Wage Rate", {"operation": job_card.operation}, "name"),
			)
			rate.valid_to = add_days(nowdate(), 30)
			with self.assertRaises(frappe.ValidationError):
				rate.save()
			rate.reload()
			rate.enabled = 0
			rate.save()
			rate.enabled = 1
			with self.assertRaises(frappe.ValidationError):
				rate.save()
			rate.reload()
		with self.set_user("Administrator"):
			with self.assertRaises(frappe.ValidationError):
				rate.delete(ignore_permissions=True)

	def test_wage_manager_is_restricted_to_explicit_company_permissions(self):
		job_card = self._make_job_card(10)
		rate = self._make_rate(job_card)
		with self.set_user(self.wage_manager):
			self.assertEqual(summary.get_wage_management_context()["companies"], ["_Test Company"])
			self.assertIn(rate.name, frappe.get_list("Operation Wage Rate", pluck="name", limit=0))
			self.assertTrue(frappe.has_permission("Operation Wage Rate", "read", doc=rate))
			with self.assertRaises(frappe.PermissionError):
				service.search_wage_employees("_Test Company 1")
			with self.assertRaises(frappe.PermissionError):
				summary.build_monthly_summaries("_Test Company 1", nowdate())

		unscoped_manager = self._make_user("Production Wage Manager")
		with self.set_user(unscoped_manager):
			self.assertEqual(frappe.get_list("Operation Wage Rate", pluck="name", limit=0), [])
			self.assertFalse(frappe.has_permission("Operation Wage Rate", "read", doc=rate))
		with self.set_user("Administrator"):
			self.assertIn(rate.name, frappe.get_list("Operation Wage Rate", pluck="name", limit=0))

	def test_worker_cannot_be_a_reviewer_or_review_their_own_report(self):
		job_card, assignment = self._setup_flow(qty=10)
		report = self._submit(assignment, 10)
		frappe.get_doc("User", self.worker_user).add_roles("Production Supervisor")
		with self.set_user(self.worker_user):
			with self.assertRaises(frappe.PermissionError):
				service.approve_work_report(report.name)
		with self.set_user(self.wage_manager):
			with self.assertRaises(frappe.PermissionError):
				service.approve_work_report(report.name)

	def test_administrator_keeps_native_all_role_semantics_in_locked_actions(self):
		job_card = self._make_job_card(10)
		self._make_rate(job_card)
		with self.set_user("Administrator"):
			assignment = service.assign_worker(job_card.name, self.worker, supervisor=self.supervisor)
		report = self._submit(assignment, 10)
		with self.set_user("Administrator"):
			approved = service.approve_work_report(report.name)
			self.assertTrue(summary.get_wage_management_context()["companies"])
		self.assertEqual(approved.status, "Approved")

	def test_enforced_native_time_logs_block_assignment(self):
		job_card = self._make_job_card(100)
		self._make_rate(job_card)
		with self.change_settings("Manufacturing Settings", {"enforce_time_logs": 1}):
			with self.set_user(self.supervisor):
				with self.assertRaises(frappe.ValidationError):
					service.assign_worker(job_card.name, self.worker)

	def test_assignment_search_only_returns_job_cards_the_backend_can_accept(self):
		job_card = self._make_job_card(100)
		with self.set_user(self.supervisor):
			self.assertNotIn(job_card.name, {row[0] for row in service.search_draft_job_cards()})
		self._make_rate(job_card)
		with self.set_user(self.supervisor):
			self.assertIn(job_card.name, {row[0] for row in service.search_draft_job_cards()})
			with self.change_settings("Manufacturing Settings", {"enforce_time_logs": 1}):
				self.assertEqual(service.search_draft_job_cards(), [])
		frappe.db.set_value("Work Order", job_card.work_order, "status", "Closed", update_modified=False)
		with self.set_user(self.supervisor):
			self.assertNotIn(job_card.name, {row[0] for row in service.search_draft_job_cards()})

	def test_work_order_cannot_close_while_managed_job_card_is_still_draft(self):
		from frappe.desk.form.save import savedocs

		from erpnext.manufacturing.doctype.work_order.work_order import close_work_order
		from process_simplification.production_reporting.stock_entry import before_submit

		job_card, assignment = self._setup_flow(qty=10)
		self.assertTrue(
			frappe.db.get_value(
				"Work Order", job_card.work_order, "custom_worker_reporting_enabled"
			)
		)
		self._approve(self._submit(assignment, 3))
		job_card.reload()
		self.assertEqual(job_card.docstatus, 0)
		with self.set_user("Administrator"):
			with self.assertRaisesRegex(frappe.ValidationError, "worker-reporting Job Cards"):
				close_work_order(job_card.work_order, "Closed")
			with self.assertRaisesRegex(frappe.ValidationError, "worker-reporting Job Cards"):
				frappe.get_doc("Work Order", job_card.work_order).cancel()
			# The standard form reaches save() directly, bypassing doc.cancel(). It
			# must also ignore a crafted read-only marker value from the payload.
			work_order = frappe.get_doc("Work Order", job_card.work_order)
			work_order.custom_worker_reporting_enabled = 0
			with self.assertRaisesRegex(frappe.ValidationError, "worker-reporting Job Cards"):
				savedocs(work_order.as_json(), "Cancel")
			with self.assertRaisesRegex(frappe.ValidationError, "worker-reporting Job Cards"):
				before_submit(
					frappe._dict(purpose="Manufacture", work_order=job_card.work_order)
				)
		self.assertNotEqual(frappe.db.get_value("Work Order", job_card.work_order, "status"), "Closed")

	def test_submitted_work_order_marker_cannot_be_cleared_by_document_save(self):
		job_card, _ = self._setup_flow(qty=10)
		with self.set_user("Administrator"):
			work_order = frappe.get_doc("Work Order", job_card.work_order)
			self.assertEqual(work_order.docstatus, 1)
			work_order.custom_worker_reporting_enabled = 0
			with self.assertRaises(frappe.ValidationError):
				work_order.save()
		self.assertTrue(
			frappe.db.get_value(
				"Work Order", job_card.work_order, "custom_worker_reporting_enabled"
			)
		)

	def test_time_wage_freezes_rate_and_enforces_natural_day_limit(self):
		job_card, assignment = self._setup_flow(qty=100, wage_type="Time", rate=20)
		with self.assertRaises(frappe.ValidationError):
			self._submit(assignment, 1, minutes=0.5)
		report = self._submit(assignment, 30, minutes=1440)
		self.assertEqual(report.wage_amount, 480)
		self._approve(report)
		with self.assertRaises(frappe.ValidationError):
			self._submit(assignment, 1, minutes=1)
		with self.set_user(self.wage_manager):
			rate = frappe.get_doc("Operation Wage Rate", report.wage_rate)
			rate.rate = 100
			rate.save()
		report.reload()
		self.assertEqual(report.rate, 20)
		self.assertEqual(report.wage_amount, 480)
		job_card.reload()
		self.assertEqual(job_card.total_time_in_mins, 0)

	def test_monthly_summary_contains_approved_reports(self):
		previous_month = add_months(get_first_day(nowdate()), -1)
		production_day = add_days(previous_month, 14)
		# A non-zero microsecond keeps Frappe's optimistic-lock timestamp string
		# stable across MariaDB round trips while the clock is frozen.
		production_moment = datetime.combine(production_day, time(9, 30, 0, 123456))
		with self.freeze_time(production_moment):
			job_card, assignment = self._setup_flow(qty=10, valid_from=previous_month)
			report = self._submit(assignment, 10, request_id="previous-month-report")
			self._approve(report)
			job_card.reload()

		with self.set_user(self.wage_manager):
			result = summary.build_monthly_summaries("_Test Company", previous_month, self.worker)
			self.assertEqual(len(result["summaries"]), 1)
			doc = summary.confirm_monthly_summary(result["summaries"][0])
		doc.reload()
		report.reload()
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(len(doc.details), 1)
		self.assertEqual(doc.total_amount, report.wage_amount)
		self.assertEqual(report.status, "Approved")
		self.assertEqual(report.monthly_summary, doc.name)

		with self.set_user("Administrator"):
			with self.assertRaises(frappe.ValidationError):
				doc.cancel()
		doc.reload()
		report.reload()
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(report.monthly_summary, doc.name)

	def test_cross_month_partial_approval_is_paid_before_job_card_finishes(self):
		previous_month = add_months(get_first_day(nowdate()), -1)
		production_day = add_days(previous_month, 14)
		production_moment = datetime.combine(production_day, time(10, 15, 0, 123456))
		with self.freeze_time(production_moment):
			job_card, assignment = self._setup_flow(qty=10, valid_from=previous_month)
			previous_report = self._submit(assignment, 3, request_id="cross-month-partial")
			self._approve(previous_report)
			job_card.reload()
			self.assertEqual(job_card.docstatus, 0)

		with self.set_user(self.wage_manager):
			result = summary.build_monthly_summaries("_Test Company", previous_month, self.worker)
			doc = summary.confirm_monthly_summary(result["summaries"][0])
		self.assertEqual([row.source_report for row in doc.details], [previous_report.name])

		current_report = self._submit(assignment, 7, request_id="cross-month-finish")
		self._approve(current_report)
		job_card.reload()
		previous_report.reload()
		self.assertEqual(job_card.docstatus, 1)
		self.assertEqual(previous_report.monthly_summary, doc.name)

	def test_current_month_summary_cannot_be_confirmed(self):
		job_card, assignment = self._setup_flow(qty=10)
		report = self._submit(assignment, 10)
		self._approve(report)
		with self.set_user(self.wage_manager):
			result = summary.build_monthly_summaries("_Test Company", nowdate(), self.worker)
			with self.assertRaises(frappe.ValidationError):
				summary.confirm_monthly_summary(result["summaries"][0])
