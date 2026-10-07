"""Exercise the operation pool against native documents on a disposable site.

Only fixture builders are reused from the worker-reporting suite. Its setUp and
its mocked material-readiness checks are deliberately not inherited. These tests
must not run concurrently with another suite using the same site's settings.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime, nowdate, random_string

from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry as make_order_stock_entry
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from process_simplification.production_reporting import dispatch_pool, service
from process_simplification.tests import test_worker_reporting as reporting_fixtures


class TestOperationDispatchPoolIntegration(IntegrationTestCase):
	TEST_COMPANY = "Operation Pool Test Company"
	TEST_COMPANY_ABBR = "OPT"
	OTHER_COMPANY = "Operation Pool Other Company"
	OTHER_COMPANY_ABBR = "OPO"
	TEST_OPERATION = "Operation Pool Test Operation"
	TEST_WORKSTATION = "Operation Pool Test Workstation"
	TEST_FINISHED_GOOD = "OPT-FG-001"
	TEST_RAW_MATERIAL = "OPT-RM-001"

	# Bind the existing native master/user builders, without importing a TestCase
	# class into this module (which would make unittest collect its entire suite).
	_ensure_company = reporting_fixtures.TestWorkerReporting.__dict__["_ensure_company"]
	_ensure_warehouse = reporting_fixtures.TestWorkerReporting.__dict__["_ensure_warehouse"]
	_ensure_item = reporting_fixtures.TestWorkerReporting.__dict__["_ensure_item"]
	_ensure_master_fixtures = reporting_fixtures.TestWorkerReporting.__dict__["_ensure_master_fixtures"]
	_email = reporting_fixtures.TestWorkerReporting._email
	_make_user = reporting_fixtures.TestWorkerReporting._make_user
	_make_worker = reporting_fixtures.TestWorkerReporting._make_worker
	_make_employee = reporting_fixtures.TestWorkerReporting._make_employee
	_make_supervisor = reporting_fixtures.TestWorkerReporting._make_supervisor
	_grant_company = reporting_fixtures.TestWorkerReporting._grant_company
	_make_rate = reporting_fixtures.TestWorkerReporting._make_rate

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._ensure_master_fixtures()

	def setUp(self):
		super().setUp()
		frappe.local.db = self._primary_connection
		frappe.set_user("Administrator")
		self._settings_before = {
			"Manufacturing Settings": {"disable_capacity_planning": frappe.db.get_single_value("Manufacturing Settings", "disable_capacity_planning")},
			"Stock Settings": {field: frappe.db.get_single_value("Stock Settings", field)
				for field in ("enable_stock_reservation", "auto_reserve_stock", "allow_negative_stock")},
		}
		self.enterContext(patch.dict(frappe.conf, {"enable_operation_dispatch_pool": 1}))
		self.enterContext(self.change_settings("Manufacturing Settings", {"disable_capacity_planning": 1}))
		self.enterContext(self.change_settings("Stock Settings", {
			"enable_stock_reservation": 1, "auto_reserve_stock": 0, "allow_negative_stock": 0,
		}))
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

	def _planned_card(self, qty=10, *, skip_transfer=False):
		plan = frappe.get_doc({
			"doctype": "Production Plan", "company": self.TEST_COMPANY, "get_items_from": "",
			"reserve_stock": 0, "po_items": [{
				"item_code": self.TEST_FINISHED_GOOD, "bom_no": self.master_bom,
				"planned_qty": qty, "stock_uom": "Nos", "warehouse": self.fg_warehouse,
				"planned_start_date": now_datetime(),
			}],
		}).insert().submit()
		order = frappe.new_doc("Work Order")
		order.update({
			"production_item": self.TEST_FINISHED_GOOD, "bom_no": self.master_bom, "qty": qty,
			"company": self.TEST_COMPANY, "stock_uom": "Nos", "source_warehouse": self.source_warehouse,
			"wip_warehouse": self.wip_warehouse, "fg_warehouse": self.fg_warehouse,
			"skip_transfer": int(skip_transfer), "planned_start_date": now_datetime(),
			"transfer_material_against": "Work Order", "production_plan": plan.name,
			"production_plan_item": plan.po_items[0].name,
		})
		order.get_items_and_operations_from_bom()
		for row in order.required_items:
			row.source_warehouse = self.source_warehouse
		order.insert().submit()
		name = frappe.get_all("Job Card", filters={"work_order": order.name}, pluck="name", limit=1)[0]
		card = frappe.get_doc("Job Card", name)
		if not frappe.db.exists("Operation Wage Rate", {
			"company": card.company, "operation": card.operation, "enabled": 1,
		}):
			self._make_rate(card)
		return card

	def _fund_and_issue(self, card, qty=None):
		qty = card.for_quantity if qty is None else qty
		# Opening fixture stock must precede the transfer regardless of small
		# wall-clock adjustments during a rapid test transaction.
		receipt = make_stock_entry(item_code=self.TEST_RAW_MATERIAL, to_warehouse=self.source_warehouse,
			company=self.TEST_COMPANY, qty=qty, basic_rate=1,
			posting_date=add_days(nowdate(), -1), posting_time="12:00:00")
		entry = frappe.get_doc(make_order_stock_entry(card.work_order, "Material Transfer for Manufacture", qty=qty))
		entry.insert().submit()
		entry.flags.pool_test_source_receipt = receipt.name
		return entry

	def _assign_first(self, card, **overrides):
		args = dict(job_card=card.name, assignments=[{"employee": self.worker, "assigned_qty": card.for_quantity}],
			expected_qty=card.for_quantity, expected_work_order=card.work_order, expected_operation_id=card.operation_id)
		args.update(overrides)
		return dispatch_pool.assign_workers_first(**args)

	def _pool(self, card):
		return dispatch_pool.get_operation_dispatch_pool(filters={"search": card.work_order})

	def _pool_row(self, card):
		return next(row for row in self._pool(card)["rows"] if row["job_card"] == card.name)

	def _business_snapshot(self, card):
		return {
			"job_card": frappe.db.get_value("Job Card", card.name, ["modified", "custom_worker_reporting_enabled"], as_dict=True),
			"work_order": frappe.db.get_value("Work Order", card.work_order, ["modified", "custom_worker_reporting_enabled"], as_dict=True),
			"assignments": frappe.db.count("Job Card Worker Assignment", {"job_card": card.name}),
			"reports": frappe.db.count("Job Card Work Report", {"job_card": card.name}),
			"stock_entries": frappe.db.count("Stock Entry", {"work_order": card.work_order}),
			"reservations": frappe.db.count("Stock Reservation Entry", {"voucher_type": "Work Order", "voucher_no": card.work_order}),
		}

	def test_first_pool_assignment_matches_old_native_assignment_facts(self):
		old_card, new_card = self._planned_card(), self._planned_card()
		self._fund_and_issue(old_card)
		self._fund_and_issue(new_card)
		with self.set_user(self.supervisor):
			old = service.assign_workers(old_card.name, [{"employee": self.worker, "assigned_qty": 10}])[0]
			result = self._assign_first(new_card)
		self.assertEqual(result["status"], "assigned")
		new = frappe.get_doc("Job Card Worker Assignment", {"job_card": new_card.name, "employee": self.worker})
		for field in ("company", "employee", "employee_user", "supervisor", "assigned_qty", "job_card_qty", "status", "assigned_by"):
			self.assertEqual(new.get(field), old.get(field), field)
		self.assertEqual(new.work_order, new_card.work_order)
		self.assertEqual(new.operation_id, new_card.operation_id)
		self.assertEqual(frappe.db.count("Job Card Worker Assignment", {"job_card": new_card.name}), 1)
		self.assertEqual(frappe.db.count("Job Card Work Report", {"job_card": new_card.name}), 0)

	def test_pool_get_does_not_write_or_reserve_direct_consumption_material(self):
		card = self._planned_card(skip_transfer=True)
		make_stock_entry(item_code=self.TEST_RAW_MATERIAL, to_warehouse=self.source_warehouse,
			company=self.TEST_COMPANY, qty=card.for_quantity, basic_rate=1,
			posting_date=add_days(nowdate(), -1), posting_time="12:00:00")
		before = self._business_snapshot(card)
		with self.set_user(self.supervisor):
			with self.assertQueryCount(0, query_type=("insert", "update", "delete")):
				row = self._pool_row(card)
		self.assertTrue(row["can_assign_first"])
		self.assertEqual(self._business_snapshot(card), before)
		self.assertEqual(before["reservations"], 0)

	def test_disabled_gate_prevents_reads_and_writes_but_keeps_old_assignment(self):
		card = self._planned_card()
		self._fund_and_issue(card)
		before = self._business_snapshot(card)
		with patch.dict(frappe.conf, {"enable_operation_dispatch_pool": 0}), self.set_user(self.supervisor):
			with self.assertRaises(frappe.PermissionError):
				self._pool(card)
			with self.assertRaises(frappe.PermissionError):
				self._assign_first(card)
			self.assertEqual(self._business_snapshot(card), before)
			old = service.assign_workers(card.name, [{"employee": self.worker, "assigned_qty": card.for_quantity}])
			self.assertEqual(len(old), 1)

	def test_role_and_company_scope_are_enforced_on_both_endpoints(self):
		card = self._planned_card()
		self._fund_and_issue(card)
		warehouse_user = self._make_user("Process Simplification Warehouse Operator")
		unscoped_manager = self._make_user("Process Simplification Production Manager")
		foreign_manager = self._make_user("Process Simplification Production Manager")
		self._grant_company(foreign_manager, self.OTHER_COMPANY)
		for user in (self.worker_user, warehouse_user, unscoped_manager):
			with self.subTest(user=user), self.set_user(user):
				with self.assertRaises(frappe.PermissionError):
					self._pool(card)
				with self.assertRaises(frappe.PermissionError):
					self._assign_first(card)
		with self.set_user(foreign_manager):
			self.assertEqual(self._pool(card)["rows"], [])
			with self.assertRaises(frappe.PermissionError):
				self._assign_first(card)
		owner = self._make_user("Process Simplification Owner")
		self._grant_company(owner, self.TEST_COMPANY)
		with self.set_user(owner):
			self.assertTrue(self._pool_row(card)["can_assign_first"])
		self.assertFalse(frappe.db.exists("Job Card Worker Assignment", {"job_card": card.name}))

	def test_partial_issue_100_to_40_still_blocks_first_assignment(self):
		card = self._planned_card(qty=100)
		self._fund_and_issue(card, qty=40)
		before = self._business_snapshot(card)
		with self.set_user(self.supervisor):
			row = self._pool_row(card)
			self.assertFalse(row["can_assign_first"])
			self.assertEqual(row["block_code"], "MATERIAL_NOT_FULLY_ISSUED")
			with self.assertRaises(frappe.ValidationError):
				self._assign_first(card)
			with self.assertRaises(frappe.ValidationError):
				service.assign_workers(card.name, [{"employee": self.worker, "assigned_qty": 100}])
		self.assertEqual(self._business_snapshot(card), before)

	def test_sequence_pending_is_not_made_assignable_by_pool(self):
		card = self._planned_card()
		self._fund_and_issue(card)
		order = frappe.get_doc("Work Order", card.work_order)
		previous_operation = frappe.get_doc({"doctype": "Operation", "name": "Pool Previous " + random_string(8)}).insert()
		previous = order.append("operations", {"operation": previous_operation.name,
			"workstation": self.TEST_WORKSTATION, "time_in_mins": 1, "sequence_id": 1,
			"status": "Pending", "completed_qty": 0})
		previous.docstatus = 1
		previous.db_insert()
		frappe.db.set_value("Work Order Operation", card.operation_id, "sequence_id", 2)
		frappe.db.set_value("Job Card", card.name, "sequence_id", 2)
		with self.set_user(self.supervisor):
			row = self._pool_row(card)
			self.assertFalse(row["can_assign_first"])
			self.assertEqual(row["block_code"], "PREVIOUS_OPERATION_PENDING")
			with self.assertRaises(frappe.ValidationError):
				self._assign_first(card)
		self.assertFalse(frappe.db.exists("Job Card Worker Assignment", {"job_card": card.name}))

	def test_stale_quantity_and_identity_are_rejected_without_assignment(self):
		card = self._planned_card()
		self._fund_and_issue(card)
		before = self._business_snapshot(card)
		with self.set_user(self.supervisor):
			for changed in ({"expected_qty": 9}, {"expected_work_order": "STALE-WORK-ORDER"},
				{"expected_operation_id": "stale-operation-row"}):
				with self.subTest(changed=changed), self.assertRaises(frappe.ValidationError):
					self._assign_first(card, **changed)
		self.assertEqual(self._business_snapshot(card), before)

	def test_repeat_or_cancelled_assignment_history_cannot_be_replaced(self):
		card = self._planned_card()
		self._fund_and_issue(card)
		with self.set_user(self.supervisor):
			self._assign_first(card)
			with self.assertRaises(frappe.ValidationError):
				self._assign_first(card)
		assignment = frappe.get_doc("Job Card Worker Assignment", {"job_card": card.name})
		# Preserve an imported/cancelled historical row. A new first dispatch may
		# never erase its identity even when no active assignment remains.
		frappe.db.set_value("Job Card Worker Assignment", assignment.name, "status", "Cancelled")
		with self.set_user(self.supervisor):
			self.assertFalse(self._pool_row(card)["can_assign_first"])
			with self.assertRaises(frappe.ValidationError):
				self._assign_first(card)
		self.assertEqual(frappe.db.count("Job Card Worker Assignment", {"job_card": card.name}), 1)
		self.assertEqual(frappe.db.get_value("Job Card Worker Assignment", assignment.name, "status"), "Cancelled")

	def test_old_single_card_api_keeps_its_unstarted_replacement_behavior(self):
		card = self._planned_card()
		self._fund_and_issue(card)
		second_user = self._make_worker()
		second_worker = frappe.db.get_value("Employee", {"user_id": second_user}, "name")
		with self.set_user(self.supervisor):
			self._assign_first(card)
			result = service.assign_workers(card.name, [{"employee": second_worker, "assigned_qty": card.for_quantity}])
		self.assertEqual([row.employee for row in result], [second_worker])
		self.assertEqual(frappe.get_all("Job Card Worker Assignment", filters={"job_card": card.name}, pluck="employee"), [second_worker])

	def test_zero_quantity_native_time_history_still_blocks_first_assignment(self):
		card = self._planned_card()
		self._fund_and_issue(card)
		row = card.append("time_logs", {"employee": self.worker, "from_time": now_datetime(), "completed_qty": 0})
		row.db_insert()
		with self.set_user(self.supervisor):
			self.assertFalse(self._pool_row(card)["can_assign_first"])
			with self.assertRaises(frappe.ValidationError):
				self._assign_first(card)
		self.assertFalse(frappe.db.exists("Job Card Worker Assignment", {"job_card": card.name}))

	def test_orphaned_work_report_history_blocks_first_assignment(self):
		card = self._planned_card()
		self._fund_and_issue(card)
		# Simulate an imported report whose old assignment was lost. This is a
		# history guard test, not a supported way to create new work reports.
		frappe.get_doc({"doctype": "Job Card Work Report", "name": "POOL-HISTORY-" + random_string(12),
			"job_card": card.name, "work_order": card.work_order, "company": card.company,
			"operation": card.operation, "operation_id": card.operation_id,
			"assignment": "IMPORTED-MISSING-ASSIGNMENT", "employee": self.worker,
			"employee_user": self.worker_user, "status": "Rejected", "completed_qty": 1,
		}).db_insert()
		with self.set_user(self.supervisor):
			self.assertFalse(self._pool_row(card)["can_assign_first"])
			with self.assertRaises(frappe.ValidationError):
				self._assign_first(card)
		self.assertFalse(frappe.db.exists("Job Card Worker Assignment", {"job_card": card.name}))

	def test_orphaned_movement_history_blocks_first_assignment(self):
		card = self._planned_card()
		self._fund_and_issue(card)
		frappe.get_doc({"doctype": "Job Card Assignment Movement", "name": "POOL-MOVEMENT-" + random_string(12),
			"movement_type": "Release", "job_card": card.name, "work_order": card.work_order,
			"company": card.company, "operation": card.operation, "operation_id": card.operation_id,
			"source_assignment": "IMPORTED-MISSING-ASSIGNMENT", "qty": 1,
		}).db_insert()
		with self.set_user(self.supervisor):
			self.assertFalse(self._pool_row(card)["can_assign_first"])
			with self.assertRaises(frappe.ValidationError):
				self._assign_first(card)
		self.assertFalse(frappe.db.exists("Job Card Worker Assignment", {"job_card": card.name}))

	def test_concurrent_committed_history_is_seen_after_stale_list_snapshot(self):
		balances_before = self._concurrency_balances()
		card = self._planned_card()
		issue = self._fund_and_issue(card)
		plan = frappe.db.get_value("Work Order", card.work_order, "production_plan")
		rate = frappe.db.get_value("Operation Wage Rate", {"company": card.company, "operation": card.operation}, "name")
		winner = None
		try:
			# Native fixture creation takes protective range locks. End that
			# transaction before establishing the reader's old snapshot; otherwise
			# the independent writer can only time out on fixture-owned gap locks.
			frappe.db.commit()
			with self.set_user(self.supervisor):
				self.assertTrue(self._pool_row(card)["can_assign_first"])
			with self.secondary_connection(), self.set_user(self.supervisor):
				# The winning request uses the same public business service, including
				# material, employee, wage and native Job Card checks. No raw history
				# insertion or bypass of the dispatch gate is involved.
				result = self._assign_first(card)
				winner = result["assignments"][0].name
				frappe.db.commit()
			with self.primary_connection(), self.set_user(self.supervisor):
				try:
					with self.assertRaisesRegex(frappe.ValidationError, "派工或生产历史"):
						self._assign_first(card)
				except frappe.QueryDeadlockError:
					# MariaDB snapshot isolation may reject the stale transaction before
					# the locked current read. This is also a safe no-write outcome.
					pass
			with self.primary_connection():
				frappe.db.rollback()
				rows = frappe.get_all("Job Card Worker Assignment", filters={"job_card": card.name},
					fields=["name", "employee", "assigned_qty"])
				self.assertEqual([(row.name, row.employee, row.assigned_qty) for row in rows],
					[(winner, self.worker, card.for_quantity)])
		finally:
			with self.primary_connection():
				frappe.db.rollback()
			with self.secondary_connection():
				frappe.db.rollback()
			with self.primary_connection(), self.set_user("Administrator"):
				self._cleanup_committed_pool_fixture(card, plan, issue, rate, balances_before)

	def _concurrency_balances(self):
		fields = ("actual_qty", "planned_qty", "reserved_qty_for_production")
		return {
			(item, warehouse): tuple(float((frappe.db.get_value("Bin", {"item_code": item,
				"warehouse": warehouse}, field) or 0)) for field in fields)
			for item, warehouse in ((self.TEST_RAW_MATERIAL, self.source_warehouse),
				(self.TEST_RAW_MATERIAL, self.wip_warehouse), (self.TEST_FINISHED_GOOD, self.fg_warehouse))
		}

	def _cleanup_committed_pool_fixture(self, card, plan, issue, rate, balances_before):
		"""Reverse native quantity effects, then delete only this test's records."""
		delete_rows = reporting_fixtures.TestWorkerReporting._delete_test_rows_and_children
		for assignment in frappe.get_all("Job Card Worker Assignment", {"job_card": card.name}, pluck="name"):
			service.unassign_worker(assignment)
		stock_entries = [issue.name, issue.flags.pool_test_source_receipt]
		for name in stock_entries:
			doc = frappe.get_doc("Stock Entry", name)
			if doc.docstatus == 1:
				doc.cancel()
		# Keep the stock ledger, GL and Bin reversal in native cancellation. The
		# standard deletion setting then removes the test vouchers' linked ledgers.
		with self.change_settings("Accounts Settings", {"delete_linked_ledger_entries": 1}):
			for name in stock_entries:
				frappe.delete_doc("Stock Entry", name, ignore_permissions=True, force=True, delete_permanently=True)
		order = frappe.get_doc("Work Order", card.work_order)
		if order.docstatus == 1:
			order.cancel()
		delete_rows(self, "Job Card", [card.name])
		delete_rows(self, "Work Order", [card.work_order])
		plan_doc = frappe.get_doc("Production Plan", plan)
		if plan_doc.docstatus == 1:
			plan_doc.cancel()
		delete_rows(self, "Production Plan", [plan])
		frappe.db.delete("Operation Wage Rate", {"name": rate})
		# Cancellation can create valuation-reposting records scoped to these
		# exact vouchers. They have no remaining ledger after native deletion.
		reposts = frappe.get_all("Repost Item Valuation", {"voucher_type": "Stock Entry", "voucher_no": ["in", stock_entries]}, pluck="name")
		delete_rows(self, "Repost Item Valuation", reposts)
		users = [self.worker_user, self.supervisor, self.wage_manager]
		employees = frappe.get_all("Employee", {"user_id": ["in", users]}, pluck="name")
		contacts = frappe.get_all("Contact", {"user": ["in", users]}, pluck="name")
		frappe.db.delete("User Permission", {"user": ["in", users]})
		for doctype, names in (("Employee", employees), ("User", users), ("Contact", contacts)):
			for name in names:
				if frappe.db.exists(doctype, name):
					frappe.delete_doc(doctype, name, ignore_permissions=True, force=True, delete_permanently=True)
		self.assertEqual(self._concurrency_balances(), balances_before)
		self.assertFalse(frappe.db.exists("Stock Ledger Entry", {"voucher_type": "Stock Entry", "voucher_no": ["in", stock_entries]}))
		self.assertFalse(frappe.db.exists("GL Entry", {"voucher_type": "Stock Entry", "voucher_no": ["in", stock_entries]}))
		for doctype, values in self._settings_before.items():
			frappe.db.set_single_value(doctype, values)
			frappe.clear_document_cache(doctype, doctype)
		frappe.db.commit()
