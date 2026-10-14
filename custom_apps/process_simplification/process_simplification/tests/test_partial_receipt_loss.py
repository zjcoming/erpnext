"""Native receipts cannot turn previously booked process loss into new output."""

import frappe
from frappe.tests import IntegrationTestCase

from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry
from process_simplification.management_access import WAREHOUSE_OPERATOR_ROLE, ensure_management_access
from process_simplification.production_exceptions import service as exceptions
from process_simplification.production_exceptions.constants import APPLIED, PROCESS_LOSS
from process_simplification.production_workflow import service as workflow
from process_simplification.tests import test_partial_receipt_integration as fixtures


class TestPartialReceiptLoss(IntegrationTestCase):
	TEST_COMPANY = fixtures.TestPartialReceiptIntegration.TEST_COMPANY
	TEST_COMPANY_ABBR = fixtures.TestPartialReceiptIntegration.TEST_COMPANY_ABBR
	OTHER_COMPANY = fixtures.TestPartialReceiptIntegration.OTHER_COMPANY
	OTHER_COMPANY_ABBR = fixtures.TestPartialReceiptIntegration.OTHER_COMPANY_ABBR
	_ensure_company = classmethod(fixtures.TestPartialReceiptIntegration._ensure_company.__func__)
	_draft = fixtures.TestPartialReceiptIntegration._draft
	_email = fixtures.TestPartialReceiptIntegration._email
	_make_user = fixtures.TestPartialReceiptIntegration._make_user
	_make_employee = fixtures.TestPartialReceiptIntegration._make_employee
	_make_worker = fixtures.TestPartialReceiptIntegration._make_worker
	_make_supervisor = fixtures.TestPartialReceiptIntegration._make_supervisor
	_grant_company = fixtures.TestPartialReceiptIntegration._grant_company
	_make_rate = fixtures.TestPartialReceiptIntegration._make_rate
	_assign = fixtures.TestPartialReceiptIntegration._assign
	_submit_as = fixtures.TestPartialReceiptIntegration._submit_as
	_submit = fixtures.TestPartialReceiptIntegration._submit
	_approve = fixtures.TestPartialReceiptIntegration._approve
	_flow = fixtures.TestPartialReceiptIntegration._flow
	_balance = fixtures.TestPartialReceiptIntegration._balance
	_rollback = fixtures.TestPartialReceiptIntegration._rollback

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

	def _assert_receipt_rejected_without_stock_change(self, entry, work_order):
		before = (
			work_order.reload().produced_qty, work_order.process_loss_qty,
			self._balance(work_order.production_item, work_order.fg_warehouse),
			self._balance(work_order.required_items[0].item_code, work_order.wip_warehouse),
		)
		frappe.db.savepoint("reject_reused_process_loss")
		with self.set_user(self.warehouse_user), self.assertRaisesRegex(
			frappe.ValidationError, "已入账损耗不能重复入库"
		):
			entry.reload().submit()
		frappe.db.rollback(save_point="reject_reused_process_loss")
		self.assertEqual(entry.reload().docstatus, 0)
		self.assertEqual((
			work_order.reload().produced_qty, work_order.process_loss_qty,
			self._balance(work_order.production_item, work_order.fg_warehouse),
			self._balance(work_order.required_items[0].item_code, work_order.wip_warehouse),
		), before)

	def test_native_partial_receipt_cannot_reuse_loss_or_pending_reports_as_output(self):
		# This regression uses legal native batch cards to isolate ordinary receipt
		# validation. It does not replace the separate in-flight continuation case.
		wo, cards, assignments = self._flow(split=True)
		first_report = self._approve(self._submit(assignments[0], 1))
		first_report_before = first_report.reload().as_dict()
		with self.set_user(self.worker_user):
			loss = exceptions.submit_exception(
				assignment=assignments[0].name, request_type=PROCESS_LOSS, qty=1,
				cause="Operation Error", reason="首批两件中一件报工，一件工序损耗",
				request_key="native-receipt-loss-" + frappe.generate_hash(length=12),
			)
		with self.set_user(self.supervisor):
			exceptions.approve_exception(loss.name)
		self.assertEqual(loss.reload().status, APPLIED)
		self.assertEqual((cards[0].reload().docstatus, cards[0].total_completed_qty, cards[0].process_loss_qty), (1, 1, 1))
		self.assertEqual(cards[1].reload().docstatus, 0)
		self.assertEqual((wo.reload().operations[0].completed_qty, wo.operations[0].process_loss_qty), (1, 1))

		# The guided quantity is gross completion, so <= the unbooked loss cannot
		# produce a positive finished-goods row and must fail with a useful message.
		with self.set_user(self.supervisor):
			for qty in (0.5, 1):
				with self.subTest(qty=qty), self.assertRaisesRegex(frappe.ValidationError, "待入账损耗"):
					workflow.request_manufacture(wo.name, qty=qty)
		self.assertEqual(frappe.db.count("Stock Entry", {"work_order": wo.name, "purpose": "Manufacture"}), 0)

		with self.set_user(self.warehouse_user):
			first = frappe.get_doc(make_stock_entry(wo.name, "Manufacture", qty=2))
			self.assertFalse(first.custom_process_workflow_action)
			first.insert().submit()
		self.assertEqual((first.fg_completed_qty, first.process_loss_qty), (2, 1))
		self.assertEqual((wo.reload().produced_qty, wo.process_loss_qty), (1, 1))
		self.assertNotEqual(wo.status, "Completed")
		self.assertEqual(self._balance(wo.production_item, wo.fg_warehouse), 1)
		self.assertEqual(self._balance(wo.required_items[0].item_code, wo.wip_warehouse), 2)

		# Native operation validation counts produced goods but omits booked loss:
		# 1 requested + 1 produced <= 1 completed + 1 loss. This draft is therefore
		# valid natively; posting must apply the current managed operation balance.
		with self.set_user(self.warehouse_user):
			extra = frappe.get_doc(make_stock_entry(wo.name, "Manufacture", qty=1)).insert()
		self.assertFalse(extra.custom_process_workflow_action)
		self.assertEqual(extra.process_loss_qty, 0)
		self._assert_receipt_rejected_without_stock_change(extra, wo)

		pending = self._submit(assignments[1], 2)
		self.assertEqual(pending.status, "Pending Approval")
		self.assertEqual((wo.reload().operations[0].completed_qty, wo.operations[0].process_loss_qty), (1, 1))
		self._assert_receipt_rejected_without_stock_change(extra, wo)

		# Genuine new approved output reopens the same ordinary native receipt path.
		second_report = self._approve(pending)
		self.assertEqual((wo.reload().operations[0].completed_qty, wo.operations[0].process_loss_qty), (3, 1))
		with self.set_user(self.warehouse_user):
			final = frappe.get_doc(make_stock_entry(wo.name, "Manufacture", qty=2)).insert().submit()
		self.assertEqual((final.fg_completed_qty, final.process_loss_qty), (2, 0))
		self.assertEqual((wo.reload().produced_qty, wo.process_loss_qty, wo.status), (3, 1, "Completed"))
		self.assertEqual(self._balance(wo.production_item, wo.fg_warehouse), 3)
		self.assertEqual(self._balance(wo.required_items[0].item_code, wo.wip_warehouse), 0)
		self.assertEqual(first_report.reload().as_dict(), first_report_before)
		self.assertEqual(first_report.wage_amount + second_report.reload().wage_amount, 9)
		self.assertEqual(frappe.db.count("Stock Entry", {
			"work_order": wo.name, "purpose": "Manufacture", "docstatus": 1,
		}), 2)
