"""Payroll-only corrections on synthetic reports in an isolated test site.

Real reporting actions create the immutable production facts. Only the unrelated
material-readiness reader is stubbed; correction, permissions, report history,
wage draft rebuilding, and confirmation use their real implementation.
"""

from datetime import datetime, time
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_months, get_first_day, nowdate, random_string

from process_simplification.management_access import OWNER_ROLE
from process_simplification.production_reporting import payroll_quantity, service, summary, wage_adjustments
from process_simplification.tests import test_worker_reporting as fixture_helpers


class TestPayrollQuantityCorrections(IntegrationTestCase):
	TEST_COMPANY = fixture_helpers.TestWorkerReporting.TEST_COMPANY
	OTHER_COMPANY = fixture_helpers.TestWorkerReporting.OTHER_COMPANY

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		for doctype, values in (
			("Warehouse Type", {"name": "Transit"}),
			("Gender", {"gender": "Female"}),
			("UOM", {"uom_name": "Nos", "enabled": 1, "must_be_whole_number": 1}),
		):
			if not frappe.db.exists(doctype, next(iter(values.values()))):
				frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True)
		# Fresh app-only sites have not run Setup Wizard's item-group/UOM seeds.
		# Reuse an existing root so an already initialized test site stays valid.
		if not frappe.db.exists("Item Group", "Products"):
			root = frappe.db.get_value("Item Group", {"parent_item_group": ("is", "not set")}, "name")
			if not root:
				root = (
					frappe.get_doc(
						{
							"doctype": "Item Group",
							"item_group_name": "All Item Groups",
							"is_group": 1,
						}
					)
					.insert(ignore_permissions=True)
					.name
				)
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "Products",
					"parent_item_group": root,
					"is_group": 0,
				}
			).insert(ignore_permissions=True)
		fixture_helpers.TestWorkerReporting._ensure_master_fixtures()

	def setUp(self):
		super().setUp()
		frappe.local.db = self._primary_connection
		frappe.set_user("Administrator")
		self.enterContext(
			patch("process_simplification.production_workflow.service.assert_work_order_dispatch_ready")
		)
		self.enterContext(
			patch(
				"process_simplification.production_workflow.service.get_work_order_dispatch_state",
				return_value=frappe._dict(can_dispatch=True, block_code=None, block_message=None),
			)
		)
		self.fixtures = fixture_helpers.TestWorkerReporting()
		self.fixtures.worker_user = self.fixtures._make_worker()
		self.fixtures.worker = frappe.db.get_value("Employee", {"user_id": self.fixtures.worker_user}, "name")
		self.fixtures.supervisor = self.fixtures._make_supervisor()
		self.fixtures.wage_manager = self.fixtures._make_user("Production Wage Manager")
		self.fixtures._grant_company(self.fixtures.wage_manager, self.TEST_COMPANY)
		self.owner = self.fixtures._make_user(OWNER_ROLE)
		self.fixtures._grant_company(self.owner, self.TEST_COMPANY)
		self.month = get_first_day(add_months(nowdate(), -1))
		self.production_moment = datetime.combine(add_days(self.month, 14), time(9, 0, 0, 123456))

	def tearDown(self):
		try:
			frappe.local.db = self._primary_connection
			frappe.set_user("Administrator")
			frappe.db.rollback()
		finally:
			super().tearDown()

	def _report(self, *, qty=120, rate=2.5, wage_type="Piecework", approve=True):
		with self.freeze_time(self.production_moment):
			job_card, assignment = self.fixtures._setup_flow(
				qty=qty, rate=rate, wage_type=wage_type, valid_from=self.month
			)
			report = self.fixtures._submit(assignment, qty, minutes=2)
			if approve:
				report = self.fixtures._approve(report)
		return report.reload(), job_card.reload(), assignment.reload()

	def _correct(
		self,
		report,
		quantity,
		*,
		reason="Count checked by owner",
		request_id=None,
		expected_modified=None,
		user=None,
	):
		with self.set_user(user or self.owner):
			return payroll_quantity.set_report_payroll_quantity(
				report.name,
				quantity,
				reason,
				str(report.modified) if expected_modified is None else expected_modified,
				request_id or "payroll-test-" + random_string(20),
			)

	def _build(self):
		with self.set_user(self.owner):
			result = summary.build_monthly_summaries(self.TEST_COMPANY, self.month, self.fixtures.worker)
		self.assertEqual(len(result["summaries"]), 1)
		return frappe.get_doc("Monthly Worker Wage Summary", result["summaries"][0])

	def _money(self, actual, expected):
		self.assertEqual(Decimal(str(actual)).quantize(Decimal("0.01")), Decimal(str(expected)))

	def _production_snapshot(self, report, job_card, assignment):
		"""Include all child time logs, WO operations and assignment audit fields."""
		report_fields = (
			"status",
			"completed_qty",
			"reported_minutes",
			"wage_amount",
			"rate",
			"wage_rate",
			"wage_rate_revision",
			"labor_date",
			"actual_start_time",
			"actual_end_time",
			"actual_minutes",
			"manual_time_entry",
			"piecework_rate_snapshot",
			"hourly_rate_snapshot",
			"request_key",
			"completion_request_key",
			"reviewed_by",
			"reviewed_at",
		)
		original = frappe.get_doc(report.doctype, report.name)
		return frappe.as_json(
			{
				"report": {field: original.get(field) for field in report_fields},
				"time_segments": [row.as_dict() for row in original.time_segments],
				"job_card": frappe.get_doc(job_card.doctype, job_card.name).as_dict(),
				"assignment": frappe.get_doc(assignment.doctype, assignment.name).as_dict(),
				"work_order": frappe.get_doc("Work Order", job_card.work_order).as_dict(),
			}
		)

	def test_owner_corrects_120_to_100_without_changing_production_or_creating_payroll(self):
		report, job_card, assignment = self._report()
		self.assertEqual(job_card.docstatus, 1)
		before = self._production_snapshot(report, job_card, assignment)
		self._correct(report, 100, reason="Actual payable quantity is 100")
		report.reload()
		self.assertEqual(report.payroll_qty_corrected, 1)
		self.assertEqual(report.payroll_qty, 100)
		self.assertEqual(report.payroll_correction_reason, "Actual payable quantity is 100")
		self.assertEqual(report.payroll_corrected_by, self.owner)
		self.assertTrue(report.payroll_corrected_at)
		comments = frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": report.doctype,
				"reference_name": report.name,
				"comment_type": "Comment",
			},
			fields=["content", "owner"],
		)
		self.assertTrue(
			any(
				"120" in row.content
				and "100" in row.content
				and "Actual payable quantity is 100" in row.content
				and row.owner == self.owner
				for row in comments
			)
		)
		self.assertEqual(self._production_snapshot(report, job_card, assignment), before)
		self.assertEqual(
			frappe.db.count("Monthly Worker Wage Summary", {"employee": self.fixtures.worker}), 0
		)
		for user, reader in (
			(self.owner, service.get_review_history),
			(self.fixtures.worker_user, service.get_worker_report_history),
		):
			with self.set_user(user):
				row = next(row for row in reader(job_card=job_card.name)["rows"] if row.name == report.name)
			self.assertEqual(row.completed_qty, 120)
			self.assertEqual(row.payroll_qty, 100)
			self._money(row.wage_amount, 300)
			self._money(row.payroll_amount, 250)
			self.assertEqual(row.payroll_correction_reason, "Actual payable quantity is 100")
			self.assertEqual(bool(row.can_correct_payroll_qty), user == self.owner)

	def test_multiple_corrections_use_original_rate_even_after_rate_rule_changes(self):
		report, job_card, assignment = self._report()
		before = self._production_snapshot(report, job_card, assignment)
		self._correct(report, 100)
		with self.set_user(self.fixtures.wage_manager):
			rule = frappe.get_doc("Operation Wage Rate", report.wage_rate)
			rule.piecework_rate = 9
			rule.save()
		self._correct(report.reload(), 80, reason="Recounted payable units")
		self.assertEqual(self._production_snapshot(report, job_card, assignment), before)
		draft = self._build()
		self._money(draft.piecework_amount, 200)
		self._money(draft.total_amount, 200)
		self.assertEqual(draft.details[0].rate, 2.5)
		self.assertEqual(draft.details[0].completed_qty, 120)
		self.assertEqual(draft.details[0].payroll_qty, 80)
		self.assertEqual(draft.details[0].payroll_qty_corrected, 1)
		comments = frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": report.doctype,
				"reference_name": report.name,
				"comment_type": "Comment",
			},
			pluck="content",
		)
		self.assertTrue(any("120" in content and "100" in content for content in comments))
		self.assertTrue(
			any(
				"100" in content and "80" in content and "Recounted payable units" in content
				for content in comments
			)
		)

	def test_request_replay_is_stable_and_payload_or_stale_version_conflicts_do_not_write(self):
		report, _, _ = self._report()
		request_id = "replay-" + random_string(20)
		original_modified = str(report.modified)
		self._correct(report, 100, request_id=request_id)
		first = frappe.as_json(report.reload().as_dict())
		comment_count = frappe.db.count(
			"Comment", {"reference_doctype": report.doctype, "reference_name": report.name}
		)
		self._correct(report, "100.000000", request_id=request_id, expected_modified=original_modified)
		self.assertEqual(frappe.as_json(report.reload().as_dict()), first)
		self.assertEqual(
			frappe.db.count("Comment", {"reference_doctype": report.doctype, "reference_name": report.name}),
			comment_count,
		)
		for options in (
			{"quantity": 99, "request_id": request_id, "expected_modified": original_modified},
			{"quantity": 100, "request_id": request_id, "reason": "Different reason"},
			{"quantity": 90, "expected_modified": original_modified},
		):
			with self.subTest(options=options), self.assertRaises(frappe.ValidationError):
				self._correct(report, **options)
			self.assertEqual(frappe.as_json(report.reload().as_dict()), first)

	def test_zero_payroll_quantity_is_not_replaced_by_original_quantity(self):
		report, job_card, assignment = self._report()
		before = self._production_snapshot(report, job_card, assignment)
		self._correct(report, 0, reason="No payable units for this report")
		draft = self._build()
		self.assertEqual(len(draft.details), 1)
		self.assertEqual(draft.details[0].payroll_qty_corrected, 1)
		self.assertEqual(draft.details[0].payroll_qty, 0)
		self.assertEqual(draft.details[0].completed_qty, 120)
		self._money(draft.details[0].amount, 0)
		self._money(draft.total_amount, 0)
		with self.set_user(self.owner):
			confirmed = summary.confirm_monthly_summary(draft.name)
		self.assertEqual(confirmed.docstatus, 1)
		self.assertEqual(self._production_snapshot(report, job_card, assignment), before)

	def test_zero_original_piecework_price_remains_zero_after_correction(self):
		report, _, _ = self._report(rate=0)
		self._correct(report, 100)
		draft = self._build()
		self.assertEqual(draft.details[0].rate, 0)
		self.assertEqual(draft.details[0].payroll_qty, 100)
		self._money(draft.details[0].amount, 0)
		self._money(draft.total_amount, 0)

	def test_invalid_quantity_and_missing_reason_are_rejected_without_mutation(self):
		report, _, _ = self._report()
		before = frappe.as_json(report.as_dict())
		for quantity in (-1, "NaN", "Infinity", "not-a-number", None):
			with self.subTest(quantity=quantity), self.assertRaises(frappe.ValidationError):
				self._correct(report, quantity)
		for reason in (None, "", "  "):
			with self.subTest(reason=reason), self.assertRaises(frappe.ValidationError):
				self._correct(report, 100, reason=reason)
		self.assertEqual(frappe.as_json(report.reload().as_dict()), before)

	def test_time_report_rejected_and_original_minutes_and_production_preserved(self):
		report, job_card, assignment = self._report(wage_type="Time", rate=30)
		before = self._production_snapshot(report, job_card, assignment)
		with self.assertRaises(frappe.ValidationError):
			self._correct(report, 100)
		self.assertEqual(self._production_snapshot(report, job_card, assignment), before)
		self.assertFalse(report.reload().payroll_qty_corrected)
		self._money(self._build().total_amount, report.wage_amount)

	def test_pending_and_rejected_reports_cannot_be_corrected(self):
		report, job_card, assignment = self._report(approve=False)
		before = self._production_snapshot(report, job_card, assignment)
		with self.assertRaises(frappe.ValidationError):
			self._correct(report, 100)
		self.assertEqual(self._production_snapshot(report, job_card, assignment), before)
		with self.set_user(self.fixtures.supervisor):
			service.reject_work_report(report.name, "Incorrect production report")
		report.reload()
		with self.assertRaises(frappe.ValidationError):
			self._correct(report, 100)
		self.assertFalse(report.reload().payroll_qty_corrected)

	def test_worker_supervisor_wage_manager_and_out_of_scope_owner_cannot_correct(self):
		report, _, _ = self._report()
		outsider = self.fixtures._make_user(OWNER_ROLE)
		self.fixtures._grant_company(outsider, self.OTHER_COMPANY)
		unscoped = self.fixtures._make_user(OWNER_ROLE)
		before = frappe.as_json(report.as_dict())
		for user in (
			self.fixtures.worker_user,
			self.fixtures.supervisor,
			self.fixtures.wage_manager,
			outsider,
			unscoped,
		):
			with self.subTest(user=user), self.assertRaises(frappe.PermissionError):
				self._correct(report, 100, user=user)
		self.assertEqual(frappe.as_json(report.reload().as_dict()), before)

	def test_system_manager_can_correct_but_confirmed_employee_month_is_frozen(self):
		report, _, _ = self._report()
		manager = self.fixtures._make_user("System Manager")
		self._correct(report, 100, user=manager)
		self.assertEqual(report.reload().payroll_corrected_by, manager)
		draft = self._build()
		with self.set_user(self.owner):
			confirmed = summary.confirm_monthly_summary(draft.name)
		self.assertEqual(confirmed.docstatus, 1)
		before = frappe.as_json(report.reload().as_dict())
		for user in (self.owner, manager, "Administrator"):
			with self.subTest(user=user), self.assertRaises(frappe.ValidationError):
				self._correct(report, 80, user=user)
		self.assertEqual(frappe.as_json(report.reload().as_dict()), before)
		self._money(confirmed.reload().total_amount, 250)

	def test_existing_monthly_draft_refreshes_in_place_then_confirmation_freezes_amount(self):
		report, job_card, assignment = self._report()
		draft = self._build()
		self._money(draft.total_amount, 300)
		before = self._production_snapshot(report, job_card, assignment)
		self._correct(report, 100)
		draft.reload()
		self.assertEqual(draft.docstatus, 0)
		self.assertEqual(len(draft.details), 1)
		self.assertEqual(draft.details[0].source_report, report.name)
		self.assertEqual(draft.details[0].completed_qty, 120)
		self.assertEqual(draft.details[0].payroll_qty, 100)
		self._money(draft.details[0].amount, 250)
		self._money(draft.total_amount, 250)
		self.assertEqual(
			frappe.db.count("Monthly Worker Wage Summary", {"employee": self.fixtures.worker}), 1
		)
		self.assertEqual(self._production_snapshot(report, job_card, assignment), before)
		with self.set_user(self.owner):
			confirmed = summary.confirm_monthly_summary(draft.name)
		self.assertEqual(confirmed.docstatus, 1)
		self._money(confirmed.total_amount, 250)
		self.assertEqual(report.reload().monthly_summary, confirmed.name)
		with self.assertRaises(frappe.ValidationError):
			self._correct(report, 80)
		self._money(confirmed.reload().total_amount, 250)

	def test_direct_document_edit_cannot_bypass_payroll_or_production_guards(self):
		report, _, _ = self._report()
		before = frappe.as_json(report.as_dict())
		for field, value in (("completed_qty", 100), ("payroll_qty", 100), ("payroll_qty_corrected", 1)):
			doc = frappe.get_doc(report.doctype, report.name)
			doc.set(field, value)
			doc.flags.worker_reporting_action = True
			with (
				self.set_user("Administrator"),
				self.subTest(field=field),
				self.assertRaises(frappe.ValidationError),
			):
				doc.save(ignore_permissions=True)
		self.assertEqual(frappe.as_json(report.reload().as_dict()), before)

	def test_correction_rebuilds_existing_adjustments_without_consuming_or_overdeducting(self):
		report, job_card, assignment = self._report()
		before = self._production_snapshot(report, job_card, assignment)
		with self.set_user(self.owner):
			supplement = wage_adjustments.record_wage_adjustment(
				self.TEST_COMPANY,
				self.fixtures.worker,
				self.month,
				"Supplement",
				20,
				"Synthetic correction bonus",
				request_id="payroll-credit-" + random_string(20),
			)
			deduction = wage_adjustments.record_wage_adjustment(
				self.TEST_COMPANY,
				self.fixtures.worker,
				self.month,
				"Deduction",
				280,
				"Synthetic correction recovery",
				request_id="payroll-debit-" + random_string(20),
			)
		draft = self._build()
		self._money(draft.piecework_amount, 300)
		self._money(draft.supplement_amount, 20)
		self._money(draft.deduction_amount, 280)
		self._money(draft.pending_deduction_amount, 0)
		self._money(draft.total_amount, 40)
		self._correct(report, 100)
		draft.reload()
		self._money(draft.piecework_amount, 250)
		self._money(draft.supplement_amount, 20)
		self._money(draft.deduction_amount, 270)
		self._money(draft.pending_deduction_amount, 10)
		self._money(draft.total_amount, 0)
		self.assertEqual(self._production_snapshot(report, job_card, assignment), before)
		with self.set_user(self.owner):
			for record, original in ((supplement, 20), (deduction, 280)):
				balance = wage_adjustments.get_wage_adjustment_balance(record["name"])
				self._money(balance["applied_amount"], 0)
				self._money(balance["remaining_amount"], original)
			confirmed = summary.confirm_monthly_summary(draft.name)
			self.assertEqual(confirmed.docstatus, 1)
			balance = wage_adjustments.get_wage_adjustment_balance(deduction["name"])
			self._money(balance["applied_amount"], 270)
			self._money(balance["remaining_amount"], 10)
			self._money(
				wage_adjustments.get_wage_adjustment_balance(supplement["name"])["remaining_amount"], 0
			)
			# The unused 10 must remain available in the following month; building a
			# draft still must not spend either adjustment a second time.
			next_month = get_first_day(add_months(self.month, 1))
			wage_adjustments.record_wage_adjustment(
				self.TEST_COMPANY,
				self.fixtures.worker,
				next_month,
				"Supplement",
				15,
				"Synthetic following-month pay",
				request_id="payroll-next-credit-" + random_string(20),
			)
			result = summary.build_monthly_summaries(self.TEST_COMPANY, next_month, self.fixtures.worker)
			next_draft = frappe.get_doc("Monthly Worker Wage Summary", result["summaries"][0])
			self._money(next_draft.supplement_amount, 15)
			self._money(next_draft.deduction_amount, 10)
			self._money(next_draft.pending_deduction_amount, 0)
			self._money(next_draft.total_amount, 5)
			self._money(
				wage_adjustments.get_wage_adjustment_balance(deduction["name"])["remaining_amount"], 10
			)

	def test_employee_rename_does_not_change_report_snapshot_response_or_correction_audit(self):
		report, job_card, assignment = self._report()
		original_employee_name = report.employee_name
		employee = frappe.get_doc("Employee", self.fixtures.worker)
		employee.first_name = "Renamed payroll fixture worker"
		employee.save(ignore_permissions=True)
		self.assertNotEqual(employee.employee_name, original_employee_name)
		self.assertEqual(report.reload().employee_name, original_employee_name)
		before = self._production_snapshot(report, job_card, assignment)
		version_filters = {"ref_doctype": report.doctype, "docname": report.name}
		old_versions = set(frappe.get_all("Version", filters=version_filters, pluck="name"))
		result = self._correct(report, 100, reason="Payroll recount after employee name update")
		stored = report.reload().as_dict()
		self.assertEqual(stored.employee_name, original_employee_name)
		self.assertEqual(result["report"].employee_name, original_employee_name)
		# Compare every persisted field, including the original captured time segments;
		# the response may additionally contain only its computed UI/payroll fields.
		self.assertEqual(
			frappe.as_json({key: result["report"].get(key) for key in stored}),
			frappe.as_json(stored),
		)
		self.assertEqual(self._production_snapshot(report, job_card, assignment), before)
		new_versions = [
			row
			for row in frappe.get_all("Version", filters=version_filters, fields=["name", "data", "owner"])
			if row.name not in old_versions
		]
		self.assertTrue(new_versions, "The payroll correction must have a Version audit")
		allowed_changes = {
			"payroll_qty_corrected",
			"payroll_qty",
			"payroll_correction_reason",
			"payroll_corrected_by",
			"payroll_corrected_at",
			"payroll_correction_request_id",
		}
		quantity_changes = []
		for version in new_versions:
			self.assertEqual(version.owner, self.owner)
			data = frappe.parse_json(version.data)
			changes = data.get("changed", [])
			fields = {row[0] for row in changes}
			self.assertNotIn("employee_name", fields)
			self.assertTrue(fields.issubset(allowed_changes), fields - allowed_changes)
			for key in ("added", "removed", "row_changed"):
				self.assertFalse(data.get(key), "Payroll correction must not audit child-table changes")
			quantity_changes.extend(row for row in changes if row[0] == "payroll_qty")
		self.assertTrue(any(float(row[2]) == 100 and row[1] != row[2] for row in quantity_changes))
