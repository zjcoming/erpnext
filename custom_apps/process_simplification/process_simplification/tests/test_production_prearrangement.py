"""Native stock/reporting integration for inert plans and explicit activation."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate, random_string

from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from process_simplification.production_reporting import prearrangement as plans, service
from process_simplification.tests import test_operation_dispatch_pool_integration as fixtures


class TestProductionPrearrangement(IntegrationTestCase):
	TEST_COMPANY = "Prearrangement Test Company"
	TEST_COMPANY_ABBR = "PAT"
	OTHER_COMPANY = "Prearrangement Other Company"
	OTHER_COMPANY_ABBR = "PAO"
	TEST_OPERATION = "Prearrangement Test Operation"
	TEST_WORKSTATION = "Prearrangement Test Workstation"
	TEST_FINISHED_GOOD = "PAT-FG-001"
	TEST_RAW_MATERIAL = "PAT-RM-001"

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
	_business_snapshot = fixtures.TestOperationDispatchPoolIntegration._business_snapshot

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._ensure_master_fixtures()

	def setUp(self):
		super().setUp()
		frappe.local.db = self._primary_connection
		frappe.set_user("Administrator")
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

	def _plan(self, card, **overrides):
		args = {"job_card": card.name, "allocations": [{"employee": self.worker, "assigned_qty": card.for_quantity}],
			"expected_qty": card.for_quantity, "expected_work_order": card.work_order,
			"expected_operation_id": card.operation_id}
		args.update(overrides)
		with self.set_user(self.supervisor):
			return plans.save_plan(**args)

	def test_unissued_plan_is_saved_without_stock_assignment_or_wage_effect(self):
		card = self._planned_card()
		before = self._business_snapshot(card)
		plan = self._plan(card)
		self.assertEqual(plan["revision"], 1)
		self.assertEqual(plan["status"], "Planned")
		self.assertEqual(self._business_snapshot(card), before)
		with self.set_user(self.worker_user):
			with self.assertQueryCount(0, query_type=("insert", "update", "delete")):
				task = plans.get_my_planned_tasks()["tasks"][0]
			self.assertFalse(task["can_start"])
			self.assertEqual(task["block_code"], "MATERIAL_NOT_FULLY_ISSUED")
			with self.assertRaises(frappe.ValidationError):
				plans.start_planned_task(plan["name"], "waiting", 1)
		self.assertEqual(self._business_snapshot(card), before)

	def test_waiting_predecessor_can_be_planned_but_cannot_start(self):
		operation = frappe.get_doc({"doctype": "Operation", "name": "Prearrangement Second " + random_string(8)}).insert()
		bom = frappe.copy_doc(frappe.get_doc("BOM", self.master_bom))
		bom.is_default = 0
		bom.operations[0].sequence_id = 1
		bom.append("operations", {"operation": operation.name, "workstation": self.TEST_WORKSTATION,
			"sequence_id": 2, "time_in_mins": 10, "operating_cost": 1})
		bom.insert().submit()
		with patch.object(self, "master_bom", bom.name):
			first = self._planned_card()
		card = frappe.get_doc("Job Card", {"work_order": first.work_order, "sequence_id": 2})
		if not frappe.db.exists("Operation Wage Rate", {"company": card.company, "operation": card.operation, "enabled": 1}):
			self._make_rate(card)
		self._fund_and_issue(card)
		plan = self._plan(card)
		with self.set_user(self.worker_user):
			self.assertEqual(plans.get_my_planned_tasks()["tasks"][0]["block_code"], "PREVIOUS_OPERATION_PENDING")
			with self.assertRaisesRegex(frappe.ValidationError, "前序"):
				plans.start_planned_task(plan["name"], "previous", 1)
		self.assertFalse(frappe.db.exists("Job Card Worker Assignment", {"job_card": card.name}))

	def test_worker_start_atomically_activates_once_and_retries_same_report(self):
		card = self._planned_card()
		plan = self._plan(card)
		self._fund_and_issue(card)
		with self.set_user(self.worker_user):
			self.assertTrue(plans.get_my_planned_tasks()["tasks"][0]["can_start"])
			first = plans.start_planned_task(plan["name"], "start-once", 1)
			second = plans.start_planned_task(plan["name"], "start-once", 1)
			self.assertEqual(first["report"].name, second["report"].name)
			self.assertEqual(frappe.session.user, self.worker_user)
			self.assertEqual(plans.get_my_planned_tasks()["tasks"], [])
		assignment = frappe.get_doc("Job Card Worker Assignment", first["assignment"])
		self.assertEqual(assignment.assigned_by, self.supervisor)
		self.assertEqual(frappe.db.get_value(plans.PLAN, plan["name"], "activated_by"), self.worker_user)
		self.assertEqual(frappe.db.count("Job Card Worker Assignment", {"job_card": card.name}), 1)
		self.assertEqual(frappe.db.count("Job Card Work Report", {"job_card": card.name}), 1)

	def test_start_failure_rolls_back_formal_assignments_and_restores_identity(self):
		card = self._planned_card(skip_transfer=True)
		# Keep fixture stock before the action's live cutoff, even if the test
		# host's wall clock adjusts while the receipt is being posted.
		make_stock_entry(item_code=self.TEST_RAW_MATERIAL, to_warehouse=self.source_warehouse,
			company=self.TEST_COMPANY, qty=card.for_quantity, basic_rate=1,
			posting_date=add_days(nowdate(), -1), posting_time="12:00:00")
		plan = self._plan(card)
		with self.set_user(self.worker_user):
			task = plans.get_my_planned_tasks()["tasks"][0]
			self.assertTrue(task["can_start"], task.get("block_message"))
		before = self._business_snapshot(card)
		frappe.db.savepoint("prearrangement_failed_start")
		with self.set_user(self.worker_user), patch.object(service, "start_work_session", side_effect=frappe.ValidationError("session failure")):
			with self.assertRaisesRegex(frappe.ValidationError, "session failure"):
				plans.start_planned_task(plan["name"], "failing", 1)
			self.assertEqual(frappe.session.user, self.worker_user)
		# This is the owning RPC's rollback contract. Internal orchestration must
		# propagate errors rather than catch them and commit partially activated work.
		frappe.db.rollback(save_point="prearrangement_failed_start")
		self.assertEqual(self._business_snapshot(card), before)
		self.assertEqual(frappe.db.get_value(plans.PLAN, plan["name"], "status"), "Planned")

	def test_direct_consumption_reserves_only_when_worker_starts(self):
		card = self._planned_card(skip_transfer=True)
		make_stock_entry(item_code=self.TEST_RAW_MATERIAL, to_warehouse=self.source_warehouse,
			company=self.TEST_COMPANY, qty=card.for_quantity, basic_rate=1,
			posting_date=add_days(nowdate(), -1), posting_time="12:00:00")
		before = self._business_snapshot(card)
		plan = self._plan(card)
		self.assertEqual(self._business_snapshot(card), before)
		with self.set_user(self.worker_user):
			with self.assertQueryCount(0, query_type=("insert", "update", "delete")):
				task = plans.get_my_planned_tasks()["tasks"][0]
			self.assertTrue(task["can_start"], task.get("block_message"))
			self.assertEqual(frappe.session.user, self.worker_user)
			self.assertEqual(self._business_snapshot(card), before)
			plans.start_planned_task(plan["name"], "direct", 1)
		self.assertGreater(self._business_snapshot(card)["reservations"], before["reservations"])

	def test_stale_revision_rejects_edit_cancel_and_worker_activation(self):
		card = self._planned_card()
		plan = self._plan(card)
		updated = self._plan(card, expected_revision=1)
		self.assertEqual(updated["revision"], 2)
		with self.assertRaisesRegex(frappe.ValidationError, "修改"):
			self._plan(card, expected_revision=1)
		with self.set_user(self.supervisor), self.assertRaisesRegex(frappe.ValidationError, "修改"):
			plans.cancel_plan(card.name, 1)
		with self.set_user(self.worker_user), self.assertRaisesRegex(frappe.ValidationError, "修改"):
			plans.start_planned_task(plan["name"], "stale", 1)
		self.assertFalse(frappe.db.exists("Job Card Worker Assignment", {"job_card": card.name}))

	def test_cancel_and_replan_retains_monotonic_revision(self):
		card = self._planned_card()
		plan = self._plan(card)
		with self.set_user(self.supervisor):
			cancelled = plans.cancel_plan(card.name, 1)
			self.assertEqual(cancelled["revision"], 2)
			self.assertEqual(cancelled["status"], "Cancelled")
		with self.set_user(self.worker_user), self.assertRaises(frappe.ValidationError):
			plans.start_planned_task(plan["name"], "cancelled", 2)
		replanned = self._plan(card, expected_revision=2)
		self.assertEqual(replanned["revision"], 3)
		self.assertEqual(replanned["name"], plan["name"])

	def test_formal_assignment_made_elsewhere_cannot_be_overwritten(self):
		card = self._planned_card()
		plan = self._plan(card)
		self._fund_and_issue(card)
		with self.set_user(self.supervisor):
			service.assign_workers(card.name, [{"employee": self.worker, "assigned_qty": 10}])
		before = self._business_snapshot(card)
		with self.set_user(self.worker_user), self.assertRaisesRegex(frappe.ValidationError, "历史"):
			plans.start_planned_task(plan["name"], "old-assignment", 1)
		self.assertEqual(self._business_snapshot(card), before)

	def test_frozen_card_quantity_change_blocks_activation(self):
		card = self._planned_card()
		plan = self._plan(card)
		frappe.db.set_value("Job Card", card.name, "for_quantity", 11)
		with self.set_user(self.worker_user):
			task = plans.get_my_planned_tasks()["tasks"][0]
			self.assertEqual(task["block_code"], "PLAN_NEEDS_REVIEW")
			with self.assertRaisesRegex(frappe.ValidationError, "数量已变化"):
				plans.start_planned_task(plan["name"], "qty-changed", 1)

	def test_worker_identity_supervisor_status_and_company_scope_fail_closed(self):
		card = self._planned_card()
		plan = self._plan(card)
		outsider = self._make_worker()
		with self.set_user(outsider):
			self.assertEqual(plans.get_my_planned_tasks()["tasks"], [])
			with self.assertRaises(frappe.PermissionError):
				plans.start_planned_task(plan["name"], "outsider", 1)
			with self.assertRaises(frappe.PermissionError):
				plans.get_plan_context([card.name])
		foreign = self._make_user("Process Simplification Production Manager")
		self._grant_company(foreign, self.OTHER_COMPANY)
		with self.set_user(foreign), self.assertRaises(frappe.PermissionError):
			plans.get_plan_context([card.name])
		frappe.db.set_value("User", self.supervisor, "enabled", 0)
		with self.set_user(self.worker_user):
			self.assertFalse(plans.get_my_planned_tasks()["tasks"][0]["can_start"])
			with self.assertRaisesRegex(frappe.ValidationError, "停用"):
				plans.start_planned_task(plan["name"], "supervisor-disabled", 1)

	def test_default_roster_is_explicit_and_does_not_rewrite_existing_plan(self):
		card = self._planned_card()
		plan = self._plan(card)
		other_user = self._make_worker()
		other_employee = frappe.db.get_value("Employee", {"user_id": other_user}, "name")
		with self.set_user(self.supervisor):
			default = plans.save_worker_defaults(card.company, card.operation, [self.worker])
			self.assertEqual(default["revision"], 1)
			plans.save_worker_defaults(card.company, card.operation, [other_employee], 1)
			context = plans.get_plan_context([card.name])["cards"][card.name]
			self.assertEqual(context["default_roster"]["workers"][0]["employee"], other_employee)
			self.assertEqual(context["plan"]["allocations"][0]["employee"], self.worker)
			self.assertEqual(context["plan"]["revision"], plan["revision"])

	def test_feature_off_keeps_plan_visible_and_cancellable_without_activation(self):
		card = self._planned_card()
		plan = self._plan(card)
		with patch.dict(frappe.conf, {"enable_operation_dispatch_pool": 0}):
			with self.set_user(self.worker_user):
				self.assertEqual(plans.get_my_planned_tasks()["tasks"][0]["block_code"], "FEATURE_DISABLED")
				with self.assertRaises(frappe.PermissionError):
					plans.start_planned_task(plan["name"], "disabled", 1)
			with self.set_user(self.supervisor):
				self.assertFalse(plans.get_plan_context([card.name])["cards"][card.name]["can_plan"])
				self.assertEqual(plans.cancel_plan(card.name, 1)["status"], "Cancelled")

	def test_direct_doctype_write_is_rejected_even_for_administrator(self):
		card = self._planned_card()
		plan = self._plan(card)
		doc = frappe.get_doc(plans.PLAN, plan["name"])
		doc.supervisor = "Administrator"
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)

	def test_nonfinite_and_unbalanced_quantities_are_rejected(self):
		card = self._planned_card()
		for value in ("nan", "inf", -1, 0, 11):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				self._plan(card, allocations=[{"employee": self.worker, "assigned_qty": value}])
		self.assertFalse(frappe.db.exists(plans.PLAN, {"job_card": card.name}))

	def test_delegation_restores_http_session_and_arguments_on_success_and_failure(self):
		with self.set_user(self.worker_user):
			frappe.session.sid = "actual-browser-session"
			frappe.session.data = frappe._dict(user=self.worker_user, csrf_token="request-local-test")
			frappe.local.form_dict = frappe._dict(cmd="process_simplification.api.production_prearrangement.start_planned_task", request_id="stable")
			original_data = frappe.session.data
			original_form = frappe.local.form_dict
			for fail in (False, True):
				try:
					with plans._as_persisted_supervisor(self.supervisor):
						self.assertEqual(frappe.session.user, self.supervisor)
						if fail:
							raise ValueError("expected")
				except ValueError:
					pass
				self.assertEqual(frappe.session.user, self.worker_user)
				self.assertEqual(frappe.session.sid, "actual-browser-session")
				self.assertIs(frappe.session.data, original_data)
				self.assertIs(frappe.local.form_dict, original_form)

	def test_second_task_explains_active_work_instead_of_showing_ready(self):
		first, second = self._planned_card(), self._planned_card()
		first_plan = self._plan(first)
		self._plan(second)
		self._fund_and_issue(first)
		self._fund_and_issue(second)
		with self.set_user(self.worker_user):
			plans.start_planned_task(first_plan["name"], "first-working", 1)
			task = plans.get_my_planned_tasks()["tasks"][0]
			self.assertEqual(task["job_card"], second.name)
			self.assertEqual(task["block_code"], "ACTIVE_WORK_SESSION")
			self.assertFalse(task["can_start"])
