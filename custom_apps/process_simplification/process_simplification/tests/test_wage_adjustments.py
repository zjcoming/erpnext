"""Wage-only fixtures on an isolated test site; not historical demo evidence.

The two settlement months below are synthetic, already-ended natural months.
These tests do not change the clock or create production/reporting facts.
"""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextvars import Context
from decimal import Decimal
from threading import Event
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_months, get_first_day, nowdate, random_string
from werkzeug.wrappers import Request

from process_simplification.api import production_reporting as reporting_api
from process_simplification.management_access import OWNER_ROLE
from process_simplification.production_reporting import summary, wage_adjustments
from process_simplification.tests import test_worker_reporting as fixture_helpers


class TestWageAdjustments(IntegrationTestCase):
	TEST_COMPANY = "Wage Adjustment Test Company"
	OTHER_COMPANY = "Wage Adjustment Other Company"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# A fresh app-only test site has not run Setup Wizard's standard seeds.
		if not frappe.db.exists("Warehouse Type", "Transit"):
			frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Gender", "Female"):
			frappe.get_doc({"doctype": "Gender", "gender": "Female"}).insert(ignore_permissions=True)
		fixture_helpers.TestWorkerReporting._ensure_company(cls.TEST_COMPANY, "WAT")
		fixture_helpers.TestWorkerReporting._ensure_company(cls.OTHER_COMPANY, "WAO")
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.local.db = self._primary_connection
		frappe.set_user("Administrator")
		# Reuse small fixture helpers only; never inherit or run the large suite's setup.
		self.fixtures = fixture_helpers.TestWorkerReporting()
		self.fixtures.TEST_COMPANY = self.TEST_COMPANY
		self.owner = self.fixtures._make_user(OWNER_ROLE)
		self.fixtures._grant_company(self.owner, self.TEST_COMPANY)
		self.worker = self.fixtures._make_worker()
		self.employee = frappe.db.get_value("Employee", {"user_id": self.worker}, "name")
		self.first_month = get_first_day(add_months(nowdate(), -2))
		self.second_month = get_first_day(add_months(nowdate(), -1))

	def tearDown(self):
		try:
			frappe.local.db = self._primary_connection
			frappe.set_user("Administrator")
			frappe.db.rollback()
		finally:
			super().tearDown()

	def _record(self, kind, amount, *, month=None, request_id=None, reason="Synthetic wage correction"):
		with self.set_user(self.owner):
			return wage_adjustments.record_wage_adjustment(
				self.TEST_COMPANY,
				self.employee,
				month or self.first_month,
				kind,
				amount,
				reason,
				request_id=request_id or "wage-test-" + random_string(20),
			)

	def _build(self, month=None, *, all_employees=False):
		with self.set_user(self.owner):
			result = summary.build_monthly_summaries(
				self.TEST_COMPANY,
				month or self.first_month,
				None if all_employees else self.employee,
			)
		self.assertEqual(len(result["summaries"]), 1)
		return frappe.get_doc("Monthly Worker Wage Summary", result["summaries"][0])

	def _confirm(self, doc):
		with self.set_user(self.owner):
			confirmed = summary.confirm_monthly_summary(doc.name)
		return confirmed.reload()

	def _balance(self, record):
		with self.set_user(self.owner):
			return wage_adjustments.get_wage_adjustment_balance(record["name"])

	def _assert_money(self, actual, expected):
		self.assertEqual(Decimal(str(actual)).quantize(Decimal("0.01")), Decimal(str(expected)))

	def _assert_no_production_reports(self):
		self.assertEqual(frappe.db.count("Job Card Work Report", {"employee": self.employee}), 0)

	def test_allocator_credits_before_deductions_and_never_negative(self):
		plan = wage_adjustments.plan_applications(
			40,
			[
				{"name": "DEBIT", "type": "Deduction", "remaining_amount": 100},
				{"name": "CREDIT", "type": "Supplement", "remaining_amount": 20},
			],
		)
		self._assert_money(plan["supplement_amount"], 20)
		self._assert_money(plan["deduction_amount"], 60)
		self._assert_money(plan["pending_deduction_amount"], 40)
		self._assert_money(plan["total_amount"], 0)
		self.assertEqual([row["wage_adjustment"] for row in plan["applications"]], ["CREDIT", "DEBIT"])

	def test_drafts_do_not_consume_and_two_confirmed_months_consume_once(self):
		credit = self._record("Supplement", 40)
		debit = self._record("Deduction", 100)
		first = self._build(all_employees=True)
		self.assertEqual(first.docstatus, 0)
		self.assertFalse(first.details)
		self._assert_money(first.total_amount, 0)
		self._assert_money(first.deduction_amount, 40)
		self._assert_money(first.pending_deduction_amount, 60)
		for _ in range(2):
			self.assertEqual(self._build().name, first.name)
			self._assert_money(self._balance(credit)["applied_amount"], 0)
			self._assert_money(self._balance(debit)["remaining_amount"], 100)
		first = self._confirm(first)
		self.assertEqual(first.docstatus, 1)
		self._assert_money(self._balance(credit)["remaining_amount"], 0)
		self._assert_money(self._balance(debit)["applied_amount"], 40)
		self._assert_money(self._balance(debit)["remaining_amount"], 60)
		self.assertEqual(self._confirm(first).name, first.name)
		self._assert_money(self._balance(debit)["applied_amount"], 40)
		second_credit = self._record("Supplement", 70, month=self.second_month)
		second = self._build(self.second_month)
		self._assert_money(second.supplement_amount, 70)
		self._assert_money(second.deduction_amount, 60)
		self._assert_money(second.total_amount, 10)
		self._assert_money(second.pending_deduction_amount, 0)
		self._assert_money(self._balance(second_credit)["applied_amount"], 0)
		self._confirm(second)
		self._assert_money(self._balance(second_credit)["remaining_amount"], 0)
		self._assert_money(self._balance(debit)["applied_amount"], 100)
		self._assert_money(self._balance(debit)["remaining_amount"], 0)
		self._assert_no_production_reports()

	def test_later_draft_must_wait_for_earlier_month_and_rebuild_stale_plan(self):
		self._record("Supplement", 40)
		debit = self._record("Deduction", 100)
		self._record("Supplement", 70, month=self.second_month)
		first = self._build()
		second = self._build(self.second_month)
		self._assert_money(second.deduction_amount, 100)
		with self.set_user(self.owner), self.assertRaises(frappe.ValidationError):
			summary.confirm_monthly_summary(second.name)
		self.assertEqual(second.reload().docstatus, 0)
		self._assert_money(self._balance(debit)["applied_amount"], 0)
		self._confirm(first)
		with self.set_user(self.owner), self.assertRaises(frappe.ValidationError):
			summary.confirm_monthly_summary(second.name)
		self.assertEqual(second.reload().docstatus, 0)
		second = self._build(self.second_month)
		self._assert_money(second.deduction_amount, 60)
		self._assert_money(second.total_amount, 10)
		self._confirm(second)
		self._assert_money(self._balance(debit)["remaining_amount"], 0)

	def test_record_request_id_replay_is_stable_and_payload_conflict_is_rejected(self):
		request_id = "wage-idempotent-" + random_string(20)
		first = self._record("Supplement", 40, request_id=request_id)
		self.assertEqual(self._record("Supplement", "40.00", request_id=request_id)["name"], first["name"])
		self.assertEqual(frappe.db.count("Worker Wage Adjustment", {"request_id": request_id}), 1)
		with self.assertRaises(frappe.ValidationError):
			self._record("Supplement", 41, request_id=request_id)
		with self.assertRaises(frappe.ValidationError):
			self._record("Supplement", 40, request_id=request_id, reason="Different correction")
		self._assert_money(self._balance(first)["remaining_amount"], 40)

	def test_cancel_unused_record_clears_existing_draft_without_consuming(self):
		record = self._record("Supplement", 40)
		draft = self._build()
		self._assert_money(draft.total_amount, 40)
		with self.set_user(self.owner):
			self.assertEqual(wage_adjustments.cancel_wage_adjustment(record["name"])["docstatus"], 2)
		cleared = self._build(all_employees=True)
		self.assertEqual(cleared.name, draft.name)
		self.assertEqual(cleared.docstatus, 0)
		self.assertFalse(cleared.adjustments)
		self._assert_money(cleared.total_amount, 0)
		self._assert_money(cleared.pending_deduction_amount, 0)
		with self.set_user(self.owner), self.assertRaises(frappe.ValidationError):
			summary.confirm_monthly_summary(cleared.name)
		self._assert_money(self._balance(record)["applied_amount"], 0)

	def test_consumed_adjustment_cannot_cancel_and_confirmed_summary_stays_final(self):
		record = self._record("Supplement", 40)
		confirmed = self._confirm(self._build())
		with self.set_user(self.owner), self.assertRaises(frappe.ValidationError):
			wage_adjustments.cancel_wage_adjustment(record["name"])
		with self.set_user("Administrator"), self.assertRaises(frappe.ValidationError):
			confirmed.cancel()
		self.assertEqual(confirmed.reload().docstatus, 1)
		self._assert_money(self._balance(record)["applied_amount"], 40)
		with self.assertRaises(frappe.ValidationError):
			self._record("Deduction", 5)

	def test_owner_company_scope_worker_denial_and_wage_manager_read_only(self):
		record = self._record("Supplement", 40)
		for user in (self.worker, self.fixtures._make_user("Production Wage Manager")):
			with self.set_user(user), self.assertRaises(frappe.PermissionError):
				wage_adjustments.record_wage_adjustment(
					self.TEST_COMPANY,
					self.employee,
					self.first_month,
					"Supplement",
					40,
					"Unauthorized duplicate",
					request_id="denied-" + random_string(20),
				)
		manager = user
		self.fixtures._grant_company(manager, self.TEST_COMPANY)
		with self.set_user(manager):
			self._assert_money(
				wage_adjustments.get_wage_adjustment_balance(record["name"])["remaining_amount"], 40
			)
			with self.assertRaises(frappe.PermissionError):
				wage_adjustments.cancel_wage_adjustment(record["name"])
		outsider = self.fixtures._make_user(OWNER_ROLE)
		self.fixtures._grant_company(outsider, self.OTHER_COMPANY)
		with self.set_user(outsider):
			with self.assertRaises(frappe.PermissionError):
				wage_adjustments.get_wage_adjustment_balance(record["name"])
			with self.assertRaises(frappe.PermissionError):
				wage_adjustments.record_wage_adjustment(
					self.TEST_COMPANY,
					self.employee,
					self.first_month,
					"Supplement",
					40,
					"Cross-company replay",
					request_id=frappe.db.get_value("Worker Wage Adjustment", record["name"], "request_id"),
				)
			self.assertFalse(
				frappe.get_list("Worker Wage Adjustment", filters={"name": record["name"]}, pluck="name")
			)
		self.assertEqual(frappe.db.count("Worker Wage Adjustment", {"employee": self.employee}), 1)

	def test_wage_employee_picker_finds_adjustment_only_employee_and_preserves_scope(self):
		self._record("Supplement", 40)
		self._assert_no_production_reports()
		employee_name = frappe.db.get_value("Employee", self.employee, "employee_name")
		other_fixtures = fixture_helpers.TestWorkerReporting()
		other_fixtures.TEST_COMPANY = self.OTHER_COMPANY
		other_worker = other_fixtures._make_worker()
		other_employee, other_name = frappe.db.get_value(
			"Employee", {"user_id": other_worker}, ["name", "employee_name"]
		)

		def search(company, text):
			return reporting_api.search_wage_employees(
				doctype="Employee",
				txt=text,
				searchfield="name",
				start=0,
				page_len=20,
				filters=frappe.as_json({"company": company}),
			)

		with self.set_user(self.owner):
			for text in (self.employee, employee_name):
				with self.subTest(search=text):
					rows = search(self.TEST_COMPANY, text)
					self.assertIn((self.employee, employee_name, self.worker), [tuple(row) for row in rows])
			for text in (other_employee, other_name):
				with self.subTest(other_company_search=text):
					self.assertFalse(search(self.TEST_COMPANY, text))
			with self.assertRaises(frappe.PermissionError):
				search(self.OTHER_COMPANY, other_employee)
		with self.set_user(self.worker), self.assertRaises(frappe.PermissionError):
			search(self.TEST_COMPANY, self.employee)
		self._assert_no_production_reports()

	def test_waiting_old_month_draft_sees_committed_consumption_and_requires_rebuild(self):
		"""Only this marked fixture is committed and retained on the isolated test site."""
		reason = "COMMITTED CONCURRENCY TEST FIXTURE; not factory wages"
		first_credit = self._record("Supplement", 40, reason=reason)
		debit = self._record("Deduction", 100, reason=reason)
		second_credit = self._record("Supplement", 70, month=self.second_month, reason=reason)
		first = self._build()
		second = self._build(self.second_month)
		self._assert_money(second.supplement_amount, 110)
		self._assert_money(second.deduction_amount, 100)
		site, owner = frappe.local.site, self.owner
		first_name, second_name, employee = first.name, second.name, self.employee
		frappe.db.commit()
		print(
			"WAGE_ADJUSTMENT_RETAINED_TEST_FIXTURE",
			{
				"site": site,
				"employee": employee,
				"owner": owner,
				"worker": self.worker,
				"summaries": [first_name, second_name],
				"adjustments": [first_credit["name"], debit["name"], second_credit["name"]],
				"reason": "isolated committed cross-month lock test",
			},
		)
		stale_read, attempts_employee_lock = Event(), Event()
		stale_rejected, allow_rebuild = Event(), Event()
		primary_id = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]

		def waiter():
			frappe.init(site=site)
			frappe.connect()
			frappe.flags.in_test = True
			frappe.set_user(owner)
			# Invoke the real transaction-owning RPC decorator without sending HTTP.
			frappe.local.request = Request.from_values(method="POST")
			frappe.local.form_dict = frappe._dict(
				cmd="process_simplification.api.production_reporting.confirm_monthly_summary"
			)
			database = frappe.local.db
			connection_id = database.sql("SELECT CONNECTION_ID()")[0][0]
			original_sql = database.sql
			original_rollback = database.rollback
			recovery = {"query_deadlocks": 0, "wrapper_rollbacks": 0, "native_error_codes": []}
			try:
				old = frappe.get_doc("Monthly Worker Wage Summary", second_name)
				if old.docstatus != 0:
					raise AssertionError(
						"Waiter must establish a real Draft snapshot before the winner commits"
					)
				stale_read.set()

				def observe_sql(query, *args, **kwargs):
					sql = " ".join(str(query).lower().split())
					if "tabemployee" in sql and "for update" in sql:
						attempts_employee_lock.set()
					try:
						return original_sql(query, *args, **kwargs)
					except frappe.QueryDeadlockError as error:
						recovery["query_deadlocks"] += 1
						cause = error.__cause__
						if cause and cause.args and isinstance(cause.args[0], int):
							recovery["native_error_codes"].append(cause.args[0])
						raise

				def observe_rollback(*args, **kwargs):
					recovery["wrapper_rollbacks"] += 1
					return original_rollback(*args, **kwargs)

				try:
					with (
						patch.object(database, "sql", side_effect=observe_sql),
						patch.object(database, "rollback", side_effect=observe_rollback),
					):
						reporting_api.confirm_monthly_summary(second_name)
				except frappe.ValidationError as error:
					# This cleanup is outside the observer: count only decorator rollbacks.
					database.rollback()
					result = {
						"connection": connection_id,
						"rejected": True,
						"requires_rebuild": "rebuild" in str(error).lower(),
						**recovery,
					}
					stale_rejected.set()
					if not allow_rebuild.wait(timeout=10):
						raise AssertionError("Controller did not release the explicit rebuild step")
					# A distinct user recovery step, not a test-only retry loop: rebuild
					# the rejected draft and then confirm the newly saved plan.
					built = summary.build_monthly_summaries(self.TEST_COMPANY, self.second_month, employee)
					if built["summaries"] != [second_name]:
						raise AssertionError("Rebuild must preserve the rejected monthly summary identity")
					rebuilt = frappe.get_doc("Monthly Worker Wage Summary", second_name)
					result["rebuilt_plan"] = [
						float(rebuilt.supplement_amount),
						float(rebuilt.deduction_amount),
						float(rebuilt.total_amount),
					]
					database.commit()
					result["docstatus"] = reporting_api.confirm_monthly_summary(second_name).docstatus
					database.commit()
					result["committed"] = True
					return result
				else:
					raise AssertionError("The waiting old draft was accepted without a rebuild")
			finally:
				database.rollback()
				frappe.destroy()

		with self.set_user(owner):
			wage_adjustments._lock_employee(self.TEST_COMPANY, employee)
			with ThreadPoolExecutor(max_workers=1) as executor:
				future = executor.submit(Context().run, waiter)
				try:
					self.assertTrue(
						stale_read.wait(timeout=10), "Waiter did not establish its stale snapshot"
					)
					self.assertTrue(
						attempts_employee_lock.wait(timeout=10), "Waiter did not attempt the Employee lock"
					)
					with self.assertRaises(FutureTimeoutError):
						future.result(timeout=0.25)
					winner = summary.confirm_monthly_summary(first_name)
					self.assertEqual(winner.docstatus, 1)
					frappe.db.commit()
					if not stale_rejected.wait(timeout=10):
						future.result(timeout=1)
						self.fail("Waiting old draft did not reject after the winner committed")
					self.assertEqual(
						frappe.db.get_value("Monthly Worker Wage Summary", second_name, "docstatus"), 0
					)
					self._assert_money(self._balance(debit)["applied_amount"], 40)
					self._assert_money(self._balance(second_credit)["applied_amount"], 0)
					frappe.db.rollback()
					allow_rebuild.set()
					waiting_result = future.result(timeout=15)
				finally:
					# Release our lock even on an assertion failure before waiting for thread shutdown.
					frappe.db.rollback()
					allow_rebuild.set()
		self.assertNotEqual(primary_id, waiting_result["connection"])
		self.assertTrue(waiting_result["rejected"])
		self.assertTrue(waiting_result["requires_rebuild"])
		self.assertGreaterEqual(waiting_result["query_deadlocks"], 1)
		self.assertEqual(waiting_result["wrapper_rollbacks"], waiting_result["query_deadlocks"])
		self.assertTrue(waiting_result["committed"])
		self.assertEqual(waiting_result["docstatus"], 1)
		self.assertEqual(waiting_result["rebuilt_plan"], [70.0, 60.0, 10.0])
		print("WAGE_ADJUSTMENT_RPC_CONFLICT_RECOVERY", waiting_result)
		self.assertEqual(second.reload().docstatus, 1)
		self._assert_money(self._balance(debit)["applied_amount"], 100)
		self._assert_money(self._balance(debit)["remaining_amount"], 0)
		for record, amount in ((first_credit, 40), (second_credit, 70)):
			balance = self._balance(record)
			self._assert_money(balance["applied_amount"], amount)
			self._assert_money(balance["remaining_amount"], 0)
			self.assertEqual(len([row for row in balance["applications"] if row["docstatus"] == 1]), 1)
		self.assertEqual(
			[float(row["amount"]) for row in self._balance(debit)["applications"] if row["docstatus"] == 1],
			[40.0, 60.0],
		)
		self._assert_no_production_reports()

	def test_submit_failure_rolls_back_confirmed_application_then_retry_consumes_once(self):
		record = self._record("Supplement", 40)
		draft = self._build()
		original_on_submit = summary.on_submit_summary
		reached_confirmed_writes = []

		def fail_after_writes(doc):
			original_on_submit(doc)
			self.assertEqual(frappe.db.get_value(doc.doctype, doc.name, "docstatus"), 1)
			self._assert_money(
				wage_adjustments.get_wage_adjustment_balance(record["name"])["applied_amount"], 40
			)
			reached_confirmed_writes.append(True)
			raise RuntimeError("synthetic failure after confirmed application writes")

		point = "wage_adjustment_submit_failure"
		frappe.db.savepoint(point)
		try:
			with (
				self.set_user(self.owner),
				patch.object(summary, "on_submit_summary", side_effect=fail_after_writes),
			):
				with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
					summary.confirm_monthly_summary(draft.name)
		finally:
			frappe.db.rollback(save_point=point)
		self.assertEqual(reached_confirmed_writes, [True])
		self.assertEqual(draft.reload().docstatus, 0)
		self.assertFalse(draft.confirmed_by)
		self._assert_money(self._balance(record)["applied_amount"], 0)
		self._assert_money(self._balance(record)["remaining_amount"], 40)
		self._confirm(draft)
		self._assert_money(self._balance(record)["applied_amount"], 40)
		self._assert_money(self._balance(record)["remaining_amount"], 0)

	def test_spent_history_does_not_block_old_base_validation_but_current_month_cannot_confirm(self):
		record = self._record("Supplement", 40)
		self._confirm(self._build(self.second_month))
		self._assert_money(self._balance(record)["remaining_amount"], 0)
		# Exercise only the ordering validator: this unsaved base-wage object is not
		# an invented historical work report or a claim that an old payroll was posted.
		older_base = frappe._dict(
			name="UNSAVED-ORDERING-VALIDATOR-ONLY",
			company=self.TEST_COMPANY,
			employee=self.employee,
			month_start=self.first_month,
			piecework_amount=40,
		)
		with self.set_user(self.owner):
			wage_adjustments.assert_confirmation_order(older_base)
		self._assert_money(self._balance(record)["applied_amount"], 40)
		current = get_first_day(nowdate())
		current_record = self._record("Supplement", 5, month=current)
		draft = self._build(current)
		with self.set_user(self.owner), self.assertRaisesRegex(frappe.ValidationError, "natural month"):
			summary.confirm_monthly_summary(draft.name)
		self.assertEqual(draft.reload().docstatus, 0)
		self._assert_money(self._balance(current_record)["applied_amount"], 0)
