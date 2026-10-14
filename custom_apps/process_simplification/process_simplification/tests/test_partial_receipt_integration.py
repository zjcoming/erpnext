"""Real native posting, approved wages and restricted roles for partial receipts."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry as make_manufacturing_entry
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from process_simplification.management_access import (
	WAREHOUSE_OPERATOR_ROLE, ensure_management_access,
)
from process_simplification.production_reporting import continuation, service as reporting
from process_simplification.production_workflow import service as workflow
from process_simplification.tests import test_native_work_order_submit as native_fixtures
from process_simplification.tests import test_worker_reporting as reporting_fixtures


class TestPartialReceiptIntegration(IntegrationTestCase):
	TEST_COMPANY = reporting_fixtures.TestWorkerReporting.TEST_COMPANY
	TEST_COMPANY_ABBR = reporting_fixtures.TestWorkerReporting.TEST_COMPANY_ABBR
	OTHER_COMPANY = reporting_fixtures.TestWorkerReporting.OTHER_COMPANY
	OTHER_COMPANY_ABBR = reporting_fixtures.TestWorkerReporting.OTHER_COMPANY_ABBR
	_ensure_company = classmethod(reporting_fixtures.TestWorkerReporting._ensure_company.__func__)
	_draft = native_fixtures.TestNativeWorkOrderSubmit._draft
	_email = reporting_fixtures.TestWorkerReporting._email
	_make_user = reporting_fixtures.TestWorkerReporting._make_user
	_make_employee = reporting_fixtures.TestWorkerReporting._make_employee
	_make_worker = reporting_fixtures.TestWorkerReporting._make_worker
	_make_supervisor = reporting_fixtures.TestWorkerReporting._make_supervisor
	_grant_company = reporting_fixtures.TestWorkerReporting._grant_company
	_make_rate = reporting_fixtures.TestWorkerReporting._make_rate
	_assign = reporting_fixtures.TestWorkerReporting._assign
	_submit_as = reporting_fixtures.TestWorkerReporting._submit_as
	_submit = reporting_fixtures.TestWorkerReporting._submit
	_approve = reporting_fixtures.TestWorkerReporting._approve

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._ensure_company(cls.TEST_COMPANY, cls.TEST_COMPANY_ABBR)
		cls._ensure_company(cls.OTHER_COMPANY, cls.OTHER_COMPANY_ABBR)
		ensure_management_access()
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.addCleanup(self._rollback)
		self.enterContext(self.change_settings("Manufacturing Settings", {
			"disable_capacity_planning": 1,
			"backflush_raw_materials_based_on": "Material Transferred for Manufacture",
		}))
		self.supervisor = self._make_supervisor()
		self.worker_user = self._make_worker()
		self.worker = frappe.db.get_value("Employee", {"user_id": self.worker_user}, "name")
		self.wage_manager = self._make_user("Production Wage Manager")
		self._grant_company(self.wage_manager, self.TEST_COMPANY)
		self.warehouse_user = self._make_user(WAREHOUSE_OPERATOR_ROLE)
		self._grant_company(self.warehouse_user, self.TEST_COMPANY)

	def _rollback(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _flow(self, split):
		wo = self._draft()
		wo.qty = 4
		prefix = "PARTIAL-" + frappe.generate_hash(length=8)
		for field, label in (("wip_warehouse", "WIP"), ("fg_warehouse", "FG")):
			wo.set(field, frappe.get_doc(dict(doctype="Warehouse",
				warehouse_name=prefix + "-" + label, company=wo.company)).insert().name)
		wo.get_items_and_operations_from_bom()
		if split:
			frappe.db.set_value("Operation", wo.operations[0].operation, "create_job_card_based_on_batch_size", 1)
			frappe.clear_document_cache("Operation", wo.operations[0].operation)
			wo.operations[0].batch_size = 2
		wo.save()
		with self.set_user(self.supervisor):
			wo.submit()
		raw = wo.required_items[0].item_code
		make_stock_entry(item_code=raw, qty=4, to_warehouse=wo.source_warehouse,
			company=wo.company, basic_rate=1, posting_date=add_days(nowdate(), -1), posting_time="12:00:00")
		with self.set_user(self.warehouse_user):
			issue = frappe.get_doc(make_manufacturing_entry(wo.name, "Material Transfer for Manufacture", qty=4))
			issue.insert().submit()
		cards = [frappe.get_doc("Job Card", name) for name in frappe.get_all("Job Card",
			{"work_order": wo.name}, pluck="name", order_by="creation,name")]
		self.assertEqual(len(cards), 2 if split else 1)
		self._make_rate(cards[0], rate=3)
		assignments = [self._assign(card) for card in cards]
		return wo.reload(), cards, assignments

	def _balance(self, item, warehouse):
		return flt(frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty"))

	def test_two_managed_batches_post_first_while_second_remains_executable(self):
		wo, cards, assignments = self._flow(split=True)
		first_report = self._approve(self._submit(assignments[0], 2))
		self.assertEqual(cards[0].reload().docstatus, 1)
		self.assertEqual(cards[1].reload().docstatus, 0)
		self.assertEqual(wo.reload().operations[0].completed_qty, 2)
		with self.set_user(self.supervisor):
			first_request = workflow.request_manufacture(wo.name, qty=2)
			self.assertEqual(workflow.request_manufacture(wo.name, qty=2)["stock_entry"], first_request["stock_entry"])
			with self.assertRaises(frappe.ValidationError):
				workflow.request_manufacture(wo.name, qty=1)
		with self.set_user(self.warehouse_user):
			first = frappe.get_doc("Stock Entry", first_request["stock_entry"]).submit()
		self.assertEqual(first.docstatus, 1)
		self.assertEqual(wo.reload().produced_qty, 2)
		self.assertNotEqual(wo.status, "Completed")
		self.assertEqual(self._balance(wo.production_item, wo.fg_warehouse), 2)
		self.assertEqual(self._balance(wo.required_items[0].item_code, wo.wip_warehouse), 2)
		# A new worker session proves the remaining assigned card is still usable.
		rejected = self._submit(assignments[1], 2)
		with self.set_user(self.supervisor):
			reporting.reject_work_report(rejected.name, "本批需重新核对数量")
		self.assertEqual(wo.reload().operations[0].completed_qty, 2)
		second_report = self._approve(self._submit(assignments[1], 2))
		with self.set_user(self.supervisor):
			second_request = workflow.request_manufacture(wo.name)
			self.assertEqual(second_request["qty"], 2)
		with self.set_user(self.warehouse_user):
			second = frappe.get_doc("Stock Entry", second_request["stock_entry"]).submit()
		self.assertNotEqual(first.name, second.name)
		self.assertEqual(wo.reload().produced_qty, 4)
		self.assertEqual(wo.status, "Completed")
		self.assertEqual(self._balance(wo.production_item, wo.fg_warehouse), 4)
		self.assertEqual(self._balance(wo.required_items[0].item_code, wo.wip_warehouse), 0)
		approved = frappe.get_all("Job Card Work Report",
			filters={"job_card": ["in", [c.name for c in cards]], "status": "Approved"},
			fields=["name", "completed_qty", "wage_amount"])
		self.assertEqual({r.name for r in approved}, {first_report.name, second_report.name})
		self.assertEqual(sum(r.completed_qty for r in approved), 4)
		self.assertEqual(sum(r.wage_amount for r in approved), 12)
		with self.set_user(self.supervisor), self.assertRaises(frappe.ValidationError):
			workflow.request_manufacture(wo.name, qty=1)

	def test_single_draft_card_approved_half_still_needs_legal_native_completion(self):
		wo, cards, assignments = self._flow(split=False)
		self._approve(self._submit(assignments[0], 2))
		self.assertEqual(cards[0].reload().docstatus, 0)
		self.assertEqual(wo.reload().operations[0].completed_qty, 0)
		with self.set_user(self.supervisor), self.assertRaises(frappe.ValidationError):
			workflow.request_manufacture(wo.name, qty=2)
		self.assertEqual(wo.reload().produced_qty, 0)
		self.assertEqual(self._balance(wo.required_items[0].item_code, wo.wip_warehouse), 4)
		self.assertEqual(self._balance(wo.production_item, wo.fg_warehouse), 0)

	def test_inflight_single_card_posts_first_batch_then_continues_without_extra_material_or_wages(self):
		wo, cards, assignments = self._flow(split=False)
		first_report = self._approve(self._submit(assignments[0], 2))
		before = first_report.reload().as_dict()
		with self.set_user(self.supervisor):
			carried = continuation.finalize_partial_job_card(
				cards[0].name, "partial-receipt-inflight", expected_completed_qty=2, expected_remaining_qty=2,
			)
			first_request = workflow.request_manufacture(wo.name, qty=2)
		with self.set_user(self.warehouse_user):
			frappe.get_doc("Stock Entry", first_request["stock_entry"]).submit()
		self.assertEqual(wo.reload().produced_qty, 2)
		self.assertNotEqual(wo.status, "Completed")
		self.assertEqual(self._balance(wo.production_item, wo.fg_warehouse), 2)
		self.assertEqual(self._balance(wo.required_items[0].item_code, wo.wip_warehouse), 2)
		self.assertEqual(frappe.db.get_value("Job Card", carried["job_card"], "docstatus"), 0)
		with self.set_user(self.supervisor), self.assertRaises(frappe.ValidationError):
			workflow.request_manufacture(wo.name, qty=1)
		remaining_assignment = frappe.get_doc("Job Card Worker Assignment", carried["assignments"][0])
		second_report = self._approve(self._submit(remaining_assignment, 2))
		with self.set_user(self.supervisor):
			second_request = workflow.request_manufacture(wo.name, qty=2)
		with self.set_user(self.warehouse_user):
			frappe.get_doc("Stock Entry", second_request["stock_entry"]).submit()
		self.assertEqual(wo.reload().produced_qty, 4)
		self.assertEqual(wo.status, "Completed")
		self.assertEqual(self._balance(wo.production_item, wo.fg_warehouse), 4)
		self.assertEqual(self._balance(wo.required_items[0].item_code, wo.wip_warehouse), 0)
		self.assertEqual(first_report.reload().as_dict(), before)
		self.assertEqual(first_report.wage_amount + second_report.reload().wage_amount, 12)
		self.assertEqual(frappe.db.count("Stock Entry", {
			"work_order": wo.name, "purpose": "Material Transfer for Manufacture", "docstatus": 1,
		}), 1)
		self.assertEqual(frappe.db.count("Job Card Work Report", {
			"work_order": wo.name, "status": "Approved",
		}), 2)
