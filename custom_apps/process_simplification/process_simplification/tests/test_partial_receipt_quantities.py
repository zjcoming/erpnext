"""Partial receipts must use submitted operation quantities, not card status/counts."""

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from process_simplification.api.production_readiness import (
	attach_work_order_execution_facts,
	derive_work_order_flow_state,
)
from process_simplification.production_workflow.receipt import operation_receipt_remaining_qty
from process_simplification.production_workflow.service import receipt_remaining_qty, request_manufacture


class TestPartialReceiptQuantities(UnitTestCase):
	def test_each_operation_must_cover_the_receipt(self):
		wo = frappe._dict(qty=4, produced_qty=0, operations=[
			frappe._dict(completed_qty=4), frappe._dict(completed_qty=2)])
		self.assertEqual(operation_receipt_remaining_qty(wo), 2)
		wo.produced_qty = 2
		self.assertEqual(operation_receipt_remaining_qty(wo), 0)
		wo.operations[1].completed_qty = 4
		self.assertEqual(operation_receipt_remaining_qty(wo), 2)

	def test_pending_quantity_and_draft_card_totals_are_not_output(self):
		wo = frappe._dict(qty=4, job_card_count=1, completed_job_card_count=0,
			operations=[frappe._dict(completed_qty=0, pending_qty=4)])
		self.assertEqual(operation_receipt_remaining_qty(wo), 0)
		wo.operations = None
		wo.completed_job_card_count = 1
		self.assertEqual(operation_receipt_remaining_qty(wo), 0)

	def test_process_loss_is_booked_once_and_never_creates_overproduction(self):
		wo = frappe._dict(qty=5, produced_qty=2, process_loss_qty=1,
			operations=[frappe._dict(completed_qty=4, process_loss_qty=1)])
		self.assertEqual(operation_receipt_remaining_qty(wo), 2)
		wo.produced_qty = 4
		self.assertEqual(operation_receipt_remaining_qty(wo), 0)

	def test_net_material_and_operation_both_limit_receipts(self):
		wo = frappe._dict(qty=4, produced_qty=0, operations=[frappe._dict(completed_qty=3)],
			required_items=[frappe._dict(item_code="RM", source_warehouse="Stores",
				required_qty=4, transferred_qty=4, returned_qty=2, include_item_in_manufacturing=1)])
		self.assertEqual(receipt_remaining_qty(wo), 2)
		wo.operations[0].completed_qty = 1
		self.assertEqual(receipt_remaining_qty(wo), 1)

	def test_workbench_offers_only_native_completed_batch_while_other_card_is_draft(self):
		wo = frappe._dict(name="WO-PARTIAL", qty=4, status="In Process", skip_transfer=1)
		attach_work_order_execution_facts([wo], [
			frappe._dict(name="JC-FIRST", work_order=wo.name, docstatus=1, status="Completed"),
			frappe._dict(name="JC-NEXT", work_order=wo.name, docstatus=0, status="Open"),
		], [], [frappe._dict(name="OP-1", parent=wo.name, completed_qty=2, process_loss_qty=0)])
		state = derive_work_order_flow_state(wo)
		self.assertEqual(state["operation_state"].code, "in_progress")
		self.assertEqual(state["receipt_state"].remaining_qty, 2)
		self.assertTrue(state["can_request_receipt"])
		wo.produced_qty = 2
		self.assertFalse(derive_work_order_flow_state(wo)["can_request_receipt"])

	def test_request_rejects_excess_nonfinite_and_unfinished_quantities(self):
		wo = frappe._dict(name="WO", qty=4, docstatus=1, status="In Process",
			skip_transfer=1, operations=[frappe._dict(completed_qty=2)])
		with patch("process_simplification.production_workflow.service._work_order_for_action", return_value=wo), \
			patch("process_simplification.production_workflow.service._existing_draft_stock_entry", return_value=None), \
			patch("process_simplification.production_workflow.service.load_work_order_stock_facts", return_value={}), \
			patch("process_simplification.production_workflow.service._insert_guided_stock_entry") as insert:
			for qty in (0, -1, 3, "nan", "inf"):
				with self.subTest(qty=qty), self.assertRaises(frappe.ValidationError):
					request_manufacture("WO", qty=qty)
			insert.assert_not_called()

	def test_existing_request_with_other_quantity_is_not_silently_reused(self):
		with patch("process_simplification.production_workflow.service._work_order_for_action",
			return_value=frappe._dict(name="WO")), \
			patch("process_simplification.production_workflow.service._existing_draft_stock_entry",
			return_value=frappe._dict(name="STE", fg_completed_qty=2)):
			with self.assertRaisesRegex(frappe.ValidationError, "不同数量"):
				request_manufacture("WO", qty=1)
