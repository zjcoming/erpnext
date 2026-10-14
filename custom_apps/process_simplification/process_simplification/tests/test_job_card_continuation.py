"""In-flight managed cards retain approved facts while remaining work carries forward."""

from datetime import datetime, time, timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_months, get_datetime, get_first_day, nowdate

from process_simplification.production_reporting import continuation, domain, service, summary
from process_simplification.production_reporting.assignment_movement import (
	assignment_effective_qty, assignment_material_allocation_qty,
)
from process_simplification.tests import test_operation_dispatch_pool_integration as fixtures
from process_simplification.tests import test_worker_reporting as reporting_fixtures


class TestJobCardContinuation(IntegrationTestCase):
	TEST_COMPANY = "Continuation Test Company"
	TEST_COMPANY_ABBR = "CTC"
	OTHER_COMPANY = "Continuation Other Company"
	OTHER_COMPANY_ABBR = "CTO"
	TEST_OPERATION = "Continuation Test Operation"
	TEST_WORKSTATION = "Continuation Test Workstation"
	TEST_FINISHED_GOOD = "CTC-FG-001"
	TEST_RAW_MATERIAL = "CTC-RM-001"

	_ensure_company = fixtures.TestOperationDispatchPoolIntegration.__dict__["_ensure_company"]
	_ensure_warehouse = fixtures.TestOperationDispatchPoolIntegration.__dict__["_ensure_warehouse"]
	_ensure_item = fixtures.TestOperationDispatchPoolIntegration.__dict__["_ensure_item"]
	_ensure_master_fixtures = fixtures.TestOperationDispatchPoolIntegration.__dict__["_ensure_master_fixtures"]
	_email = fixtures.TestOperationDispatchPoolIntegration._email
	_make_user = fixtures.TestOperationDispatchPoolIntegration._make_user
	_make_worker = fixtures.TestOperationDispatchPoolIntegration._make_worker
	_make_employee = fixtures.TestOperationDispatchPoolIntegration._make_employee
	_make_supervisor = fixtures.TestOperationDispatchPoolIntegration._make_supervisor
	_grant_company = fixtures.TestOperationDispatchPoolIntegration._grant_company
	_make_rate = fixtures.TestOperationDispatchPoolIntegration._make_rate
	_planned_card = fixtures.TestOperationDispatchPoolIntegration._planned_card
	_fund_and_issue = fixtures.TestOperationDispatchPoolIntegration._fund_and_issue
	_submit_as = reporting_fixtures.TestWorkerReporting._submit_as
	_submit = reporting_fixtures.TestWorkerReporting._submit
	_approve = reporting_fixtures.TestWorkerReporting._approve

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._ensure_master_fixtures()

	def setUp(self):
		super().setUp()
		frappe.local.db = self._primary_connection
		frappe.set_user("Administrator")
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

	def _flow(self, *, qty=4, completed=2, rate=None):
		card = self._planned_card(qty)
		if rate is not None:
			with self.set_user(self.wage_manager):
				rule = frappe.get_doc("Operation Wage Rate", {"company": card.company, "operation": card.operation, "enabled": 1})
				rule.piecework_rate = rate
				rule.save()
		self._fund_and_issue(card)
		with self.set_user(self.supervisor):
			assignment = service.assign_worker(card.name, self.worker)
		report = self._submit(assignment, completed)
		self._approve(report)
		return card.reload(), assignment.reload(), report.reload()

	def _carry(self, card, request_id="continuation-test", **kwargs):
		with self.set_user(self.supervisor):
			return continuation.finalize_partial_job_card(card.name, request_id, **kwargs)

	def _cards(self, work_order):
		return frappe.get_all("Job Card", filters={"work_order": work_order},
			fields=["name", "for_quantity", "total_completed_qty", "pending_qty", "docstatus"], order_by="creation,name")

	def _submit_native_pending(self, card, completed):
		started = get_datetime() - timedelta(days=1)
		card.pending_qty = card.for_quantity - completed
		card.append("time_logs", dict(employee=self.worker, completed_qty=completed,
			from_time=started, to_time=started + timedelta(minutes=1)))
		with service._allow_work_order_update_for_approval(card.work_order):
			card.save()
			card.submit()
		return card.reload()

	def test_inflight_card_carries_remaining_quantity_without_rewriting_reports_or_wages(self):
		card, assignment, report = self._flow()
		with self.set_user(self.supervisor):
			context = service.get_work_order_assignment_context(card.work_order)
			self.assertTrue(context["job_cards"][0]["can_finalize_partial"])
		report_before = report.as_dict()
		assignment_before = {field: assignment.get(field) for field in (
			"job_card", "job_card_qty", "assigned_qty", "employee", "work_order", "operation_id")}
		stock_before = frappe.db.count("Stock Entry", {"work_order": card.work_order})
		result = self._carry(card, expected_completed_qty=2, expected_remaining_qty=2)
		target = frappe.get_doc("Job Card", result["job_card"])
		carried = frappe.get_doc("Job Card Worker Assignment", result["assignments"][0])
		card.reload(); assignment.reload(); report.reload()
		self.assertEqual((card.for_quantity, card.total_completed_qty, card.pending_qty, card.docstatus), (4, 2, 2, 1))
		self.assertEqual((target.for_quantity, target.total_completed_qty, target.docstatus), (2, 0, 0))
		self.assertEqual((target.work_order, target.operation_id, target.company), (card.work_order, card.operation_id, card.company))
		self.assertEqual(report.as_dict(), report_before)
		self.assertEqual({field: assignment.get(field) for field in assignment_before}, assignment_before)
		self.assertEqual((assignment.status, assignment_effective_qty(assignment), assignment_material_allocation_qty(assignment)), ("Completed", 2, 2))
		self.assertEqual((carried.employee, carried.supervisor, carried.assigned_qty, assignment_effective_qty(carried)), (self.worker, self.supervisor, 2, 2))
		self.assertEqual(frappe.db.count("Stock Entry", {"work_order": card.work_order}), stock_before)
		self.assertEqual(frappe.db.get_value("Work Order Operation", card.operation_id, "completed_qty"), 2)
		self.assertNotIn(continuation._CONTEXT_FLAG, frappe.flags)
		with self.set_user(self.supervisor):
			context = service.get_work_order_assignment_context(card.work_order)
			old_row = next(row for row in context["job_cards"] if row["name"] == card.name)
			new_row = next(row for row in context["job_cards"] if row["name"] == target.name)
			self.assertEqual((old_row["remaining_qty"], old_row["continued_qty"], old_row["continued_to"]), (0, 2, target.name))
			self.assertFalse(new_row["can_assign"])
		with self.set_user(self.supervisor):
			service.approve_work_report(report.name)
		remaining_report = self._submit(carried, 2)
		self._approve(remaining_report)
		self.assertEqual(frappe.db.get_value("Work Order Operation", card.operation_id, "completed_qty"), 4)
		self.assertEqual(frappe.db.get_value("Job Card", target.name, "docstatus"), 1)
		self.assertEqual(report.reload().as_dict(), report_before)

	def test_repeated_request_and_new_request_for_same_source_return_the_same_continuation(self):
		card, _, _ = self._flow()
		first = self._carry(card)
		for request_id in ("continuation-test", "another-click"):
			result = self._carry(card, request_id)
			self.assertTrue(result["reused"])
			self.assertEqual(result["job_card"], first["job_card"])
		self.assertEqual(len(self._cards(card.work_order)), 2)
		self.assertEqual(frappe.db.count("Job Card Assignment Movement", {"job_card": card.name, "movement_type": "Continuation"}), 1)

	def test_carries_only_unfinished_worker_and_does_not_duplicate_completed_worker(self):
		card = self._planned_card(4)
		self._fund_and_issue(card)
		other_user = self._make_worker()
		other_employee = frappe.db.get_value("Employee", {"user_id": other_user}, "name")
		with self.set_user(self.supervisor):
			assignments = service.assign_workers(card.name, [dict(employee=self.worker, assigned_qty=2), dict(employee=other_employee, assigned_qty=2)])
		report = self._submit(assignments[0], 2)
		self._approve(report)
		result = self._carry(card)
		self.assertEqual(len(result["assignments"]), 1)
		carried = frappe.get_doc("Job Card Worker Assignment", result["assignments"][0])
		self.assertEqual((carried.employee, carried.assigned_qty), (other_employee, 2))
		self.assertEqual(assignment_effective_qty(assignments[1]), 0)
		report2 = self._submit_as(carried, other_user, 2)
		self._approve(report2)
		self.assertEqual(frappe.db.get_value("Work Order Operation", card.operation_id, "completed_qty"), 4)

	def test_worker_and_foreign_company_are_denied_while_company_reviewers_keep_existing_scope(self):
		card, _, _ = self._flow()
		foreign = self._make_user("Process Simplification Production Manager")
		self._grant_company(foreign, self.OTHER_COMPANY)
		other_supervisor = self._make_supervisor()
		for user in (self.worker_user, foreign):
			with self.set_user(user), self.assertRaises(frappe.PermissionError):
				continuation.finalize_partial_job_card(card.name, "denied")
		self.assertEqual(len(self._cards(card.work_order)), 1)
		# Existing REVIEW_ROLES all have administrative review scope inside their
		# company. Continuation must not invent a different supervisor role model.
		with self.set_user(other_supervisor):
			result = continuation.finalize_partial_job_card(card.name, "company-reviewer")
		self.assertEqual(frappe.db.get_value("Job Card Worker Assignment", result["assignments"][0], "supervisor"), self.supervisor)

	def test_pending_active_and_stale_quantity_block_without_closing_original_card(self):
		card, assignment, _ = self._flow()
		with self.assertRaises(frappe.ValidationError):
			self._carry(card, expected_completed_qty=3)
		with self.set_user(self.worker_user):
			active = service.start_work_session(assignment.name, "active-after-partial")
		with self.assertRaises(frappe.ValidationError):
			self._carry(card)
		with self.set_user(self.worker_user):
			service.cancel_work_session(active.name)
		self._submit(assignment, 1)
		with self.assertRaises(frappe.ValidationError):
			self._carry(card)
		self.assertEqual(frappe.db.get_value("Job Card", card.name, "docstatus"), 0)
		self.assertEqual(len(self._cards(card.work_order)), 1)

	def test_stopped_and_completed_work_orders_block_new_continuation(self):
		card, _, _ = self._flow()
		for status in ("Stopped", "Closed", "Completed", "Cancelled"):
			frappe.db.set_value("Work Order", card.work_order, "status", status)
			with self.assertRaises(frappe.ValidationError):
				self._carry(card)
		self.assertEqual(len(self._cards(card.work_order)), 1)

	def test_failure_after_original_submit_rolls_back_and_restores_request_scope(self):
		card, _, report = self._flow()
		before = report.as_dict()
		frappe.db.savepoint("continuation_failure")
		with patch("erpnext.manufacturing.doctype.work_order.work_order.create_job_card", side_effect=frappe.ValidationError("injected continuation failure")):
			with self.assertRaisesRegex(frappe.ValidationError, "injected"):
				self._carry(card)
		self.assertNotIn(continuation._CONTEXT_FLAG, frappe.flags)
		self.assertNotIn("worker_reporting_approval_work_order", frappe.flags)
		frappe.db.rollback(save_point="continuation_failure")
		self.assertEqual(frappe.db.get_value("Job Card", card.name, "docstatus"), 0)
		self.assertEqual(frappe.db.get_value("Work Order Operation", card.operation_id, "completed_qty"), 0)
		self.assertEqual(len(self._cards(card.work_order)), 1)
		self.assertEqual(report.reload().as_dict(), before)

	def test_continuation_assignment_and_card_cannot_be_removed_or_replaced(self):
		card, _, _ = self._flow()
		result = self._carry(card)
		with self.set_user(self.supervisor), self.assertRaises(frappe.ValidationError):
			service.unassign_worker(result["assignments"][0])
		other_user = self._make_worker()
		other_employee = frappe.db.get_value("Employee", {"user_id": other_user}, "name")
		with self.set_user(self.supervisor), self.assertRaises(frappe.ValidationError):
			service.assign_worker(result["job_card"], other_employee)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc("Job Card", result["job_card"]).discard()
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc("Work Order", card.work_order).update_status("Closed")

	def test_remaining_card_cannot_reuse_material_already_consumed_by_original_reports(self):
		card, _, _ = self._flow()
		result = self._carry(card)
		target = domain.job_card_values(result["job_card"])
		# This models a later net material return without changing posted stock facts.
		with patch.object(domain, "work_order_material_capacity", return_value=2):
			self.assertEqual(domain.material_reportable_qty(target), 0)
		with patch.object(domain, "work_order_material_capacity", return_value=3):
			self.assertEqual(domain.material_reportable_qty(target), 1)
		self.assertEqual(domain.operation_reporting_used_qty(target), 2)

	def test_partial_issue_dispatch_carries_balance_and_waits_for_replenishment(self):
		card = self._planned_card(qty=200)
		first_issue = self._fund_and_issue(card, qty=2)
		other_user = self._make_worker()
		other_employee = frappe.db.get_value("Employee", {"user_id": other_user}, "name")
		with self.set_user(self.supervisor):
			assignments = service.assign_workers(card.name, [
				dict(employee=self.worker, assigned_qty=100),
				dict(employee=other_employee, assigned_qty=100),
			])
		by_employee = {row.employee: row for row in assignments}
		self.assertEqual(sum(row.assigned_qty for row in assignments), 200)
		self.assertEqual(domain.work_order_material_capacity(domain.job_card_values(card.name)), 2)
		with self.set_user(self.worker_user):
			active = service.start_work_session(by_employee[self.worker].name, "partial-issue-start")
			before_invalid_finish = active.reload().as_dict()
			ended_at = get_datetime(active.actual_start_time) + timedelta(minutes=1)
			with self.assertRaises(frappe.ValidationError):
				service.finish_work_session(active.name, 3, "partial-issue-overreport", ended_at=ended_at)
			self.assertEqual(active.reload().as_dict(), before_invalid_finish)
			first_report = service.finish_work_session(active.name, 2, "partial-issue-finish", ended_at=ended_at)
		# Pending production already occupies both issued units for all workers.
		with self.set_user(other_user), self.assertRaises(frappe.ValidationError):
			service.start_work_session(by_employee[other_employee].name, "partial-issue-third-unit")
		self.assertEqual(frappe.db.count("Job Card Work Report", {"work_order": card.work_order}), 1)
		self._approve(first_report)
		first_report_before = first_report.reload().as_dict()
		result = self._carry(card, expected_completed_qty=2, expected_remaining_qty=198)
		target = frappe.get_doc("Job Card", result["job_card"])
		carried = {row.employee: row for row in (
			frappe.get_doc("Job Card Worker Assignment", name) for name in result["assignments"]
		)}
		self.assertEqual((card.reload().for_quantity, card.total_completed_qty, card.pending_qty, card.docstatus), (200, 2, 198, 1))
		self.assertEqual((target.work_order, target.operation_id, target.for_quantity, target.docstatus),
			(card.work_order, card.operation_id, 198, 0))
		self.assertEqual((carried[self.worker].assigned_qty, carried[other_employee].assigned_qty), (98, 100))
		self.assertEqual(target.get_current_operation_data()[0].pending_qty, 198)
		self.assertEqual(domain.material_reportable_qty(domain.job_card_values(target.name)), 0)
		for employee, user in ((self.worker, self.worker_user), (other_employee, other_user)):
			with self.set_user(user), self.assertRaises(frappe.ValidationError):
				service.start_work_session(carried[employee].name, "partial-issue-wait-" + employee)
		self.assertEqual(frappe.db.count("Job Card Work Report", {"work_order": card.work_order}), 1)
		self.assertEqual(frappe.db.count("Stock Entry", {
			"work_order": card.work_order, "purpose": "Material Transfer for Manufacture", "docstatus": 1,
		}), 1)

		second_issue = self._fund_and_issue(card, qty=2)
		self.assertNotEqual(second_issue.name, first_issue.name)
		self.assertEqual(domain.work_order_material_capacity(domain.job_card_values(target.name)), 4)
		self.assertEqual(domain.material_reportable_qty(domain.job_card_values(target.name)), 2)
		second_report = self._submit(carried[self.worker], 1)
		third_report = self._submit_as(carried[other_employee], other_user, 1)
		self._approve(second_report)
		self._approve(third_report)
		self.assertEqual(domain.material_reportable_qty(domain.job_card_values(target.name)), 0)
		self.assertEqual(domain.operation_reporting_used_qty(domain.job_card_values(target.name)), 4)
		self.assertEqual((frappe.db.get_value("Work Order", card.work_order, "qty"), target.reload().total_completed_qty), (200, 2))
		self.assertEqual(first_report.reload().as_dict(), first_report_before)
		approved = frappe.get_all("Job Card Work Report", filters={
			"work_order": card.work_order, "status": "Approved",
		}, fields=["name", "completed_qty", "wage_amount"])
		self.assertEqual({row.name for row in approved}, {first_report.name, second_report.name, third_report.name})
		self.assertEqual(sum(row.completed_qty for row in approved), 4)
		self.assertEqual(sum(row.wage_amount for row in approved),
			first_report_before["wage_amount"] + second_report.reload().rate + third_report.reload().rate)
		self.assertEqual(frappe.db.count("Stock Entry", {
			"work_order": card.work_order, "purpose": "Material Transfer for Manufacture", "docstatus": 1,
		}), 2)
		# Both worker movements describe one card's balance. Once the next card
		# submits, that historical balance must stop contributing to WO pending.
		self.assertEqual(target.get_current_operation_data()[0].pending_qty, 198)
		second_batch_reports = [row.reload().as_dict() for row in (second_report, third_report)]
		next_batch = self._carry(target, "second-material-batch", expected_completed_qty=2, expected_remaining_qty=196)
		latest = frappe.get_doc("Job Card", next_batch["job_card"])
		self.assertEqual(len(next_batch["assignments"]), 2)
		self.assertEqual((card.reload().pending_qty, target.reload().pending_qty, latest.for_quantity), (198, 196, 196))
		self.assertEqual(frappe.db.get_value("Work Order Operation", card.operation_id, "pending_qty"), 196)
		self.assertEqual(latest.get_current_operation_data()[0].completed_qty, 4)
		self.assertEqual(latest.get_current_operation_data()[0].pending_qty, 196)
		self.assertEqual(domain.material_reportable_qty(domain.job_card_values(latest.name)), 0)

		self._fund_and_issue(card, qty=2)
		latest_assignments = {row.employee: row for row in (
			frappe.get_doc("Job Card Worker Assignment", name) for name in next_batch["assignments"]
		)}
		self._approve(self._submit(latest_assignments[self.worker], 1))
		self._approve(self._submit_as(latest_assignments[other_employee], other_user, 1))
		third_batch = self._carry(latest, "third-material-batch", expected_completed_qty=2, expected_remaining_qty=194)
		self.assertEqual(len(third_batch["assignments"]), 2)
		self.assertEqual(frappe.db.get_value("Work Order Operation", card.operation_id, "pending_qty"), 194)
		self.assertEqual(frappe.db.get_value("Work Order Operation", card.operation_id, "completed_qty"), 6)
		self.assertEqual((card.reload().pending_qty, target.reload().pending_qty, latest.reload().pending_qty), (198, 196, 194))
		self.assertEqual(first_report.reload().as_dict(), first_report_before)
		self.assertEqual([row.reload().as_dict() for row in (second_report, third_report)], second_batch_reports)

	def test_asymmetric_native_issue_replenishes_only_short_component_after_partial_reports(self):
		from frappe.utils import flt
		from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry as make_order_stock_entry
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
		from process_simplification.production_workflow import bulk_materials as bulk

		second_material = "CTC-KIT-RM-" + frappe.generate_hash(length=8)
		self._ensure_item(second_material, is_purchase_item=True)
		bom = frappe.copy_doc(frappe.get_doc("BOM", self.master_bom))
		bom.is_default = 0
		bom.append("items", dict(item_code=second_material, qty=1, uom="Nos", stock_uom="Nos",
			rate=1, operation=self.TEST_OPERATION, source_warehouse=self.source_warehouse))
		bom.insert().submit()
		original_bom = self.master_bom
		self.master_bom = bom.name
		try:
			card = self._planned_card(6)
		finally:
			self.master_bom = original_bom
		warehouse = self._make_user("Process Simplification Warehouse Operator")
		self._grant_company(warehouse, self.TEST_COMPANY)
		self.assertNotIn("System Manager", frappe.get_roles(warehouse))

		def stock_balances():
			return {(item, location): flt(frappe.db.get_value("Bin", {
				"item_code": item, "warehouse": location}, "actual_qty"))
				for item in (self.TEST_RAW_MATERIAL, second_material)
				for location in (self.source_warehouse, self.wip_warehouse)}

		with patch.dict(frappe.conf, {"enable_operation_dispatch_pool": 1}), self.change_settings(
			"Manufacturing Settings", {"validate_components_quantities_per_bom": 0,
				"backflush_raw_materials_based_on": "BOM"}
		):
			self.assertEqual(set(stock_balances().values()), {0})
			for item in (self.TEST_RAW_MATERIAL, second_material):
				make_stock_entry(item_code=item, to_warehouse=self.source_warehouse,
					company=self.TEST_COMPANY, qty=4, basic_rate=1,
					posting_date=add_days(nowdate(), -1), posting_time="12:00:00")
			with self.set_user(warehouse):
				first_issue = frappe.get_doc(make_order_stock_entry(
					card.work_order, "Material Transfer for Manufacture", qty=2))
				next(row for row in first_issue.items if row.item_code == self.TEST_RAW_MATERIAL).qty = 4
				first_issue.insert().submit()
			self.assertEqual(first_issue.fg_completed_qty, 2)
			self.assertEqual({row.item_code: row.transfer_qty for row in first_issue.items},
				{self.TEST_RAW_MATERIAL: 4, second_material: 2})
			self.assertEqual(frappe.db.get_value("Work Order", card.work_order,
				"material_transferred_for_manufacturing"), 2)
			self.assertEqual(domain.work_order_material_capacity(domain.job_card_values(card.name)), 2)
			issued_stock = { (self.TEST_RAW_MATERIAL, self.source_warehouse): 0,
				(self.TEST_RAW_MATERIAL, self.wip_warehouse): 4,
				(second_material, self.source_warehouse): 2, (second_material, self.wip_warehouse): 2 }
			self.assertEqual(stock_balances(), issued_stock)
			with self.set_user(self.supervisor):
				assignment = service.assign_worker(card.name, self.worker)
			first_report = self._submit(assignment, 2)
			self.assertEqual(domain.material_reportable_qty(domain.job_card_values(card.name)), 0)
			self._approve(first_report)
			first_report_before = first_report.reload().as_dict()
			first_wage = (first_report.rate, first_report.wage_amount)
			carried = self._carry(card, expected_completed_qty=2, expected_remaining_qty=4)
			target = domain.job_card_values(carried["job_card"])
			self.assertEqual(domain.material_reportable_qty(target), 0)
			self.assertEqual(frappe.db.get_value("Work Order", card.work_order, "produced_qty"), 0)

			with self.set_user(self.supervisor):
				preview = bulk.preview_material_requests([card.work_order])["rows"][0]
				self.assertTrue(preview["can_select"])
				self.assertEqual((preview["available_qty"], preview["request_qty"]), (2, 2))
				self.assertEqual({row["item_code"]: row["qty"] for row in preview["items"]},
					{second_material: 2})
				made = bulk.request_material(card.work_order, preview["expected_state"], 2, allow_partial=1)
			entry = frappe.get_doc("Stock Entry", made["stock_entry"])
			self.assertEqual((entry.docstatus, entry.fg_completed_qty, entry.custom_process_coverage_qty), (0, 2, 2))
			self.assertFalse(entry.is_additional_transfer_entry)
			self.assertEqual(entry.custom_process_requested_by, self.supervisor)
			self.assertEqual({row.item_code: row.transfer_qty for row in entry.items}, {second_material: 2})
			self.assertEqual(stock_balances(), issued_stock)
			self.assertEqual(frappe.db.count("Stock Ledger Entry", {"voucher_no": entry.name}), 0)
			reservation_names = frappe.get_all("Stock Reservation Entry", filters={"docstatus": 1,
				"from_voucher_type": "Stock Entry", "from_voucher_no": entry.name}, pluck="name")
			reservations_before = [frappe.get_doc("Stock Reservation Entry", name).as_dict()
				for name in sorted(reservation_names)]
			self.assertEqual([(row["item_code"], row["warehouse"], row["reserved_qty"])
				for row in reservations_before], [(second_material, self.source_warehouse, 2)])
			entry_before = entry.as_dict()

			# Single-component replenishment must retain the native strict BOM refusal.
			frappe.db.savepoint("asymmetric_strict_bom")
			try:
				with self.change_settings("Manufacturing Settings", {"validate_components_quantities_per_bom": 1}), self.set_user(warehouse):
					confirmation = bulk.preview_material_submissions([entry.name])["rows"][0]
					with self.assertRaises(frappe.ValidationError) as blocked:
						bulk.submit_material(entry.name, confirmation["expected_state"])
					self.assertIn(bom.name, str(blocked.exception))
					self.assertIn(self.TEST_RAW_MATERIAL, str(blocked.exception))
			finally:
				frappe.db.rollback(save_point="asymmetric_strict_bom")
			self.assertEqual(frappe.get_doc("Stock Entry", entry.name).as_dict(), entry_before)
			self.assertEqual([frappe.get_doc("Stock Reservation Entry", name).as_dict()
				for name in sorted(reservation_names)], reservations_before)
			self.assertEqual(stock_balances(), issued_stock)
			self.assertEqual(frappe.db.count("Stock Ledger Entry", {"voucher_no": entry.name}), 0)
			self.assertEqual(first_report.reload().as_dict(), first_report_before)

			with self.set_user(warehouse):
				confirmation = bulk.preview_material_submissions([entry.name])["rows"][0]
				self.assertTrue(confirmation["can_select"])
				bulk.submit_material(entry.name, confirmation["expected_state"])
			entry.reload()
			self.assertEqual((entry.docstatus, entry.fg_completed_qty, entry.modified_by), (1, 2, warehouse))
			self.assertEqual(frappe.db.get_value("Work Order", card.work_order,
				"material_transferred_for_manufacturing"), 4)
			self.assertEqual(stock_balances(), {(self.TEST_RAW_MATERIAL, self.source_warehouse): 0,
				(self.TEST_RAW_MATERIAL, self.wip_warehouse): 4,
				(second_material, self.source_warehouse): 0, (second_material, self.wip_warehouse): 4})
			target = domain.job_card_values(carried["job_card"])
			self.assertEqual(domain.work_order_material_capacity(target), 4)
			self.assertEqual(domain.operation_reporting_used_qty(target), 2)
			self.assertEqual(domain.material_reportable_qty(target), 2)
			self.assertEqual(first_report.reload().as_dict(), first_report_before)
			self.assertEqual((first_report.rate, first_report.wage_amount), first_wage)

	def test_native_pending_without_continuation_keeps_native_aggregation(self):
		card = self._submit_native_pending(self._planned_card(6), 3)
		self.assertFalse(card.custom_worker_reporting_enabled)
		data = card.get_current_operation_data()[0]
		self.assertEqual((data.completed_qty, data.process_loss_qty, data.pending_qty), (3, 0, 3))
		self.assertEqual(frappe.db.get_value("Work Order Operation", card.operation_id, "pending_qty"), 3)

	def test_continuation_pending_keeps_independent_native_batch(self):
		from erpnext.manufacturing.doctype.work_order.work_order import create_job_card
		from process_simplification.api.production_plan_adapter import _allow_generated_job_cards_for

		card = self._planned_card(10)
		card.for_quantity = 6
		card.save()
		self._fund_and_issue(card)
		with self.set_user(self.supervisor):
			assignment = service.assign_worker(card.name, self.worker)
		self._approve(self._submit(assignment, 2))
		first = self._carry(card, "mixed-first-batch")
		target = frappe.get_doc("Job Card", first["job_card"])
		carried = frappe.get_doc("Job Card Worker Assignment", first["assignments"][0])
		self._approve(self._submit(carried, 2))
		self._carry(target, "mixed-second-batch")

		work_order = frappe.get_doc("Work Order", card.work_order)
		operation = frappe._dict(work_order.operations[0].as_dict(), job_card_qty=4)
		with _allow_generated_job_cards_for(card.work_order):
			independent = create_job_card(work_order, operation, auto_create=True)
		self._submit_native_pending(independent, 1)
		data = independent.get_current_operation_data()[0]
		self.assertEqual((data.completed_qty, data.process_loss_qty, data.pending_qty), (5, 0, 5))
		self.assertEqual((card.reload().pending_qty, target.reload().pending_qty, independent.pending_qty), (4, 2, 3))
		self.assertEqual(frappe.db.get_value("Work Order Operation", card.operation_id, "pending_qty"), 5)

	def test_confirmed_previous_month_wages_remain_immutable_after_continuation(self):
		previous_month = add_months(get_first_day(nowdate()), -1)
		with self.freeze_time(datetime.combine(add_days(previous_month, 14), time(10, 15, 0, 123456))):
			card, _, report = self._flow()
		with self.set_user(self.wage_manager):
			name = summary.build_monthly_summaries(self.TEST_COMPANY, previous_month, self.worker)["summaries"][0]
			settled = summary.confirm_monthly_summary(name)
		before = report.reload().as_dict()
		result = self._carry(card)
		carried = frappe.get_doc("Job Card Worker Assignment", result["assignments"][0])
		new_report = self._submit(carried, 2)
		self._approve(new_report)
		self.assertEqual(report.reload().as_dict(), before)
		self.assertEqual(report.monthly_summary, settled.name)
		self.assertEqual([row.source_report for row in settled.reload().details], [report.name])
		self.assertIsNone(new_report.reload().monthly_summary)

	def test_native_non_half_remainder_is_carried_without_overproduction(self):
		card, _, _ = self._flow(qty=5, completed=2)
		result = self._carry(card)
		self.assertEqual(frappe.db.get_value("Job Card", result["job_card"], "for_quantity"), 3)
		self.assertEqual(frappe.db.get_value("Work Order", card.work_order, "qty"), 5)

	def test_new_session_uses_current_rate_while_original_approved_wage_stays_frozen(self):
		card, _, report = self._flow(rate=3)
		self.assertEqual((report.rate, report.wage_amount), (3, 6))
		before = report.as_dict()
		with self.set_user(self.wage_manager):
			rule = frappe.get_doc("Operation Wage Rate", report.wage_rate)
			rule.piecework_rate = 4
			rule.save()
		result = self._carry(card)
		carried = frappe.get_doc("Job Card Worker Assignment", result["assignments"][0])
		new_report = self._submit(carried, 2)
		self._approve(new_report)
		self.assertEqual((new_report.reload().rate, new_report.wage_amount), (4, 8))
		self.assertEqual(report.reload().as_dict(), before)
		self.assertEqual(report.wage_amount + new_report.wage_amount, 14)

	def test_successor_continuation_waits_for_the_next_predecessor_batch(self):
		operation_name = "Continuation successor " + frappe.generate_hash(length=8)
		frappe.get_doc(dict(doctype="Operation", name=operation_name,
			workstation=self.TEST_WORKSTATION)).insert()
		bom = frappe.copy_doc(frappe.get_doc("BOM", self.master_bom))
		bom.is_default = 0
		bom.operations[0].sequence_id = 1
		bom.append("operations", dict(operation=operation_name, workstation=self.TEST_WORKSTATION,
			sequence_id=2, time_in_mins=10))
		bom.insert().submit()
		original_bom = self.master_bom
		self.master_bom = bom.name
		try:
			created = self._planned_card(4)
		finally:
			self.master_bom = original_bom
		self._fund_and_issue(created)
		cards = frappe.get_all("Job Card", filters={"work_order": created.work_order},
			fields=["name", "sequence_id"], order_by="sequence_id")
		first, second = [frappe.get_doc("Job Card", row.name) for row in cards]
		for card in (first, second):
			if not frappe.db.exists("Operation Wage Rate", {"operation": card.operation, "company": card.company, "enabled": 1}):
				self._make_rate(card)
		with self.set_user(self.supervisor):
			first_assignment = service.assign_worker(first.name, self.worker)
		first_report = self._submit(first_assignment, 2)
		self._approve(first_report)
		first_carry = self._carry(first, "first-operation-batch")
		other_user = self._make_worker()
		other_employee = frappe.db.get_value("Employee", {"user_id": other_user}, "name")
		with self.set_user(self.supervisor):
			second_assignment = service.assign_worker(second.name, other_employee)
		second_report = self._submit_as(second_assignment, other_user, 2)
		self._approve(second_report)
		second_carry = self._carry(second, "second-operation-batch")
		waiting = frappe.get_doc("Job Card Worker Assignment", second_carry["assignments"][0])
		with self.set_user(other_user), self.assertRaises(frappe.ValidationError):
			service.start_work_session(waiting.name, "must-wait-for-next-batch")
		self.assertEqual(domain.material_reportable_qty(domain.job_card_values(waiting.job_card)), 0)
		first_remaining = frappe.get_doc("Job Card Worker Assignment", first_carry["assignments"][0])
		self._approve(self._submit(first_remaining, 2))
		self.assertEqual(domain.material_reportable_qty(domain.job_card_values(waiting.job_card)), 2)
		self._approve(self._submit_as(waiting, other_user, 2))
		self.assertEqual(frappe.db.get_value("Work Order Operation", first.operation_id, "completed_qty"), 4)
		self.assertEqual(frappe.db.get_value("Work Order Operation", second.operation_id, "completed_qty"), 4)


	def test_close_unstarted_continuation_preserves_history_and_removes_worker_tasks(self):
		from process_simplification.production_reporting.work_order import assert_no_managed_draft_job_cards
		from process_simplification.production_workflow.work_order_close import close_work_order

		card = self._planned_card(6)
		self._fund_and_issue(card)
		other_user = self._make_worker()
		other_worker = frappe.db.get_value("Employee", {"user_id": other_user}, "name")
		with self.set_user(self.supervisor):
			assignments = service.assign_workers(card.name, [
				{"employee": self.worker, "assigned_qty": 3},
				{"employee": other_worker, "assigned_qty": 3},
			])
		reports = []
		for user, employee in ((self.worker_user, self.worker), (other_user, other_worker)):
			assignment = next(row for row in assignments if row.employee == employee)
			report = self._submit_as(assignment, user, 1)
			self._approve(report)
			reports.append(report)
		result = self._carry(card.reload(), "close-unstarted-continuation")
		target = frappe.get_doc("Job Card", result["job_card"])
		carried = [frappe.get_doc("Job Card Worker Assignment", name) for name in result["assignments"]]
		with self.set_user(self.wage_manager):
			summaries = [summary.build_monthly_summaries(self.TEST_COMPANY, nowdate(), employee)["summaries"][0]
				for employee in (self.worker, other_worker)]
		protected = [(row.doctype, row.name, row.reload().as_dict()) for row in [card, *assignments, *reports, target]]
		protected += [("Job Card Assignment Movement", row.name, frappe.get_doc("Job Card Assignment Movement", row.name).as_dict())
			for row in frappe.get_all("Job Card Assignment Movement", filters={"job_card": card.name}, fields=["name"])]
		protected += [("Monthly Worker Wage Summary", name, frappe.get_doc("Monthly Worker Wage Summary", name).as_dict()) for name in summaries]
		carried_before = {row.name: row.as_dict() for row in carried}
		with self.set_user(self.supervisor):
			self.assertEqual(close_work_order(card.work_order, "Closed"), "Closed")
		for doctype, name, before in protected:
			self.assertEqual(frappe.get_doc(doctype, name).as_dict(), before)
		for row in carried:
			row.reload()
			self.assertEqual(row.status, "Cancelled")
			self.assertEqual({key: value for key, value in row.as_dict().items() if key not in {"status", "modified", "modified_by"}},
				{key: value for key, value in carried_before[row.name].items() if key not in {"status", "modified", "modified_by"}})
		order = frappe.get_doc("Work Order", card.work_order)
		self.assertEqual((order.status, order.qty, order.operations[0].completed_qty, order.operations[0].pending_qty), ("Closed", 6, 2, 4))
		for user in (self.worker_user, other_user):
			with self.set_user(user):
				self.assertFalse([row for row in service.get_worker_dashboard()["assignments"] if row["work_order"] == order.name])
		with self.set_user(self.worker_user), self.assertRaises(frappe.ValidationError):
			service.start_work_session(next(row.name for row in carried if row.employee == self.worker), "must-not-start-after-close")
		before_repeat = [row.reload().as_dict() for row in carried]
		with self.set_user(self.supervisor):
			self.assertEqual(close_work_order(order.name, "Closed"), "Closed")
		self.assertEqual([row.reload().as_dict() for row in carried], before_repeat)
		# Permanent close does not relax Finish, generic status updates, or Cancel.
		with self.assertRaises(frappe.ValidationError):
			assert_no_managed_draft_job_cards(order.name)
		with self.assertRaises(frappe.ValidationError):
			order.update_status("Closed")
		with self.assertRaises(frappe.ValidationError):
			order.cancel()
		for employee, report in zip((self.worker, other_worker), reports):
			self.assertIn(report.name, [row.name for row in summary._eligible_reports(self.TEST_COMPANY, employee, get_first_day(nowdate()))])

	def test_started_continuation_blocks_close_without_changing_any_protected_fact(self):
		from process_simplification.production_workflow.work_order_close import close_work_order

		card, source, approved = self._flow()
		result = self._carry(card, "close-started-continuation")
		target = frappe.get_doc("Job Card", result["job_card"])
		carried = frappe.get_doc("Job Card Worker Assignment", result["assignments"][0])
		with self.set_user(self.worker_user):
			active = service.start_work_session(carried.name, "start-before-close")
		documents = [frappe.get_doc("Work Order", card.work_order), card, source, approved, target, carried, active]
		before = [(row.doctype, row.name, row.reload().as_dict()) for row in documents]
		with self.set_user(self.supervisor), self.assertRaises(frappe.ValidationError):
			close_work_order(card.work_order, "Closed")
		for doctype, name, snapshot in before:
			self.assertEqual(frappe.get_doc(doctype, name).as_dict(), snapshot)

	def test_native_time_log_without_report_blocks_unstarted_continuation_close(self):
		from process_simplification.production_workflow.work_order_close import close_work_order

		card, _, _ = self._flow()
		result = self._carry(card, "close-native-time-log")
		frappe.get_doc(dict(doctype="Job Card Time Log", parent=result["job_card"],
			parenttype="Job Card", parentfield="time_logs", employee=self.worker,
			from_time=get_datetime(), to_time=get_datetime() + timedelta(minutes=1),
			time_in_mins=1, completed_qty=0)).insert(ignore_permissions=True)
		before = frappe.get_doc("Work Order", card.work_order).as_dict()
		with self.set_user(self.supervisor), self.assertRaises(frappe.ValidationError):
			close_work_order(card.work_order, "Closed")
		self.assertEqual(frappe.get_doc("Work Order", card.work_order).as_dict(), before)

	def test_close_failure_after_retiring_continuation_rolls_back_history_and_status(self):
		from process_simplification.production_workflow import work_order_close as close

		card, source, approved = self._flow()
		result = self._carry(card, "close-rollback-continuation")
		carried = frappe.get_doc("Job Card Worker Assignment", result["assignments"][0])
		documents = [frappe.get_doc("Work Order", card.work_order), card, source, approved,
			frappe.get_doc("Job Card", result["job_card"]), carried]
		before = [(row.doctype, row.name, row.reload().as_dict()) for row in documents]
		retire = close.cancel_unstarted_continuation_assignments
		def fail_after_retire(*args, **kwargs):
			retire(*args, **kwargs)
			raise RuntimeError("after continuation retirement")
		# Model the HTTP request rollback; the service must not commit partial close state.
		frappe.db.savepoint("continuation_close_failure")
		try:
			with self.set_user(self.supervisor), patch.object(close, "cancel_unstarted_continuation_assignments", side_effect=fail_after_retire), self.assertRaisesRegex(RuntimeError, "after continuation"):
				close.close_work_order(card.work_order, "Closed")
		finally:
			frappe.db.rollback(save_point="continuation_close_failure")
		for doctype, name, snapshot in before:
			self.assertEqual(frappe.get_doc(doctype, name).as_dict(), snapshot)


	def test_reserved_partial_continuation_close_keeps_closed_and_cancels_real_reservation(self):
		from process_simplification.production_workflow.work_order_close import close_work_order

		# Submit with reserve_stock enabled and real stock, as in the factory close path.
		fixtures.make_stock_entry(item_code=self.TEST_RAW_MATERIAL, to_warehouse=self.source_warehouse,
			company=self.TEST_COMPANY, qty=6, basic_rate=1,
			posting_date=add_days(nowdate(), -1), posting_time="12:00:00")
		order = frappe.get_doc(dict(doctype="Work Order", company=self.TEST_COMPANY,
			production_item=self.TEST_FINISHED_GOOD, bom_no=self.master_bom, qty=6, stock_uom="Nos",
			source_warehouse=self.source_warehouse, wip_warehouse=self.wip_warehouse,
			fg_warehouse=self.fg_warehouse, reserve_stock=1, transfer_material_against="Work Order",
			planned_start_date=get_datetime()))
		order.get_items_and_operations_from_bom()
		for row in order.required_items:
			row.source_warehouse = self.source_warehouse
		order.insert().submit()
		reservations = frappe.get_all("Stock Reservation Entry", filters=dict(
			voucher_type="Work Order", voucher_no=order.name, docstatus=1), pluck="name")
		self.assertEqual(len(reservations), 1)
		reservation = frappe.get_doc("Stock Reservation Entry", reservations[0])
		self.assertEqual(reservation.reserved_qty, 6)
		card = frappe.get_doc("Job Card", {"work_order": order.name})
		if not frappe.db.exists("Operation Wage Rate", {"company": card.company, "operation": card.operation, "enabled": 1}):
			self._make_rate(card)
		entry = frappe.get_doc(fixtures.make_order_stock_entry(order.name, "Material Transfer for Manufacture", qty=2))
		entry.insert().submit()
		self.assertEqual(reservation.reload().transferred_qty, 2)
		with self.set_user(self.supervisor):
			assignment = service.assign_worker(card.name, self.worker)
		report = self._submit(assignment, 2)
		self._approve(report)
		result = self._carry(card.reload(), "close-reserved-partial-continuation")
		self.assertEqual(order.reload().operations[0].status, "Work in Progress")
		report_before = report.reload().as_dict()
		ledger_before = frappe.get_all("Stock Ledger Entry", filters={"voucher_no": entry.name}, fields=["*"], order_by="name")
		with self.set_user(self.supervisor):
			self.assertEqual(close_work_order(order.name, "Closed"), "Closed")
		self.assertEqual(order.reload().status, "Closed")
		self.assertEqual(order.get_status(), "Closed")
		self.assertEqual(reservation.reload().docstatus, 2)
		self.assertEqual(frappe.get_doc("Job Card Worker Assignment", result["assignments"][0]).status, "Cancelled")
		self.assertEqual(report.reload().as_dict(), report_before)
		self.assertEqual(frappe.get_all("Stock Ledger Entry", filters={"voucher_no": entry.name}, fields=["*"], order_by="name"), ledger_before)
