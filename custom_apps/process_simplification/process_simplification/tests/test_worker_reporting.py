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
from process_simplification.production_exceptions import service as exception_service
from process_simplification.production_exceptions.constants import (
	APPLIED,
	APPROVED,
	AWAITING_STOCK_ENTRY,
	COMPLETED,
	MATERIAL_RETURN,
	MATERIAL_SCRAP,
	PENDING_APPROVAL,
	PROCESS_LOSS,
	REJECTED,
)
from process_simplification.production_reporting.setup import (
	backfill_operation_wage_rate_modes,
	backfill_work_report_wage_option_snapshots,
	ensure_worker_reporting_reference_fields,
	ensure_worker_reporting_reference_permissions,
)
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
		cls.scrap_warehouse = cls._ensure_warehouse("Worker Reporting Scrap")
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

	def _make_rate(
		self,
		job_card,
		wage_type="Piecework",
		rate=5,
		valid_from=None,
		hourly_rate=None,
	):
		if wage_type not in {"Piecework", "Time", "Both"}:
			raise ValueError(f"Unsupported test wage type: {wage_type}")
		values = {
			"doctype": "Operation Wage Rate",
			"company": job_card.company,
			"operation": job_card.operation,
			"enable_piecework": int(wage_type in {"Piecework", "Both"}),
			"piecework_rate": rate if wage_type in {"Piecework", "Both"} else 0,
			"enable_time": int(wage_type in {"Time", "Both"}),
			"hourly_rate": (
				(hourly_rate if hourly_rate is not None else rate)
				if wage_type in {"Time", "Both"}
				else 0
			),
			"valid_from": valid_from or nowdate(),
			"enabled": 1,
		}
		with self.set_user(self.wage_manager):
			return frappe.get_doc(values).insert()

	def _assign(self, job_card, employee=None):
		with self.set_user(self.supervisor):
			return service.assign_worker(job_card.name, employee or self.worker)

	def _setup_flow(
		self,
		qty=100,
		wage_type="Piecework",
		rate=5,
		valid_from=None,
		hourly_rate=None,
	):
		job_card = self._make_job_card(qty)
		self._make_rate(
			job_card,
			wage_type=wage_type,
			rate=rate,
			valid_from=valid_from,
			hourly_rate=hourly_rate,
		)
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
				reported_minutes=duration,
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
		self.assertEqual(
			approved.employee_name,
			frappe.db.get_value("Employee", self.worker, "employee_name"),
		)
		self.assertEqual(job_card.total_completed_qty, 30)
		self.assertEqual(job_card.pending_qty, 70)
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

	def test_process_loss_requires_worker_request_and_supervisor_approval(self):
		job_card, assignment = self._setup_flow(qty=10, wage_type="Piecework", rate=5)
		report = self._submit(assignment, 6, request_id="loss-approved-output")
		self._approve(report)

		with self.set_user(self.worker_user):
			request = exception_service.submit_exception(
				assignment=assignment.name,
				request_type=PROCESS_LOSS,
				qty=4,
				cause="Operation Error",
				reason="操作失误造成四件无法继续加工",
				request_key="loss-request-" + random_string(12),
			)
		self.assertEqual(request.status, PENDING_APPROVAL)
		job_card.reload()
		self.assertEqual(job_card.total_completed_qty, 6)
		self.assertEqual(job_card.process_loss_qty, 0)
		self.assertEqual(job_card.pending_qty, 4)

		with self.set_user(self.supervisor):
			exception_service.approve_exception(request.name)
		request.reload()
		job_card.reload()
		report.reload()
		self.assertEqual(request.status, APPLIED)
		self.assertEqual(job_card.docstatus, 1)
		self.assertEqual(job_card.total_completed_qty, 6)
		self.assertEqual(job_card.process_loss_qty, 4)
		self.assertEqual(job_card.pending_qty, 0)
		self.assertEqual(report.completed_qty, 6)
		self.assertEqual(report.wage_amount, 30)

	def test_rejected_process_loss_does_not_change_job_card(self):
		job_card, assignment = self._setup_flow(qty=10)
		with self.set_user(self.worker_user):
			request = exception_service.submit_exception(
				assignment=assignment.name,
				request_type=PROCESS_LOSS,
				qty=3,
				cause="Other",
				reason="申请主管复核",
				request_key="loss-reject-" + random_string(12),
			)
		with self.set_user(self.supervisor):
			exception_service.reject_exception(request.name, "不属于工序损耗，请继续加工")
		request.reload()
		job_card.reload()
		self.assertEqual(request.status, REJECTED)
		self.assertEqual(job_card.process_loss_qty, 0)
		self.assertEqual(job_card.total_completed_qty, 0)
		self.assertEqual(job_card.pending_qty, 10)

	def test_material_return_and_scrap_create_native_drafts_then_post_stock(self):
		from erpnext.manufacturing.doctype.work_order.work_order import (
			make_stock_entry as make_work_order_stock_entry,
		)
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
		from erpnext.stock.doctype.stock_entry.stock_entry import get_available_materials
		from erpnext.stock.utils import get_stock_balance

		job_card, assignment = self._setup_flow(qty=10)
		source_before = get_stock_balance(self.TEST_RAW_MATERIAL, self.source_warehouse)
		wip_before = get_stock_balance(self.TEST_RAW_MATERIAL, self.wip_warehouse)
		scrap_before = get_stock_balance(self.TEST_RAW_MATERIAL, self.scrap_warehouse)
		with self.set_user("Administrator"):
			make_stock_entry(
				item_code=self.TEST_RAW_MATERIAL,
				to_warehouse=self.source_warehouse,
				company=self.TEST_COMPANY,
				qty=10,
				basic_rate=1,
			)
			frappe.db.set_value(
				"Work Order",
				job_card.work_order,
				{
					"skip_transfer": 0,
					"source_warehouse": self.source_warehouse,
					"wip_warehouse": self.wip_warehouse,
					"scrap_warehouse": self.scrap_warehouse,
				},
				update_modified=False,
			)
			transfer = frappe.get_doc(
				make_work_order_stock_entry(
					job_card.work_order,
					"Material Transfer for Manufacture",
					qty=5,
				)
			)
			transfer.from_warehouse = self.source_warehouse
			transfer.to_warehouse = self.wip_warehouse
			for item in transfer.items:
				item.s_warehouse = self.source_warehouse
				item.t_warehouse = self.wip_warehouse
			transfer.insert(ignore_permissions=True)
			transfer.submit()

		with self.set_user(self.worker_user):
			native_materials = get_available_materials(job_card.work_order)
			self.assertTrue(
				native_materials,
				msg=f"native available materials missing after transfer: {transfer.as_dict()}",
			)
			options = exception_service.get_exception_options(assignment.name)
			self.assertEqual(len(options["materials"]), 1)
			material = options["materials"][0]
			self.assertEqual(material.item_code, self.TEST_RAW_MATERIAL)
			self.assertEqual(
				material.item_name,
				frappe.db.get_value("Item", self.TEST_RAW_MATERIAL, "item_name"),
			)
			self.assertEqual(material.requestable_qty, 5)
			return_request = exception_service.submit_exception(
				assignment=assignment.name,
				request_type=MATERIAL_RETURN,
				qty=2,
				cause="Material Defect",
				reason="领料后发现其中两件尚未使用，需要退回原仓",
				request_key="material-return-" + random_string(12),
				material_key=material.key,
			)

		with self.set_user(self.supervisor):
			exception_service.approve_exception(return_request.name)
		return_request.reload()
		self.assertEqual(return_request.status, AWAITING_STOCK_ENTRY)
		return_entry = frappe.get_doc("Stock Entry", return_request.stock_entry)
		self.assertEqual(return_entry.docstatus, 0)
		self.assertTrue(return_entry.is_return)
		self.assertEqual(return_entry.purpose, "Material Transfer for Manufacture")
		self.assertEqual(return_entry.items[0].s_warehouse, self.wip_warehouse)
		self.assertEqual(return_entry.items[0].t_warehouse, self.source_warehouse)
		with self.set_user("Administrator"):
			with self.assertRaises(frappe.ValidationError):
				return_entry.delete()
		return_entry.reload()
		return_entry.custom_production_exception_request = None
		return_entry.items[0].qty = 3
		with self.set_user("Administrator"):
			with self.assertRaises(frappe.ValidationError):
				return_entry.submit()
		return_entry.reload()

		with self.set_user("Administrator"):
			return_entry.submit()
		return_request.reload()
		self.assertEqual(return_request.status, COMPLETED)
		with self.set_user("Administrator"):
			return_entry.cancel()
		return_request.reload()
		self.assertEqual(return_request.status, APPROVED)
		self.assertFalse(return_request.stock_entry)
		with self.set_user(self.supervisor):
			exception_service.approve_exception(return_request.name)
		return_request.reload()
		self.assertEqual(return_request.status, AWAITING_STOCK_ENTRY)
		self.assertNotEqual(return_request.stock_entry, return_entry.name)
		return_entry = frappe.get_doc("Stock Entry", return_request.stock_entry)
		with self.set_user("Administrator"):
			return_entry.submit()
		return_request.reload()
		self.assertEqual(return_request.status, COMPLETED)

		with self.set_user(self.worker_user):
			options = exception_service.get_exception_options(assignment.name)
			material = options["materials"][0]
			self.assertEqual(material.requestable_qty, 3)
			scrap_request = exception_service.submit_exception(
				assignment=assignment.name,
				request_type=MATERIAL_SCRAP,
				qty=1,
				cause="Material Defect",
				reason="该物料已经损坏，申请转入报废仓",
				request_key="material-scrap-" + random_string(12),
				material_key=material.key,
			)
		with self.set_user(self.supervisor):
			exception_service.approve_exception(scrap_request.name)
		scrap_request.reload()
		scrap_entry = frappe.get_doc("Stock Entry", scrap_request.stock_entry)
		self.assertEqual(scrap_entry.items[0].t_warehouse, self.scrap_warehouse)
		with self.set_user("Administrator"):
			scrap_entry.submit()

		scrap_request.reload()
		self.assertEqual(scrap_request.status, COMPLETED)
		self.assertEqual(
			get_stock_balance(self.TEST_RAW_MATERIAL, self.source_warehouse) - source_before,
			7,
		)
		self.assertEqual(
			get_stock_balance(self.TEST_RAW_MATERIAL, self.wip_warehouse) - wip_before,
			2,
		)
		self.assertEqual(
			get_stock_balance(self.TEST_RAW_MATERIAL, self.scrap_warehouse) - scrap_before,
			1,
		)
		job_card.reload()
		self.assertEqual(service.material_reportable_qty(job_card), 2)

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

	def test_metadata_keeps_worker_writes_api_only_and_auditable_backlinks(self):
		from process_simplification import hooks

		report_meta = frappe.get_meta("Job Card Work Report")
		self.assertEqual(report_meta.title_field, "employee_name")
		self.assertTrue(report_meta.get_field("operation_id").hidden)
		self.assertTrue(report_meta.get_field("employee").in_list_view)
		self.assertTrue(report_meta.get_field("labor_date").in_list_view)
		self.assertTrue(
			frappe.get_meta("Monthly Worker Wage Summary Detail")
			.get_field("operation_id")
			.hidden
		)
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
		self.assertTrue(
			frappe.get_meta("Job Card Time Log").get_field("employee").ignore_user_permissions
		)
		self.assertTrue(
			frappe.get_meta("Job Card Time Log")
			.get_field("custom_reported_employee")
			.ignore_user_permissions
		)
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
		self.assertEqual(report_meta.get_field("time_segments").options, "Job Card Work Report Time Segment")
		self.assertEqual(report_meta.get_field("manual_time_entry").fieldtype, "Check")
		self.assertEqual(report_meta.get_field("piecework_rate_snapshot").fieldtype, "Currency")
		self.assertEqual(report_meta.get_field("hourly_rate_snapshot").fieldtype, "Currency")
		self.assertEqual(report_meta.get_field("time_manual_entry_snapshot").fieldtype, "Check")
		self.assertEqual(
			frappe.get_meta("Process Simplification Settings")
			.get_field("allow_manual_time_entry")
			.default,
			"1",
		)
		self.assertTrue(
			frappe.get_meta("Job Card Time Log").get_field("custom_job_card_work_report_segment")
		)
		rate_meta = frappe.get_meta("Operation Wage Rate")
		self.assertEqual(rate_meta.get_field("enable_piecework").default, "1")
		self.assertEqual(rate_meta.get_field("piecework_rate").fieldtype, "Currency")
		self.assertEqual(rate_meta.get_field("enable_time").default, "0")
		self.assertEqual(rate_meta.get_field("hourly_rate").fieldtype, "Currency")
		self.assertTrue(rate_meta.get_field("wage_type").hidden)
		self.assertTrue(rate_meta.get_field("rate").hidden)
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
			{
				"jcwr_job_status_name",
				"jcwr_employee_day_status",
				"jcwr_assignment_status",
				"jcwr_review_status_time",
				"jcwr_assignment_review_time",
			}.issubset(
				report_indexes
			)
		)
		assignment_indexes = {
			row.Key_name
			for row in frappe.db.sql("show index from `tabJob Card Worker Assignment`", as_dict=True)
		}
		self.assertIn("jcwa_work_order_status", assignment_indexes)
		exception_meta = frappe.get_meta("Production Exception Request")
		self.assertFalse(
			any(row.role == "Production Worker" for row in exception_meta.permissions)
		)
		self.assertTrue(exception_meta.get_field("request_key").unique)
		stock_exception_link = frappe.get_meta("Stock Entry").get_field(
			"custom_production_exception_request"
		)
		self.assertFalse(stock_exception_link.unique)
		self.assertTrue(stock_exception_link.search_index)
		exception_indexes = {
			row.Key_name
			for row in frappe.db.sql(
				"show index from `tabProduction Exception Request`", as_dict=True
			)
		}
		self.assertTrue(
			{"per_worker_time_status", "per_review_queue", "per_material_reservation"}.issubset(
				exception_indexes
			)
		)

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

	def test_review_dashboard_and_history_use_names_and_server_pagination(self):
		approved_job_card, approved_assignment = self._setup_flow(qty=10)
		approved_report = self._submit(approved_assignment, 2)
		self._approve(approved_report)

		rejected_job_card = self._make_job_card(qty=10)
		rejected_assignment = self._assign(rejected_job_card)
		rejected_report = self._submit(rejected_assignment, 1)
		with self.set_user(self.supervisor):
			service.reject_work_report(rejected_report.name, "历史审核分页验证")
			dashboard = service.get_review_dashboard(page_length=1)
			history_first = service.get_review_history(
				page=1,
				page_length=1,
				employee=self.worker,
			)
			history_second = service.get_review_history(
				page=2,
				page_length=1,
				employee=self.worker,
			)

		self.assertLessEqual(len(dashboard["assignments"]), 1)
		self.assertGreaterEqual(dashboard["pagination"]["assignments"]["total_count"], 2)
		self.assertEqual(dashboard["pagination"]["assignments"]["page_length"], 1)
		self.assertEqual(history_first["pagination"]["page_length"], 1)
		self.assertGreaterEqual(history_first["pagination"]["total_count"], 2)
		self.assertTrue(history_first["pagination"]["has_next"])
		self.assertEqual(len(history_first["rows"]), 1)
		self.assertEqual(len(history_second["rows"]), 1)
		self.assertNotEqual(history_first["rows"][0].name, history_second["rows"][0].name)
		self.assertTrue(history_first["rows"][0].employee_name)
		self.assertEqual(
			{history_first["rows"][0].work_order, history_second["rows"][0].work_order},
			{approved_job_card.work_order, rejected_job_card.work_order},
		)
		with self.set_user(self.supervisor):
			with self.assertRaises(frappe.ValidationError):
				service.get_review_history(status="Pending Approval")

	def test_worker_history_is_self_scoped_filterable_and_includes_review_audit(self):
		_approved_job_card, approved_assignment = self._setup_flow(qty=10)
		approved_report = self._submit(approved_assignment, 2)
		self._approve(approved_report)

		rejected_job_card = self._make_job_card(qty=10)
		rejected_assignment = self._assign(rejected_job_card)
		rejected_report = self._submit(rejected_assignment, 1)
		with self.set_user(self.supervisor):
			service.reject_work_report(rejected_report.name, "员工历史查询验证")

		with self.set_user(self.worker_user):
			first_page = service.get_worker_report_history(page=1, page_length=1)
			second_page = service.get_worker_report_history(page=2, page_length=1)
			approved_only = service.get_worker_report_history(status="Approved")
			operation_only = service.get_worker_report_history(operation=self.TEST_OPERATION)

		self.assertEqual(first_page["pagination"]["page_length"], 1)
		self.assertTrue(first_page["pagination"]["has_next"])
		self.assertEqual(len(first_page["rows"]), 1)
		self.assertEqual(len(second_page["rows"]), 1)
		self.assertEqual(
			{first_page["rows"][0].name, second_page["rows"][0].name},
			{approved_report.name, rejected_report.name},
		)
		self.assertEqual({row.status for row in approved_only["rows"]}, {"Approved"})
		self.assertTrue(operation_only["rows"])
		for row in (*first_page["rows"], *second_page["rows"]):
			self.assertEqual(row.employee, self.worker)
			self.assertEqual(row.employee_user, self.worker_user)
			self.assertTrue(row.reviewed_by)
			self.assertTrue(row.reviewed_at)

		with self.set_user(self.worker_user):
			with self.assertRaises(frappe.ValidationError):
				service.get_worker_report_history(status="In Progress")
			with self.assertRaises(frappe.ValidationError):
				service.get_worker_report_history(
					from_date=add_days(nowdate(), 1),
					to_date=nowdate(),
				)
		with self.set_user(self.supervisor):
			with self.assertRaises(frappe.PermissionError):
				service.get_worker_report_history()

	def test_worker_assignment_priority_keeps_material_waits_last(self):
		rows = [
			frappe._dict(name="waiting", block_code="MATERIAL_NOT_TRANSFERRED"),
			frappe._dict(name="ready", can_start=True),
			frappe._dict(name="active", active_report="JCWR-1"),
			frappe._dict(name="pending", block_code="PENDING_REPORT"),
		]
		rows.sort(key=service._worker_assignment_priority)
		self.assertEqual([row.name for row in rows], ["active", "ready", "pending", "waiting"])

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
		job_card, assignment = self._setup_flow(qty=100)
		unassigned_job_card = self._make_job_card(qty=1)
		report = self._submit(assignment, 10)
		with self.set_user("Administrator"):
			ensure_worker_reporting_reference_permissions()
			ensure_worker_reporting_reference_fields()
		with self.set_user(self.supervisor):
			self.assertTrue(
				frappe.has_permission("Job Card Worker Assignment", "read", doc=assignment)
			)
			self.assertTrue(frappe.has_permission("Job Card Work Report", "read", doc=report))
			self.assertFalse(frappe.has_permission("Job Card Work Report", "write", doc=report))
			self.assertFalse(frappe.has_permission("Job Card Work Report", "delete", doc=report))
			self.assertTrue(frappe.has_permission("Job Card", "read", doc=job_card))
			self.assertTrue(
				frappe.has_permission("Work Order", "read", doc=frappe.get_doc("Work Order", job_card.work_order))
			)
			self.assertFalse(frappe.has_permission("Job Card", "write", doc=job_card))
			self.assertFalse(frappe.has_permission("Job Card", "read", doc=unassigned_job_card))

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

	def test_dual_wage_rule_defaults_to_piecework_and_worker_can_choose_time(self):
		started_at = datetime.combine(get_datetime(nowdate()).date(), time(8, 0))
		job_card, assignment = self._setup_flow(
			qty=10,
			wage_type="Both",
			rate=5,
			hourly_rate=30,
		)

		with self.set_user(self.worker_user):
			dashboard = service.get_worker_dashboard()
			row = next(item for item in dashboard["assignments"] if item.name == assignment.name)
			self.assertEqual(
				row.wage_options,
				[
					{"wage_type": "Piecework", "rate": 5.0},
					{"wage_type": "Time", "rate": 30.0},
				],
			)
			self.assertFalse(row.can_choose_wage_type)
			self.assertEqual(row.wage_type, "Piecework")
			self.assertEqual(row.rate, 5)

			time_report = service.start_work_session(
				assignment.name,
				"dual-select-at-finish",
				started_at=started_at,
			)
			self.assertEqual(time_report.wage_type, "Piecework")
			self.assertEqual(time_report.rate, 5)
			self.assertEqual(time_report.piecework_rate_snapshot, 5)
			self.assertEqual(time_report.hourly_rate_snapshot, 30)
			self.assertTrue(time_report.time_manual_entry_snapshot)

			active_dashboard = service.get_worker_dashboard()
			active_row = next(
				item for item in active_dashboard["assignments"] if item.name == assignment.name
			)
			self.assertTrue(active_row.can_choose_wage_type)
			self.assertEqual(active_row.wage_options, row.wage_options)
			self.assertTrue(active_row.time_manual_entry_available)

		with self.set_user(self.wage_manager):
			rate = frappe.get_doc(
				"Operation Wage Rate",
				frappe.db.get_value(
					"Operation Wage Rate", {"operation": job_card.operation}, "name"
				),
			)
			rate.piecework_rate = 9
			rate.hourly_rate = 60
			rate.save()

		with self.set_user(self.worker_user):
			active_dashboard = service.get_worker_dashboard()
			active_row = next(
				item for item in active_dashboard["assignments"] if item.name == assignment.name
			)
			self.assertEqual(
				active_row.wage_options,
				[
					{"wage_type": "Piecework", "rate": 5.0},
					{"wage_type": "Time", "rate": 30.0},
				],
			)
			pending = service.finish_work_session(
				time_report.name,
				2,
				"dual-select-at-finish-submit",
				reported_minutes=30,
				wage_type="Time",
				ended_at=started_at + timedelta(minutes=45),
			)

		self.assertEqual(pending.wage_type, "Time")
		self.assertEqual(pending.rate, 30)
		self.assertEqual(pending.reported_minutes, 30)
		self.assertEqual(pending.wage_amount, 15)
		self.assertEqual(
			pending.wage_rate,
			frappe.db.get_value(
				"Operation Wage Rate", {"operation": job_card.operation}, "name"
			),
		)

		default_job_card = self._make_job_card(10)
		default_assignment = self._assign(default_job_card)
		with self.set_user(self.worker_user):
			default_report = service.start_work_session(
				default_assignment.name,
				"dual-default-at-finish",
				started_at=started_at + timedelta(minutes=60),
			)
			default_pending = service.finish_work_session(
				default_report.name,
				2,
				"dual-default-at-finish-submit",
				ended_at=started_at + timedelta(minutes=90),
			)
		self.assertEqual(default_pending.wage_type, "Piecework")
		self.assertEqual(default_pending.rate, 9)
		self.assertEqual(default_pending.wage_amount, 18)

	def test_worker_cannot_submit_a_wage_type_not_frozen_for_the_work_session(self):
		started_at = datetime.combine(get_datetime(nowdate()).date(), time(8, 0))
		_, assignment = self._setup_flow(qty=10, wage_type="Piecework", rate=5)
		with self.set_user(self.worker_user):
			report = service.start_work_session(
				assignment.name,
				"piecework-only-start",
				started_at=started_at,
			)
			with self.assertRaises(frappe.ValidationError):
				service.finish_work_session(
					report.name,
					1,
					"piecework-cannot-submit-time",
					reported_minutes=10,
					wage_type="Time",
					ended_at=started_at + timedelta(minutes=10),
				)
		report.reload()
		self.assertEqual(report.status, "In Progress")
		self.assertFalse(report.completion_request_key)

	def test_active_legacy_dual_session_recovers_choices_only_at_the_same_rule_revision(self):
		_, assignment = self._setup_flow(
			qty=10,
			wage_type="Both",
			rate=5,
			hourly_rate=30,
		)
		with self.set_user(self.worker_user):
			report = service.start_work_session(assignment.name, "legacy-dual-active")

		frappe.db.set_value(
			"Job Card Work Report",
			report.name,
			{
				"piecework_rate_snapshot": 5,
				"hourly_rate_snapshot": 0,
				"time_manual_entry_snapshot": 0,
			},
			update_modified=False,
		)
		backfill_work_report_wage_option_snapshots()
		report.reload()
		self.assertEqual(report.piecework_rate_snapshot, 5)
		self.assertEqual(report.hourly_rate_snapshot, 30)
		self.assertTrue(report.time_manual_entry_snapshot)

		with self.set_user(self.wage_manager):
			rate = frappe.get_doc("Operation Wage Rate", report.wage_rate)
			rate.hourly_rate = 60
			rate.save()
		frappe.db.set_value(
			"Job Card Work Report",
			report.name,
			{"hourly_rate_snapshot": 0, "time_manual_entry_snapshot": 0},
			update_modified=False,
		)
		backfill_work_report_wage_option_snapshots()
		report.reload()
		self.assertEqual(report.hourly_rate_snapshot, 0)
		self.assertFalse(report.time_manual_entry_snapshot)

	def test_legacy_single_method_wage_rule_is_backfilled_without_changing_its_rate(self):
		job_card = self._make_job_card(10)
		rate = self._make_rate(job_card, wage_type="Time", rate=42)
		frappe.db.set_value(
			"Operation Wage Rate",
			rate.name,
			{
				"enable_piecework": 0,
				"piecework_rate": 0,
				"enable_time": 0,
				"hourly_rate": 0,
				"wage_type": "Time",
				"rate": 42,
			},
			update_modified=False,
		)

		backfill_operation_wage_rate_modes()
		rate.reload()
		self.assertFalse(rate.enable_piecework)
		self.assertEqual(rate.piecework_rate, 0)
		self.assertTrue(rate.enable_time)
		self.assertEqual(rate.hourly_rate, 42)

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

	def test_work_order_assignment_context_scopes_job_cards_and_explains_prerequisites(self):
		job_card = self._make_job_card(10)
		with self.set_user(self.supervisor):
			context = service.get_work_order_assignment_context(job_card.work_order)
		row = next(row for row in context["job_cards"] if row["name"] == job_card.name)
		self.assertFalse(row["can_assign"])
		self.assertEqual(row["block_code"], "RATE_MISSING")
		self.assertEqual(row["material_status"], "READY_TO_REPORT")

		self._make_rate(job_card)
		with self.set_user(self.supervisor):
			context = service.get_work_order_assignment_context(job_card.work_order)
			scoped_results = service.search_draft_job_cards(work_order=job_card.work_order)
			supervisor_reviewers = service.search_assignment_supervisors(job_card.work_order)
		row = next(row for row in context["job_cards"] if row["name"] == job_card.name)
		self.assertTrue(context["can_assign"])
		self.assertTrue(row["can_assign"])
		self.assertEqual(context["assignment_supervisor"], self.supervisor)
		self.assertEqual(row["assignment_supervisor"], self.supervisor)
		self.assertFalse(row["can_choose_supervisor"])
		self.assertEqual({result[0] for result in scoped_results}, {job_card.name})
		self.assertEqual([result[0] for result in supervisor_reviewers], [self.supervisor])
		self.assertTrue(supervisor_reviewers[0][1])

		with self.set_user("Administrator"):
			admin_context = service.get_work_order_assignment_context(job_card.work_order)
			admin_row = next(
				row for row in admin_context["job_cards"] if row["name"] == job_card.name
			)
			reviewers = {
				result[0]
				for result in service.search_assignment_supervisors(job_card.work_order)
			}
		self.assertTrue(admin_context["can_choose_supervisor"])
		self.assertTrue(admin_row["can_choose_supervisor"])
		self.assertIn(self.supervisor, reviewers)
		self.assertIn("Administrator", reviewers)

		assignment = self._assign(job_card)
		with self.set_user(self.supervisor):
			context = service.get_work_order_assignment_context(job_card.work_order)
		row = next(row for row in context["job_cards"] if row["name"] == job_card.name)
		self.assertEqual(
			[(item["name"], item["employee"]) for item in row["assignments"]],
			[(assignment.name, self.worker)],
		)

	def test_completed_work_order_assignment_context_retains_read_only_history(self):
		job_card, assignment = self._setup_flow(qty=1)
		report = self._submit(assignment, 1)
		self._approve(report)
		job_card.reload()
		assignment.reload()
		self.assertEqual(job_card.docstatus, 1)
		self.assertEqual(assignment.status, "Completed")

		with self.set_user(self.supervisor):
			context = service.get_work_order_assignment_context(job_card.work_order)
		row = next(row for row in context["job_cards"] if row["name"] == job_card.name)
		self.assertFalse(context["can_assign"])
		self.assertFalse(row["can_assign"])
		self.assertEqual(row["block_code"], "JOB_CARD_NOT_DRAFT")
		self.assertEqual(row["display_supervisor"], self.supervisor)
		self.assertEqual(
			[
				(
					item["name"],
					item["employee"],
					item["assignment_status"],
					item["report_status"],
				)
				for item in row["assignments"]
			],
			[(assignment.name, self.worker, "Completed", "Approved")],
		)

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

	def test_time_wage_defaults_to_manual_minutes_and_pause_resume_excludes_breaks(self):
		started_at = datetime.combine(
			get_datetime(nowdate()).date(), time(9, 0, 0, 123456)
		)
		with self.change_settings(
			"Process Simplification Settings", {"allow_manual_time_entry": 1}
		):
			with self.freeze_time(started_at):
				job_card, assignment = self._setup_flow(qty=10, wage_type="Time", rate=30)
				with self.set_user(self.worker_user):
					report = service.start_work_session(
						assignment.name,
						"pause-resume-start",
						started_at=started_at,
					)
					paused = service.pause_work_session(
						report.name,
						"pause-resume-pause",
						paused_at=started_at + timedelta(minutes=30),
					)
					self.assertEqual(
						get_datetime(paused.timer_paused_at),
						started_at + timedelta(minutes=30),
					)
					self.assertEqual(
						service.pause_work_session(report.name, "pause-resume-pause").name,
						report.name,
					)
					service.resume_work_session(
						report.name,
						"pause-resume-resume",
						resumed_at=started_at + timedelta(minutes=90),
					)
					pending = service.finish_work_session(
						report.name,
						2,
						"pause-resume-finish",
						reported_minutes=45,
						ended_at=started_at + timedelta(minutes=120),
					)

		self.assertTrue(pending.manual_time_entry)
		self.assertEqual(pending.actual_minutes, 60)
		self.assertEqual(pending.reported_minutes, 45)
		self.assertEqual(pending.wage_amount, 22.5)
		self.assertEqual(len(pending.time_segments), 2)
		self._approve(pending)
		self._approve(pending)
		job_card.reload()
		rows = [
			row
			for row in job_card.time_logs
			if row.custom_job_card_work_report_segment == pending.name
		]
		self.assertEqual(len(rows), 2)
		self.assertEqual(sum(row.time_in_mins for row in rows), 60)
		self.assertEqual(sum(row.completed_qty for row in rows), 2)
		self.assertEqual(rows[-1].custom_job_card_work_report, pending.name)
		self.assertEqual(job_card.pending_qty, 8)

	def test_timer_mode_uses_effective_minutes_even_when_client_sends_manual_value(self):
		started_at = datetime.combine(
			get_datetime(nowdate()).date(), time(13, 0, 0, 123456)
		)
		with self.change_settings(
			"Process Simplification Settings", {"allow_manual_time_entry": 0}
		):
			with self.freeze_time(started_at):
				_, assignment = self._setup_flow(qty=10, wage_type="Time", rate=20)
				with self.set_user(self.worker_user):
					report = service.start_work_session(
						assignment.name,
						"timer-mode-start",
						started_at=started_at,
					)
					report = service.finish_work_session(
						report.name,
						1,
						"timer-mode-finish",
						reported_minutes=999,
						ended_at=started_at + timedelta(minutes=75),
					)

		self.assertFalse(report.manual_time_entry)
		self.assertEqual(report.actual_minutes, 75)
		self.assertEqual(report.reported_minutes, 75)
		self.assertEqual(report.wage_amount, 25)

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
					reported_minutes=120,
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
		self.assertEqual(
			doc.employee_name,
			frappe.db.get_value("Employee", self.worker, "employee_name"),
		)
		self.assertEqual(doc.wage_month, f"{previous_month.year}年{previous_month.month:02d}月")
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

	def test_cancelled_legacy_summary_does_not_block_regeneration(self):
		job_card, assignment = self._setup_flow(qty=1)
		report = self._submit(assignment, 1)
		self._approve(report)

		with self.set_user(self.wage_manager):
			first = summary.build_monthly_summaries(
				self.TEST_COMPANY, nowdate(), self.worker
			)["summaries"][0]
			frappe.db.set_value(
				"Monthly Worker Wage Summary",
				first,
				"docstatus",
				2,
				update_modified=False,
			)
			second = summary.build_monthly_summaries(
				self.TEST_COMPANY, nowdate(), self.worker
			)["summaries"][0]

		self.assertNotEqual(first, second)
		self.assertIsNone(
			frappe.db.get_value("Monthly Worker Wage Summary", first, "summary_key")
		)
		self.assertEqual(
			frappe.db.get_value("Monthly Worker Wage Summary", second, "docstatus"), 0
		)
