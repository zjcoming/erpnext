from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
from erpnext.stock.doctype.purchase_receipt.purchase_receipt import (
	make_purchase_return,
	make_purchase_return_against_rejected_warehouse,
)
from process_simplification.purchasing import receipts
from process_simplification.purchasing.receipt_quantities import (
	PurchaseReceiptAcceptedQuantityMixin,
	accepted_stock_qty,
	rejected_stock_qty,
)
from process_simplification.tests import test_material_coverage_integration as fixtures


class TestPurchaseRejectionQuantities(IntegrationTestCase):
	TEST_COMPANY = "Material Coverage Test Company"
	TEST_COMPANY_ABBR = "MCT"
	_ensure_company = classmethod(fixtures.TestMaterialCoverageIntegration._ensure_company.__func__)
	_make_item = fixtures.TestMaterialCoverageIntegration._make_item
	_make_warehouse = fixtures.TestMaterialCoverageIntegration._make_warehouse
	_make_purchase_order = fixtures.TestMaterialCoverageIntegration._make_purchase_order
	_make_material_request = fixtures.TestMaterialCoverageIntegration._make_material_request

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._ensure_company(cls.TEST_COMPANY, cls.TEST_COMPANY_ABBR)
		cls.supplier = "Purchase Rejection Quantity Test Supplier"
		if not frappe.db.exists("Supplier", cls.supplier):
			frappe.get_doc({
				"doctype": "Supplier", "supplier_name": cls.supplier,
				"supplier_group": "All Supplier Groups",
			}).insert()
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.warehouse = "Stores - MCT"
		self.enterContext(patch.object(receipts, "events_enabled", return_value=True))
		self.enterContext(patch.object(receipts, "process_notifications_enabled", return_value=False))

	def _flow(self, *, conversion=1, accepted=600, rejected=100):
		item = self._make_item("REJECTION", uoms=[{"uom": "Box", "conversion_factor": 10}])
		mr = self._make_material_request(
			item_code=item.name, warehouse=self.warehouse,
			schedule_date=add_days(nowdate(), 1), qty=700,
		)
		po = self._make_purchase_order(
			item_code=item.name, warehouse=self.warehouse,
			schedule_date=add_days(nowdate(), 1), qty=700 / conversion,
			uom="Box" if conversion == 10 else "Nos", conversion_factor=conversion,
			material_request=mr.name, material_request_item=mr.items[0].name,
		)
		pr = make_purchase_receipt(po.name)
		self.assertIsInstance(pr, PurchaseReceiptAcceptedQuantityMixin)
		pr.items[0].qty = accepted / conversion
		pr.items[0].rejected_qty = rejected / conversion
		pr.items[0].received_qty = (accepted + rejected) / conversion
		pr.items[0].rejected_warehouse = self._make_warehouse("Rejected", is_rejected_warehouse=1)
		pr.insert().submit()
		return mr, po, pr

	def _assert_progress(self, mr, accepted, *, status=None):
		mr.reload()
		self.assertEqual(mr.items[0].received_qty, accepted)
		self.assertAlmostEqual(mr.per_received, min(accepted / 700, 1) * 100, places=5)
		if status:
			self.assertEqual(mr.status, status)
		progress = receipts.request_progress([mr.name])[0]
		self.assertEqual(progress["items"][0].received_qty, accepted)
		self.assertEqual(progress["items"][0].remaining_qty, 700 - accepted)
		self.assertEqual(progress["complete"], accepted == 700)

	def _stock(self, pr, rejected=False):
		return frappe.db.get_value("Bin", {
			"item_code": pr.items[0].item_code,
			"warehouse": pr.items[0].rejected_warehouse if rejected else self.warehouse,
		}, "actual_qty") or 0

	def _event(self, pr, kind):
		return frappe.get_doc(receipts.EVENT, receipts.event_name(pr.name, kind))

	def test_rejected_return_replacement_and_cancellations(self):
		mr, po, pr = self._flow()
		self._assert_progress(mr, 600, status="Partially Received")
		ret = make_purchase_return_against_rejected_warehouse(pr.name)
		ret.insert().submit()
		self.assertEqual(ret.items[0].stock_qty, -100)
		self.assertEqual(accepted_stock_qty(ret.items[0]), 0)
		self.assertEqual(rejected_stock_qty(ret.items[0]), -100)
		self._assert_progress(mr, 600, status="Partially Received")
		self.assertEqual(self._stock(pr), 600)
		self.assertEqual(self._stock(pr, rejected=True), 0)
		self.assertEqual(po.reload().items[0].received_qty, 600)
		event = self._event(ret, "Return")
		self.assertEqual(json.loads(event.snapshot)["requests"][0]["items"][0]["received_qty"], 600)
		self.assertIn("退回合格 0 Nos，退回拒收 100 Nos", receipts._message(event)[1])

		# Cancellation restores rejected stock without adding usable stock.
		ret.cancel()
		self._assert_progress(mr, 600)
		self.assertEqual(self._stock(pr, rejected=True), 100)
		self.assertEqual(po.reload().items[0].received_qty, 700)
		self.assertEqual(json.loads(self._event(ret, "Return Cancelled").snapshot)["requests"][0]["items"][0]["received_qty"], 600)

		ret = make_purchase_return_against_rejected_warehouse(pr.name)
		ret.insert().submit()
		replacement = make_purchase_receipt(po.name)
		self.assertEqual(replacement.items[0].qty, 100)
		replacement.insert().submit()
		self._assert_progress(mr, 700, status="Received")
		self.assertEqual(self._stock(pr), 700)
		self.assertEqual(po.reload().per_received, 100)
		replacement.cancel()
		self._assert_progress(mr, 600, status="Partially Received")
		ret.cancel()
		pr.cancel()
		self._assert_progress(mr, 0)
		self.assertEqual(self._stock(pr), 0)
		self.assertEqual(self._stock(pr, rejected=True), 0)

	def test_accepted_only_return_reduces_usable_progress(self):
		mr, _, pr = self._flow()
		ret = make_purchase_return(pr.name)
		ret.items[0].qty = ret.items[0].received_qty = -50
		ret.items[0].rejected_qty = 0
		ret.insert().submit()
		self._assert_progress(mr, 550)
		self.assertEqual(self._stock(pr), 550)
		self.assertEqual(self._stock(pr, rejected=True), 100)
		self.assertIn("退回合格 50 Nos，退回拒收 0 Nos", receipts._message(self._event(ret, "Return"))[1])
		ret.cancel()
		self._assert_progress(mr, 600)

	def test_mixed_return_reduces_only_accepted_portion(self):
		from process_simplification.purchasing import allocation

		mr, po, pr = self._flow()
		ret = make_purchase_return(pr.name)
		ret.items[0].qty = -200
		ret.items[0].rejected_qty = -30
		ret.items[0].received_qty = -230
		ret.insert().submit()
		self._assert_progress(mr, 400)
		self.assertEqual(self._stock(pr), 400)
		self.assertEqual(self._stock(pr, rejected=True), 70)
		self.assertEqual(po.reload().items[0].received_qty, 470)
		self.assertEqual(allocation._order_followup(po)["items"][0]["returned_qty"], 230)
		self.assertIn("退回合格 200 Nos，退回拒收 30 Nos", receipts._message(self._event(ret, "Return"))[1])
		ret.cancel()
		self._assert_progress(mr, 600)

	def test_all_rejected_return_never_makes_progress_negative(self):
		mr, po, pr = self._flow(accepted=0, rejected=700)
		self._assert_progress(mr, 0)
		ret = make_purchase_return_against_rejected_warehouse(pr.name)
		ret.insert().submit()
		self._assert_progress(mr, 0)
		self.assertEqual(self._stock(pr), 0)
		self.assertEqual(self._stock(pr, rejected=True), 0)
		replacement = make_purchase_receipt(po.name)
		self.assertEqual(replacement.items[0].qty, 700)
		replacement.insert().submit()
		self._assert_progress(mr, 700, status="Received")

	def test_rejected_return_uses_stock_uom_and_rebuilds_old_wrong_progress(self):
		mr, po, pr = self._flow(conversion=10)
		ret = make_purchase_return_against_rejected_warehouse(pr.name)
		ret.insert().submit()
		self.assertEqual(ret.items[0].qty, -10)
		self.assertEqual(ret.items[0].stock_qty, -100)
		self._assert_progress(mr, 600)
		self.assertIn("退回合格 0 Box，退回拒收 10 Box", receipts._message(self._event(ret, "Return"))[1])
		# Simulate the pre-fix persisted MR quantity; the next native recalculation
		# uses the entire submitted history, not a compensating incremental delta.
		frappe.db.set_value("Material Request Item", mr.items[0].name, "received_qty", 500)
		replacement = make_purchase_receipt(po.name)
		self.assertEqual(replacement.items[0].qty, 10)
		replacement.insert().submit()
		self._assert_progress(mr, 700, status="Received")

	def test_rejection_recipients_union_and_delivery_rechecks_responsibility(self):
		users = [frappe.get_doc({
			"doctype": "User", "email": f"rejection-{frappe.generate_hash(length=10)}@example.com",
			"first_name": label, "send_welcome_email": 0, "user_type": "System User",
			"roles": [{"role": "Stock User"}],
		}).insert().name for label in ("Receipt Test", "Warehouse Test")]
		eligible = {
			receipts.PURCHASE_RECEIPT_RESPONSIBILITY: [users[0]],
			receipts.WAREHOUSE_RESPONSIBILITY: [users[0], users[1]],
		}
		with (
			patch.object(receipts, "responsibility_recipients", side_effect=lambda company, responsibility: eligible[responsibility]),
			patch.object(receipts, "process_notifications_enabled", return_value=True),
			patch.object(receipts, "enqueue_event"),
			patch.object(receipts, "is_notifications_enabled", return_value=True),
		):
			self.assertEqual(receipts._receipt_recipients(self.TEST_COMPANY, [{"qty": 700, "rejected_qty": 0}]), [users[0]])
			self.assertEqual(set(receipts._receipt_recipients(self.TEST_COMPANY, [{"return_qty_from_rejected_warehouse": 1}])), set(users))
			_, _, pr = self._flow()
			event = self._event(pr, "Arrival")
			self.assertEqual({row.recipient for row in event.deliveries}, set(users))
			notice = receipts.get_notice(event.name)
			self.assertEqual(notice["rejection_source_receipt"], pr.name)
			self.assertIn("purchase-rejection-followup?receipt=", notice["rejection_followup_url"])
			# A saved delivery recipient must still hold the relevant responsibility.
			eligible[receipts.WAREHOUSE_RESPONSIBILITY] = []
			receipts.deliver_event(event.name)
			event.reload()
			self.assertEqual({row.recipient: row.status for row in event.deliveries}, {
				users[0]: "Delivered", users[1]: "Skipped",
			})
			ret = make_purchase_return_against_rejected_warehouse(pr.name)
			ret.insert().submit()
			self.assertEqual(receipts.get_notice(self._event(ret, "Return").name)["rejection_source_receipt"], pr.name)
