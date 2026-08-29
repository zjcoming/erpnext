from __future__ import annotations

from datetime import datetime, time, timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import (
	add_days,
	add_months,
	flt,
	getdate,
	get_datetime,
	get_first_day,
	now_datetime,
	nowdate,
	random_string,
)

from process_simplification.production_reporting import service, summary
from process_simplification.process_simplification.doctype.job_card_work_report.job_card_work_report import (
	JobCardWorkReport,
)


class TestWorkerReporting(IntegrationTestCase):
	TEST_COMPANY = "Worker Reporting Test Company"
	TEST_COMPANY_ABBR = "WRT"
	OTHER_COMPANY = "Worker Reporting Other Company"
	OTHER_COMPANY_ABBR = "WRO"
	TEST_OPERATION = "Worker Reporting Test Operation"
	TEST_WORKSTATION = "Worker Reporting Test Workstation"
	TEST_FINISHED_GOOD = "WRT-FG-001"
	TEST_RAW_MATERIAL = "WRT-RM-001"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._ensure_master_fixtures()

	@classmethod
	def _ensure_company(cls, company_name, abbr):
		if not frappe.db.exists("Company", company_name):
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": company_name,
					"abbr": abbr,
					"default_currency": "INR",
					"country": "India",
					"create_chart_of_accounts_based_on": "Standard Template",
				}
			).insert(ignore_permissions=True)

	@classmethod
	def _ensure_warehouse(cls, warehouse_name):
		name = "{0} - {1}".format(warehouse_name, cls.TEST_COMPANY_ABBR)
		if not frappe.db.exists("Warehouse", name):
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": warehouse_name,
					"parent_warehouse": "All Warehouses - {0}".format(
						cls.TEST_COMPANY_ABBR
					),
					"company": cls.TEST_COMPANY,
				}
			).insert(ignore_permissions=True)
		return name

	@classmethod
	def _ensure_item(cls, item_code, *, is_sales_item=False, is_purchase_item=False):
		if not frappe.db.exists("Item", item_code):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item_code,
					"item_name": item_code,
					"description": item_code,
					"item_group": "Products",
					"stock_uom": "Nos",
					"is_stock_item": 1,
					"is_sales_item": int(is_sales_item),
					"is_purchase_item": int(is_purchase_item),
					"include_item_in_manufacturing": 1,
					"valuation_rate": 1,
				}
			).insert(ignore_permissions=True)

	@classmethod
	def _ensure_master_fixtures(cls):
		cls._ensure_company(cls.TEST_COMPANY, cls.TEST_COMPANY_ABBR)
		cls._ensure_company(cls.OTHER_COMPANY, cls.OTHER_COMPANY_ABBR)
		cls.source_warehouse = cls._ensure_warehouse("Worker Reporting Source")
		cls.wip_warehouse = cls._ensure_warehouse("Worker Reporting WIP")
		cls.fg_warehouse = cls._ensure_warehouse("Worker Reporting Finished Goods")
		cls._ensure_item(cls.TEST_RAW_MATERIAL, is_purchase_item=True)
		cls._ensure_item(cls.TEST_FINISHED_GOOD, is_sales_item=True)

		if not frappe.db.exists("Workstation", cls.TEST_WORKSTATION):
			frappe.get_doc(
				{"doctype": "Workstation", "workstation_name": cls.TEST_WORKSTATION}
			).insert(ignore_permissions=True)
		if not frappe.db.exists("Operation", cls.TEST_OPERATION):
			frappe.get_doc(
				{
					"doctype": "Operation",
					"name": cls.TEST_OPERATION,
					"workstation": cls.TEST_WORKSTATION,
				}
			).insert(ignore_permissions=True)

		cls.master_bom = frappe.db.get_value(
			"BOM",
			{
				"item": cls.TEST_FINISHED_GOOD,
				"company": cls.TEST_COMPANY,
				"docstatus": 1,
				"is_active": 1,
				"is_default": 1,
			},
			"name",
		)
		if not cls.master_bom:
			bom = frappe.get_doc(
				{
					"doctype": "BOM",
					"item": cls.TEST_FINISHED_GOOD,
					"company": cls.TEST_COMPANY,
					"currency": "INR",
					"quantity": 1,
					"is_active": 1,
					"is_default": 1,
					"with_operations": 1,
					"operations": [
						{
							"operation": cls.TEST_OPERATION,
							"workstation": cls.TEST_WORKSTATION,
							"time_in_mins": 10,
							"operating_cost": 1,
						}
					],
					"items": [
						{
							"item_code": cls.TEST_RAW_MATERIAL,
							"qty": 1,
							"uom": "Nos",
							"stock_uom": "Nos",
							"rate": 1,
							"operation": cls.TEST_OPERATION,
							"source_warehouse": cls.source_warehouse,
						}
					],
				}
			)
			with patch("erpnext.manufacturing.doctype.bom.bom.BOM.check_recursion"):
				bom.insert(ignore_permissions=True)
				bom.submit()
			cls.master_bom = bom.name
		frappe.db.set_value(
			"Item", cls.TEST_FINISHED_GOOD, "default_bom", cls.master_bom
		)
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		# secondary_connection() intentionally leaves its connection active after
		# first initialization; every test fixture belongs on the primary transaction.
		frappe.local.db = self._primary_connection
		self.worker_user = self._make_worker()
		self.worker = frappe.db.get_value("Employee", {"user_id": self.worker_user}, "name")
		self.supervisor = self._make_supervisor()
		self.wage_manager = self._make_user("Production Wage Manager")
		self._grant_company(self.wage_manager, self.TEST_COMPANY)

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
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Production Worker",
				"send_welcome_email": 0,
				"roles": [{"role": "Production Worker"}],
			}
		).insert(ignore_permissions=True)
		self._make_employee(email)
		return email

	def _make_employee(self, email):
		return frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": email,
				"company": self.TEST_COMPANY,
				"user_id": email,
				"date_of_birth": "1990-05-08",
				"date_of_joining": "2013-01-01",
				"gender": "Female",
				"company_email": email,
				"prefered_contact_email": "Company Email",
				"status": "Active",
			}
		).insert(ignore_permissions=True)

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
		self._make_employee(email)
		return email

	def _make_job_card(self, qty=100):
		original_default_bom = frappe.db.get_value(
			"Item", self.TEST_FINISHED_GOOD, "default_bom"
		)
		planned_qty_before = flt(
			frappe.db.get_value(
				"Bin",
				{
					"item_code": self.TEST_FINISHED_GOOD,
					"warehouse": self.fg_warehouse,
				},
				"planned_qty",
			)
		)
		bom = frappe.copy_doc(frappe.get_doc("BOM", self.master_bom))
		bom.set_rate_of_sub_assembly_item_based_on_bom = 0
		bom.rm_cost_as_per = "Valuation Rate"
		bom.is_default = 0
		bom.items[0].uom = "Nos"
		bom.items[0].conversion_factor = 1
		bom.insert()
		# Capacity scheduling is unrelated to worker-reporting invariants and can
		# exhaust the shared test workstation after committed concurrency fixtures.
		with self.change_settings("Manufacturing Settings", {"disable_capacity_planning": 1}):
			work_order = frappe.new_doc("Work Order")
			work_order.production_item = self.TEST_FINISHED_GOOD
			work_order.bom_no = bom.name
			work_order.qty = qty
			work_order.company = self.TEST_COMPANY
			work_order.stock_uom = "Nos"
			work_order.source_warehouse = self.source_warehouse
			work_order.wip_warehouse = self.wip_warehouse
			work_order.fg_warehouse = self.fg_warehouse
			work_order.skip_transfer = 1
			work_order.planned_start_date = now_datetime()
			work_order.transfer_material_against = "Work Order"
			work_order.get_items_and_operations_from_bom()
			for row in work_order.required_items:
				row.source_warehouse = self.source_warehouse
			work_order.insert()
			work_order.submit()
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

	def _assign(self, job_card, employee=None):
		with self.set_user(self.supervisor):
			return service.assign_worker(job_card.name, employee or self.worker)

	def _setup_flow(self, qty=100, wage_type="Piecework", rate=5, valid_from=None):
		job_card = self._make_job_card(qty)
		self._make_rate(job_card, wage_type=wage_type, rate=rate, valid_from=valid_from)
		assignment = self._assign(job_card)
		return job_card, assignment

	def _submit_as(self, assignment, worker_user, qty, minutes=0, request_id=None):
		request_id = request_id or random_string(16)
		duration = flt(minutes) if flt(minutes) > 0 else 1
		started_at = now_datetime()
		latest = frappe.get_all(
			"Job Card Work Report",
			filters={"employee": assignment.employee, "actual_end_time": ["is", "set"]},
			fields=["actual_end_time"],
			order_by="actual_end_time desc",
			limit=1,
		)
		if latest and get_datetime(latest[0].actual_end_time) > started_at:
			started_at = get_datetime(latest[0].actual_end_time)
		with self.set_user(worker_user):
			report = service.start_work_session(
				assignment.name,
				f"{request_id}-start",
				started_at=started_at,
			)
			return service.finish_work_session(
				report.name,
				qty,
				f"{request_id}-finish",
				ended_at=started_at + timedelta(minutes=duration),
			)

	def _submit(self, assignment, qty, minutes=0, request_id=None):
		return self._submit_as(
			assignment,
			self.worker_user,
			qty,
			minutes=minutes,
			request_id=request_id,
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
				 %(request_key)s, %(assignment)s, %(job_card)s, %(company)s, %(operation)s, 'raw-operation-row',
				 %(employee)s, %(employee_user)s, current_date(), %(wage_type)s, 'Pending Approval',
				 %(qty)s, %(minutes)s, 1, 1)
			""",
			{
				"name": name,
				"request_key": f"raw-{name}",
				"assignment": assignment,
				"job_card": job_card,
				"company": self.TEST_COMPANY,
				"operation": self.TEST_OPERATION,
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
			frappe.db.get_value("Item", self.TEST_FINISHED_GOOD, "default_bom"),
			original_default_bom,
		)
		self.assertTrue(frappe.db.exists("BOM", original_default_bom))
		self.assertTrue(frappe.db.get_value("BOM", original_default_bom, "is_default"))
		self.assertEqual(
			flt(
				frappe.db.get_value(
					"Bin",
					{
						"item_code": self.TEST_FINISHED_GOOD,
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
		self.assertEqual(get_datetime(row.from_time), get_datetime(approved.actual_start_time))
		self.assertEqual(get_datetime(row.to_time), get_datetime(approved.actual_end_time))
		self.assertEqual(row.time_in_mins, approved.actual_minutes)

		self._approve(report)
		job_card.reload()
		self.assertEqual(job_card.total_completed_qty, 30)
		self.assertEqual(len(job_card.time_logs), 1)

	def test_fixture_uses_an_explicit_nondefault_bom(self):
		original_default = frappe.db.get_value(
			"Item", self.TEST_FINISHED_GOOD, "default_bom"
		)
		job_card = self._make_job_card(qty=10)
		test_bom = job_card.flags.worker_reporting_test_bom
		self.assertEqual(job_card.bom_no, test_bom)
		self.assertNotEqual(test_bom, original_default)
		self.assertFalse(frappe.db.get_value("BOM", test_bom, "is_default"))
		self.assertEqual(
			frappe.db.get_value("Item", self.TEST_FINISHED_GOOD, "default_bom"),
			original_default,
		)
		self.assertTrue(frappe.db.exists("BOM", original_default))
		self.assertTrue(frappe.db.get_value("BOM", original_default, "is_default"))

	def test_metadata_keeps_worker_writes_api_only_and_uses_unique_backlinks(self):
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
		self.assertFalse(frappe.get_meta("Job Card Worker Assignment").get_field("job_card").unique)
		self.assertTrue(report_meta.get_field("request_key").unique)
		self.assertTrue(report_meta.get_field("completion_request_key").unique)
		self.assertIn("In Progress", report_meta.get_field("status").options.splitlines())
		self.assertEqual(report_meta.get_field("actual_start_time").fieldtype, "Datetime")
		self.assertEqual(report_meta.get_field("actual_end_time").fieldtype, "Datetime")
		self.assertEqual(report_meta.get_field("actual_minutes").fieldtype, "Float")
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

	def test_multiple_workers_can_share_one_job_card_and_complete_it_together(self):
		job_card, first_assignment = self._setup_flow(qty=100)
		second_user = self._make_worker()
		second_employee = frappe.db.get_value("Employee", {"user_id": second_user}, "name")
		with self.set_user(self.supervisor):
			available_workers = {row[0] for row in service.search_workers(job_card.name)}
		self.assertNotIn(self.worker, available_workers)
		self.assertIn(second_employee, available_workers)
		second_assignment = self._assign(job_card, second_employee)
		self.assertEqual(
			frappe.db.count("Job Card Worker Assignment", {"job_card": job_card.name}),
			2,
		)
		with self.set_user(self.supervisor):
			available_workers = {row[0] for row in service.search_workers(job_card.name)}
		self.assertNotIn(second_employee, available_workers)
		first = self._submit_as(first_assignment, self.worker_user, 40)
		second = self._submit_as(second_assignment, second_user, 60)
		self._approve(first)
		job_card.reload()
		self.assertEqual(job_card.docstatus, 0)
		self.assertEqual(job_card.total_completed_qty, 40)
		self._approve(second)
		job_card.reload()
		first_assignment.reload()
		second_assignment.reload()
		self.assertEqual(job_card.docstatus, 1)
		self.assertEqual(job_card.total_completed_qty, 100)
		self.assertEqual({row.employee for row in job_card.time_logs}, {self.worker, second_employee})
		self.assertEqual(first_assignment.status, "Completed")
		self.assertEqual(second_assignment.status, "Completed")

	def test_worker_can_cancel_only_an_active_session(self):
		_, assignment = self._setup_flow(qty=10)
		with self.set_user(self.worker_user):
			active = service.start_work_session(assignment.name, "cancel-active-start")
			self.assertEqual(active.status, "In Progress")
			service.cancel_work_session(active.name)
		self.assertFalse(frappe.db.exists("Job Card Work Report", active.name))

		pending = self._submit(assignment, 1)
		with self.set_user(self.worker_user):
			with self.assertRaises(frappe.ValidationError):
				service.cancel_work_session(pending.name)

	def test_worker_can_cancel_active_session_after_work_order_is_stopped(self):
		job_card, assignment = self._setup_flow(qty=10)
		with self.set_user(self.worker_user):
			active = service.start_work_session(assignment.name, "stopped-order-cancel-start")
		frappe.db.set_value(
			"Work Order", job_card.work_order, "status", "Stopped", update_modified=False
		)
		with self.set_user(self.worker_user):
			service.cancel_work_session(active.name)
		self.assertFalse(frappe.db.exists("Job Card Work Report", active.name))

	def test_supervisor_can_cancel_orphaned_active_session_after_worker_is_disabled(self):
		job_card, assignment = self._setup_flow(qty=10)
		with self.set_user(self.worker_user):
			active = service.start_work_session(assignment.name, "disabled-worker-cancel-start")
		frappe.db.set_value("User", self.worker_user, "enabled", 0, update_modified=False)
		frappe.db.set_value(
			"Work Order", job_card.work_order, "status", "Stopped", update_modified=False
		)
		with self.set_user(self.supervisor):
			dashboard_assignment = next(
				row
				for row in service.get_review_dashboard()["assignments"]
				if row.name == assignment.name
			)
			self.assertEqual(dashboard_assignment.active_report, active.name)
			self.assertTrue(dashboard_assignment.can_cancel_session)
			service.cancel_work_session(active.name)
		self.assertFalse(frappe.db.exists("Job Card Work Report", active.name))

	def test_material_transfer_caps_worker_reporting_and_current_approval(self):
		job_card, assignment = self._setup_flow(qty=10)
		frappe.db.set_value(
			"Work Order",
			job_card.work_order,
			{
				"skip_transfer": 0,
				"material_transferred_for_manufacturing": 0,
				"status": "Not Started",
			},
			update_modified=False,
		)
		with self.set_user(self.worker_user):
			dashboard_assignment = next(
				row
				for row in service.get_worker_dashboard()["assignments"]
				if row.name == assignment.name
			)
			self.assertFalse(dashboard_assignment.can_start)
			self.assertEqual(dashboard_assignment.block_code, "MATERIAL_NOT_TRANSFERRED")
			with self.assertRaises(frappe.ValidationError):
				service.start_work_session(assignment.name, "unissued-material-start")

		frappe.db.set_value(
			"Work Order",
			job_card.work_order,
			{
				"material_transferred_for_manufacturing": 2,
				"status": "In Process",
			},
			update_modified=False,
		)
		with self.set_user(self.worker_user):
			active = service.start_work_session(assignment.name, "partial-material-start")
			with self.assertRaises(frappe.ValidationError):
				service.finish_work_session(
					active.name,
					3,
					"partial-material-too-much",
					ended_at=get_datetime(active.actual_start_time) + timedelta(minutes=1),
				)
			pending = service.finish_work_session(
				active.name,
				2,
				"partial-material-finish",
				ended_at=get_datetime(active.actual_start_time) + timedelta(minutes=1),
			)

		frappe.db.set_value(
			"Work Order",
			job_card.work_order,
			{
				"material_transferred_for_manufacturing": 0,
				"status": "Not Started",
			},
			update_modified=False,
		)
		with self.set_user(self.supervisor):
			dashboard_report = next(
				row
				for row in service.get_review_dashboard()["reports"]
				if row.name == pending.name
			)
			self.assertFalse(dashboard_report.can_approve)
			self.assertEqual(dashboard_report.approve_block_code, "MATERIAL_NOT_TRANSFERRED")
			self.assertTrue(dashboard_report.can_reject)
			with self.assertRaises(frappe.ValidationError):
				service.approve_work_report(pending.name)
			service.reject_work_report(pending.name, "发料已撤销，退回重新报工")

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

	def test_piecework_session_over_24_hours_stays_active_for_safe_cancellation(self):
		_, assignment = self._setup_flow(qty=10, wage_type="Piecework")
		started_at = now_datetime()
		with self.set_user(self.worker_user):
			active = service.start_work_session(
				assignment.name,
				"stale-piecework-start",
				started_at=started_at,
			)
			with self.assertRaises(frappe.ValidationError):
				service.finish_work_session(
					active.name,
					1,
					"stale-piecework-finish",
					ended_at=started_at + timedelta(minutes=1441),
				)
			active.reload()
			self.assertEqual(active.status, "In Progress")
			self.assertFalse(active.completion_request_key)
			service.cancel_work_session(active.name)
		self.assertFalse(frappe.db.exists("Job Card Work Report", active.name))

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

	def test_approve_and_reject_decisions_are_mutually_exclusive(self):
		approved_job_card, approved_assignment = self._setup_flow(qty=10)
		approved_report = self._submit(approved_assignment, 3)
		self._approve(approved_report)
		with self.set_user(self.supervisor):
			with self.assertRaises(frappe.ValidationError):
				service.reject_work_report(approved_report.name, "迟到的驳回请求")
		approved_report.reload()
		approved_job_card.reload()
		self.assertEqual(approved_report.status, "Approved")
		self.assertEqual(approved_job_card.total_completed_qty, 3)
		self.assertEqual(
			len(
				[
					row
					for row in approved_job_card.time_logs
					if row.custom_job_card_work_report == approved_report.name
				]
			),
			1,
		)

		rejected_job_card = self._make_job_card(qty=10)
		rejected_assignment = self._assign(rejected_job_card)
		rejected_report = self._submit(rejected_assignment, 3)
		with self.set_user(self.supervisor):
			service.reject_work_report(rejected_report.name, "先到的驳回请求")
			with self.assertRaises(frappe.ValidationError):
				service.approve_work_report(rejected_report.name)
		rejected_report.reload()
		rejected_job_card.reload()
		self.assertEqual(rejected_report.status, "Rejected")
		self.assertEqual(rejected_job_card.total_completed_qty, 0)
		self.assertEqual(len(rejected_job_card.time_logs), 0)

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

	def test_full_approval_preserves_reviewer_web_session_identity(self):
		job_card, assignment = self._setup_flow(qty=10)
		report = self._submit(assignment, 10)
		with self.set_user(self.supervisor):
			frappe.session.sid = "worker-reporting-web-session"
			approved = service.approve_work_report(report.name)
			self.assertEqual(frappe.session.user, self.supervisor)
			self.assertEqual(frappe.session.sid, "worker-reporting-web-session")

		job_card.reload()
		self.assertEqual(approved.status, "Approved")
		self.assertEqual(job_card.docstatus, 1)

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
		new_report = self._submit_as(
			first_assignment,
			new_user,
			7,
			request_id="rebound-user-report",
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
			self.assertEqual(
				summary.get_wage_management_context()["companies"], [self.TEST_COMPANY]
			)
			self.assertIn(rate.name, frappe.get_list("Operation Wage Rate", pluck="name", limit=0))
			self.assertTrue(frappe.has_permission("Operation Wage Rate", "read", doc=rate))
			with self.assertRaises(frappe.PermissionError):
				service.search_wage_employees(self.OTHER_COMPANY)
			with self.assertRaises(frappe.PermissionError):
				summary.build_monthly_summaries(self.OTHER_COMPANY, nowdate())

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
		production_moment = datetime.combine(
			get_datetime(nowdate()).date(), time(0, 0, 0, 123456)
		)
		with self.freeze_time(production_moment):
			job_card, assignment = self._setup_flow(qty=100, wage_type="Time", rate=20)
			report = self._submit(assignment, 30, minutes=720)
			self.assertEqual(report.actual_minutes, 720)
			self.assertEqual(report.reported_minutes, 720)
			self.assertEqual(report.wage_amount, 240)
			self._approve(report)
			with self.assertRaises(frappe.ValidationError):
				self._submit(assignment, 1, minutes=721)
		with self.set_user(self.wage_manager):
			rate = frappe.get_doc("Operation Wage Rate", report.wage_rate)
			rate.rate = 100
			rate.save()
		report.reload()
		self.assertEqual(report.rate, 20)
		self.assertEqual(report.wage_amount, 240)
		job_card.reload()
		self.assertEqual(job_card.total_time_in_mins, 720)

	def test_cross_midnight_time_session_belongs_to_its_starting_production_day(self):
		production_moment = datetime.combine(
			get_datetime(nowdate()).date(), time(23, 30, 0, 123456)
		)
		with self.freeze_time(production_moment):
			job_card, assignment = self._setup_flow(qty=10, wage_type="Time", rate=20)
			with self.set_user(self.worker_user):
				report = service.start_work_session(
					assignment.name,
					"cross-midnight-start",
					started_at=production_moment,
				)
				report = service.finish_work_session(
					report.name,
					1,
					"cross-midnight-finish",
					ended_at=production_moment + timedelta(minutes=120),
				)

		self.assertEqual(getdate(report.labor_date), production_moment.date())
		self.assertEqual(report.actual_minutes, 120)
		self.assertEqual(report.reported_minutes, 120)
		self.assertEqual(report.wage_amount, 40)
		self.assertEqual(
			getdate(report.actual_end_time),
			(production_moment + timedelta(days=1)).date(),
		)

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
			result = summary.build_monthly_summaries(
				self.TEST_COMPANY, previous_month, self.worker
			)
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
			result = summary.build_monthly_summaries(
				self.TEST_COMPANY, previous_month, self.worker
			)
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
			result = summary.build_monthly_summaries(
				self.TEST_COMPANY, nowdate(), self.worker
			)
			with self.assertRaises(frappe.ValidationError):
				summary.confirm_monthly_summary(result["summaries"][0])
