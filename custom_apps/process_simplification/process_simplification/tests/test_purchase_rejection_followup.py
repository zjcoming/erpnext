from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate

from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
from process_simplification.purchasing import allocation, rejections
from process_simplification.tests import test_purchase_rejection_quantities as quantity_tests


class TestPurchaseRejectionFollowup(quantity_tests.TestPurchaseRejectionQuantities):
	def _balance(self, mr):
		return allocation.coverage(mr.name)[0][mr.items[0].name]

	def _pending_names(self):
		result = rejections.get_page(company=self.TEST_COMPANY)
		return [row["name"] for row in result["rows"]]

	def test_rejection_is_separate_from_delivery_and_cannot_be_ordered_twice(self):
		mr, po, pr = self._flow()
		balance = self._balance(mr)
		self.assertEqual(balance.received_qty, 600)
		self.assertEqual(balance.ordered_pending_qty, 0)
		self.assertEqual(balance.rejected_pending_qty, 100)
		self.assertEqual(balance.occupied_qty, 700)
		self.assertIn(pr.name, self._pending_names())
		row = rejections.get_context(pr.name)["items"][0]
		self.assertEqual((row["accepted_qty"], row["rejected_qty"], row["pending_return_qty"]), (600, 100, 100))
		self.assertEqual(row["order_pending_qty"], 0)
		self.assertEqual(po.reload().per_received, 100)
		with self.assertRaises(frappe.ValidationError):
			allocation._check_available(mr, {"quantities": {mr.items[0].name: 100}})

	def test_partial_return_then_replacement_moves_between_real_queues(self):
		mr, po, pr = self._flow()
		first = rejections.make_rejected_return(pr.name)
		first.items[0].qty = first.items[0].received_qty = -40
		first.insert().submit()
		row = rejections.get_context(pr.name)["items"][0]
		self.assertEqual((row["returned_rejected_qty"], row["pending_return_qty"], row["order_pending_qty"]), (40, 60, 40))
		balance = self._balance(mr)
		self.assertEqual((balance.received_qty, balance.ordered_pending_qty, balance.rejected_pending_qty), (600, 40, 60))
		second = rejections.make_rejected_return(pr.name)
		self.assertEqual(second.items[0].qty, -60)
		second.insert().submit()
		self.assertNotIn(pr.name, self._pending_names())
		self._assert_progress(mr, 600)
		self.assertTrue(allocation._order_followup(po.reload())["can_receive"])
		with self.assertRaises(frappe.ValidationError):
			rejections.make_rejected_return(pr.name)
		replacement = make_purchase_receipt(po.name)
		self.assertEqual(replacement.items[0].qty, 100)
		replacement.insert().submit()
		self._assert_progress(mr, 700, status="Received")
		self.assertEqual(self._stock(pr), 700)
		self.assertFalse(allocation._order_followup(po.reload())["can_receive"])
		self.assertEqual(self._balance(mr).rejected_pending_qty, 0)

	def test_return_draft_does_not_clear_queue_or_post_stock(self):
		_, _, pr = self._flow()
		before = frappe.db.count("Stock Ledger Entry")
		draft = rejections.make_rejected_return(pr.name).insert()
		context = rejections.get_context(pr.name)
		self.assertEqual(context["return_drafts"], [draft.name])
		self.assertEqual(context["items"][0]["pending_return_qty"], 100)
		self.assertIn(pr.name, self._pending_names())
		self.assertEqual(frappe.db.count("Stock Ledger Entry"), before)
		with self.assertRaises(frappe.ValidationError):
			rejections.make_rejected_return(pr.name)

	def test_cancel_return_reopens_rejection_action_without_changing_accepted_stock(self):
		mr, _, pr = self._flow()
		ret = rejections.make_rejected_return(pr.name).insert()
		ret.submit()
		self.assertNotIn(pr.name, self._pending_names())
		ret.cancel()
		self.assertIn(pr.name, self._pending_names())
		self._assert_progress(mr, 600)
		self.assertEqual(self._stock(pr), 600)
		self.assertEqual(self._balance(mr).rejected_pending_qty, 100)

	def test_close_supplier_order_allows_same_request_repurchase_without_hiding_rejects(self):
		mr, po, pr = self._flow()
		po.update_status("Closed")
		self.assertEqual(mr.reload().items[0].ordered_qty, 600)
		balance = self._balance(mr)
		self.assertEqual((balance.received_qty, balance.ordered_pending_qty, balance.rejected_pending_qty), (600, 0, 0))
		self.assertEqual(allocation.get_allocation_context(mr.name)["items"][0]["available_qty"], 100)
		self.assertIn(pr.name, self._pending_names())
		ret = rejections.make_rejected_return(pr.name).insert()
		ret.submit()
		self.assertEqual(po.reload().status, "Closed")
		other_supplier = frappe.get_doc({
			"doctype": "Supplier", "supplier_name": "Replacement Supplier " + frappe.generate_hash(length=8),
			"supplier_group": "All Supplier Groups",
		}).insert()
		result = allocation.create_purchase_orders(mr.name, frappe.generate_hash(), {
			"company": self.TEST_COMPANY, "quantities": {mr.items[0].name: 100},
			"rows": [{"material_request_item": mr.items[0].name,
				"supplier": other_supplier.name, "qty": 100, "uom": "Nos", "rate": 5,
				"currency": "INR", "warehouse": self.warehouse,
				"schedule_date": add_days(nowdate(), 1)}],
		})
		new_po = frappe.get_doc("Purchase Order", result["orders"][0]["name"])
		self.assertNotEqual(new_po.supplier, po.supplier)
		self.assertEqual(new_po.supplier, other_supplier.name)
		self.assertEqual(new_po.items[0].material_request_item, mr.items[0].name)
		new_po.submit()
		self.assertEqual(mr.reload().items[0].ordered_qty, 700)
		# Reopening the first supplier's commitment would order this gap twice.
		frappe.db.savepoint("reopen_replaced_supplier")
		with self.assertRaises(frappe.ValidationError):
			po.reload().update_status("To Receive and Bill")
		frappe.db.rollback(save_point="reopen_replaced_supplier")
		self.assertEqual(po.reload().status, "Closed")
		make_purchase_receipt(new_po.name).insert().submit()
		self._assert_progress(mr, 700, status="Received")
		self.assertNotIn(pr.name, self._pending_names())
		self.assertEqual(self._stock(pr), 700)

	def test_closed_order_accepted_return_and_cancellation_refresh_retained_supply(self):
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_return

		mr, po, pr = self._flow()
		po.update_status("Closed")
		ret = make_purchase_return(pr.name)
		ret.items[0].qty = ret.items[0].received_qty = -50
		ret.items[0].rejected_qty = 0
		ret.insert().submit()
		self._assert_progress(mr, 550)
		self.assertEqual(mr.reload().items[0].ordered_qty, 550)
		self.assertEqual(allocation.get_allocation_context(mr.name)["items"][0]["available_qty"], 150)
		ret.cancel()
		self._assert_progress(mr, 600)
		self.assertEqual(mr.reload().items[0].ordered_qty, 600)
		self.assertEqual(po.reload().status, "Closed")

	def test_real_bom_shortage_excludes_rejected_stock_and_tracks_replacement_supply(self):
		from process_simplification.api.shortage import calculate_material_coverage

		_, po, pr = self._flow()
		finished = self._make_item("REJECTION-FG")
		bom = frappe.get_doc({
			"doctype": "BOM", "item": finished.name, "company": self.TEST_COMPANY,
			"currency": "INR", "quantity": 1, "is_active": 1, "is_default": 1,
			"items": [{"item_code": pr.items[0].item_code, "qty": 7, "uom": "Nos", "rate": 5}],
		}).insert()
		bom.submit()

		def coverage():
			result = calculate_material_coverage(
				[{"bom_no": bom.name, "qty": 100}], self.TEST_COMPANY,
				need_by_date=add_days(nowdate(), 3),
				defaults=frappe._dict(source_warehouse=self.warehouse),
			)
			self.assertEqual(len(result.materials), 1)
			return result.materials[0]

		# Real BOM needs 700. Rejected warehouse stock cannot make the 600 usable
		# units appear sufficient, and the fully delivered PO is not fake inbound.
		row = coverage()
		self.assertEqual((row["required_qty"], row["actual_qty"], row["available_qty"]), (700, 600, 600))
		self.assertEqual((row["current_gap_qty"], row["shortage_qty"]), (100, 100))
		self.assertEqual(row["open_purchase_order_qty"], 0)
		self.assertEqual(row["status"], "new_purchase_required")
		self.assertEqual(self._stock(pr, rejected=True), 100)

		rejections.make_rejected_return(pr.name).insert().submit()
		row = coverage()
		self.assertEqual((row["actual_qty"], row["current_gap_qty"]), (600, 100))
		self.assertEqual((row["open_purchase_order_qty"], row["shortage_qty"]), (100, 0))
		self.assertEqual(row["status"], "awaiting_purchase_receipt")

		make_purchase_receipt(po.name).insert().submit()
		row = coverage()
		self.assertEqual((row["actual_qty"], row["available_qty"], row["current_gap_qty"]), (700, 700, 0))
		self.assertEqual((row["open_purchase_order_qty"], row["shortage_qty"]), (0, 0))
		self.assertEqual(row["status"], "ready_now")

	def test_other_request_receipt_changes_inventory_without_rewriting_source_progress(self):
		mr, _, pr = self._flow()
		other = self._make_material_request(item_code=pr.items[0].item_code, warehouse=self.warehouse,
			schedule_date=add_days(nowdate(), 1), qty=100)
		po = self._make_purchase_order(item_code=pr.items[0].item_code, warehouse=self.warehouse,
			schedule_date=add_days(nowdate(), 1), qty=100,
			material_request=other.name, material_request_item=other.items[0].name)
		make_purchase_receipt(po.name).insert().submit()
		self.assertEqual(self._stock(pr), 700)
		self._assert_progress(mr, 600)
		self.assertEqual(other.reload().status, "Received")
		self.assertIn(pr.name, self._pending_names())

	def test_stock_unit_followup_for_box_purchases(self):
		_, _, pr = self._flow(conversion=10)
		ret = rejections.make_rejected_return(pr.name)
		ret.items[0].qty = ret.items[0].received_qty = -4
		ret.insert().submit()
		row = rejections.get_context(pr.name)["items"][0]
		self.assertEqual(row["stock_uom"], "Nos")
		self.assertEqual((row["rejected_qty"], row["returned_rejected_qty"], row["pending_return_qty"], row["order_pending_qty"]), (100, 40, 60, 40))

	def test_company_boundary_is_checked_before_listing_or_mapping(self):
		_, _, pr = self._flow()
		with patch.object(rejections, "_user_matches_company", return_value=False):
			with self.assertRaises(frappe.PermissionError):
				rejections.get_page(company=self.TEST_COMPANY)
			with self.assertRaises(frappe.PermissionError):
				rejections.get_context(pr.name)
			with self.assertRaises(frappe.PermissionError):
				rejections.make_rejected_return(pr.name)

	def test_real_warehouse_operator_needs_company_and_rejected_warehouse_access(self):
		from process_simplification.management_access import (
			ROLE_DEFINITION_BY_ROLE,
			WAREHOUSE_OPERATOR_ROLE,
			ensure_management_access,
		)

		_, _, pr = self._flow()
		ensure_management_access()
		self._ensure_company("Material Coverage Other Company", "MCO")
		user = frappe.get_doc({
			"doctype": "User", "email": f"reject-operator-{frappe.generate_hash(length=8)}@example.com",
			"first_name": "Rejection Warehouse Operator", "send_welcome_email": 0,
			"role_profiles": [{"role_profile": ROLE_DEFINITION_BY_ROLE[WAREHOUSE_OPERATOR_ROLE]["profile"]}],
		}).insert()
		for doctype, name in (("Company", self.TEST_COMPANY), ("Warehouse", self.warehouse)):
			frappe.get_doc({
				"doctype": "User Permission", "user": user.name, "allow": doctype,
				"for_value": name, "apply_to_all_doctypes": 1,
			}).insert()
		try:
			frappe.set_user(user.name)
			self.assertIn(WAREHOUSE_OPERATOR_ROLE, frappe.get_roles())
			with self.assertRaises(frappe.PermissionError):
				rejections.get_page(company="Material Coverage Other Company")
			self.assertNotIn(pr.name, self._pending_names())
			with self.assertRaises(frappe.PermissionError):
				rejections.get_context(pr.name)
			with self.assertRaises(frappe.PermissionError):
				rejections.make_rejected_return(pr.name)

			frappe.set_user("Administrator")
			frappe.get_doc({
				"doctype": "User Permission", "user": user.name, "allow": "Warehouse",
				"for_value": pr.items[0].rejected_warehouse, "apply_to_all_doctypes": 1,
			}).insert()
			frappe.clear_cache(user=user.name)
			frappe.set_user(user.name)
			self.assertIn(pr.name, self._pending_names())
			self.assertTrue(rejections.get_context(pr.name)["can_return"])
			mapped = rejections.make_rejected_return(pr.name)
			self.assertEqual(mapped.items[0].warehouse, pr.items[0].rejected_warehouse)
			self.assertEqual(mapped.items[0].qty, -100)
		finally:
			frappe.set_user("Administrator")

	def test_closed_or_cancelled_source_does_not_invent_a_return_action(self):
		_, _, pr = self._flow()
		pr.cancel()
		self.assertNotIn(pr.name, self._pending_names())
		with self.assertRaises(frappe.ValidationError):
			rejections.make_rejected_return(pr.name)
