"""Restricted-role closure with real stock and native SRE lifecycle; isolated site only."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime, nowdate, nowtime

from erpnext.manufacturing.doctype.work_order.work_order import stop_unstop
from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
	StockReservationEntry,
	get_available_qty_to_reserve,
)
from process_simplification.management_access import (
	OWNER_ROLE,
	PRODUCTION_MANAGER_ROLE,
	ensure_management_access,
)
from process_simplification.production_workflow.work_order_close import close_work_order
from process_simplification.production_workflow.stock_reservation import _CLOSE_SRE_VOUCHER_FLAG
from process_simplification.tests import test_worker_reporting as reporting_fixtures


class TestWorkOrderCloseIntegration(IntegrationTestCase):
	TEST_COMPANY = reporting_fixtures.TestWorkerReporting.TEST_COMPANY
	TEST_COMPANY_ABBR = reporting_fixtures.TestWorkerReporting.TEST_COMPANY_ABBR
	OTHER_COMPANY = reporting_fixtures.TestWorkerReporting.OTHER_COMPANY
	OTHER_COMPANY_ABBR = reporting_fixtures.TestWorkerReporting.OTHER_COMPANY_ABBR
	_ensure_company = classmethod(reporting_fixtures.TestWorkerReporting._ensure_company.__func__)
	_make_user = reporting_fixtures.TestWorkerReporting._make_user
	_email = reporting_fixtures.TestWorkerReporting._email
	_grant_company = reporting_fixtures.TestWorkerReporting._grant_company

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
		self.enterContext(self.change_settings("Stock Settings", {"enable_stock_reservation": 1}))
		self.users = {}
		for role in (OWNER_ROLE, PRODUCTION_MANAGER_ROLE):
			user = self._make_user(role)
			self._grant_company(user, self.TEST_COMPANY)
			self.users[role] = user

	def _rollback(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _reserved_work_order(self, company=None):
		company = company or self.TEST_COMPANY
		with self.set_user("Administrator"):
			prefix = "CLOSE-UAT-" + frappe.generate_hash(length=8)
			warehouse = frappe.get_doc(dict(doctype="Warehouse", warehouse_name=prefix,
				company=company)).insert().name
			items = []
			for suffix in ("RM", "FG"):
				items.append(frappe.get_doc(dict(doctype="Item", item_code=prefix + "-" + suffix,
					item_name=prefix + "-" + suffix, item_group="Products", stock_uom="Nos",
					is_stock_item=1, is_purchase_item=1, include_item_in_manufacturing=1,
					valuation_rate=10)).insert().name)
			defaults = frappe.db.get_value("Company", company,
				["stock_adjustment_account", "cost_center"], as_dict=True)
			receipt = frappe.get_doc(dict(doctype="Stock Entry", company=company,
				stock_entry_type="Material Receipt", purpose="Material Receipt",
				posting_date=nowdate(), posting_time=nowtime(), set_posting_time=1,
				items=[dict(item_code=items[0], t_warehouse=warehouse, qty=4,
					uom="Nos", stock_uom="Nos", conversion_factor=1, transfer_qty=4, basic_rate=10,
					cost_center=defaults.cost_center, expense_account=defaults.stock_adjustment_account)]))
			receipt.insert().submit()
			bom = frappe.get_doc(dict(doctype="BOM", item=items[1], company=company,
				currency="INR", quantity=1, is_active=1, is_default=1,
				items=[dict(item_code=items[0], qty=1, uom="Nos", stock_uom="Nos",
					rate=10, source_warehouse=warehouse)]))
			bom.insert().submit()
			wo = frappe.get_doc(dict(doctype="Work Order", company=company,
				production_item=items[1], bom_no=bom.name, qty=2, stock_uom="Nos",
				source_warehouse=warehouse, wip_warehouse=warehouse, fg_warehouse=warehouse,
				reserve_stock=1, transfer_material_against="Work Order", planned_start_date=now_datetime()))
			wo.get_items_and_operations_from_bom()
			for row in wo.required_items:
				row.source_warehouse = warehouse
			wo.insert().submit()
			names = frappe.get_all("Stock Reservation Entry", filters=dict(
				voucher_type="Work Order", voucher_no=wo.name, docstatus=1), pluck="name")
			self.assertEqual(len(names), 1)
			sre = frappe.get_doc("Stock Reservation Entry", names[0])
			self.assertEqual(sre.reserved_qty, 2)
			return wo, sre, items[0], warehouse

	def _snapshot(self, wo, sre, item, warehouse):
		return {
			"work_order": frappe.db.get_value("Work Order", wo.name, ["docstatus", "status"], as_dict=True),
			"reservation": frappe.db.get_value("Stock Reservation Entry", sre.name,
				["docstatus", "status", "reserved_qty", "modified_by"], as_dict=True),
			"bin": frappe.db.get_value("Bin", dict(item_code=item, warehouse=warehouse),
				["actual_qty", "reserved_stock"], as_dict=True),
			"ledger": frappe.get_all("Stock Ledger Entry", filters=dict(item_code=item, warehouse=warehouse),
				fields=["name", "actual_qty", "qty_after_transaction"], order_by="name"),
			"available": get_available_qty_to_reserve(item, warehouse),
		}

	def test_both_app_manager_roles_release_real_reservations_with_native_audit(self):
		for role, user in self.users.items():
			with self.subTest(role=role):
				wo, sre, item, warehouse = self._reserved_work_order()
				before = self._snapshot(wo, sre, item, warehouse)
				self.assertEqual(before["available"], 2)
				with self.set_user(user):
					self.assertTrue(frappe.has_permission("Work Order", "write", doc=wo))
					self.assertFalse(frappe.has_permission("Stock Reservation Entry", "cancel", doc=sre))
					self.assertEqual(close_work_order(wo.name, "Closed"), "Closed")
					self.assertFalse(frappe.has_permission("Stock Reservation Entry", "cancel", doc=sre))
					self.assertNotIn(_CLOSE_SRE_VOUCHER_FLAG, frappe.flags)
				after = self._snapshot(wo, sre, item, warehouse)
				self.assertEqual(after["work_order"].status, "Closed")
				self.assertEqual(after["reservation"].docstatus, 2)
				self.assertEqual(after["reservation"].status, "Cancelled")
				self.assertEqual(after["reservation"].modified_by, user)
				self.assertEqual(after["bin"].actual_qty, before["bin"].actual_qty)
				self.assertEqual(after["bin"].reserved_stock, 0)
				self.assertEqual(after["available"], 4)
				self.assertEqual(after["ledger"], before["ledger"])
				with self.set_user(user):
					self.assertEqual(close_work_order(wo.name, "Closed"), "Closed")
				self.assertEqual(self._snapshot(wo, sre, item, warehouse), after)

	def test_pause_keeps_reservation_and_direct_cancel_still_fails(self):
		wo, sre, item, warehouse = self._reserved_work_order()
		user = self.users[PRODUCTION_MANAGER_ROLE]
		with self.set_user(user):
			stop_unstop(wo.name, "Stopped")
			self.assertEqual(frappe.db.get_value("Work Order", wo.name, "status"), "Stopped")
			self.assertEqual(frappe.db.get_value("Stock Reservation Entry", sre.name, "docstatus"), 1)
			frappe.db.savepoint("direct_cancel_denied")
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc("Stock Reservation Entry", sre.name).cancel()
			frappe.db.rollback(save_point="direct_cancel_denied")
		self.assertEqual(self._snapshot(wo, sre, item, warehouse)["available"], 2)
		with self.set_user(user):
			self.assertEqual(close_work_order(wo.name, "Closed"), "Closed")

	def test_foreign_company_and_other_work_orders_remain_untouched(self):
		own = self._reserved_work_order()
		other = self._reserved_work_order()
		foreign = self._reserved_work_order(self.OTHER_COMPANY)
		other_before, foreign_before = self._snapshot(*other), self._snapshot(*foreign)
		with self.set_user(self.users[OWNER_ROLE]):
			with self.assertRaises(frappe.PermissionError):
				close_work_order(foreign[0].name, "Closed")
			self.assertEqual(close_work_order(own[0].name, "Closed"), "Closed")
		self.assertEqual(self._snapshot(*other), other_before)
		self.assertEqual(self._snapshot(*foreign), foreign_before)

	def test_failure_after_native_cancel_rolls_back_the_whole_close_transaction(self):
		case = self._reserved_work_order()
		before = self._snapshot(*case)
		native_cancel = StockReservationEntry.on_cancel
		def fail_after_native(doc):
			native_cancel(doc)
			frappe.throw("Injected failure after native SRE and Bin updates")
		frappe.db.savepoint("close_failure")
		with self.set_user(self.users[PRODUCTION_MANAGER_ROLE]), \
			patch.object(StockReservationEntry, "on_cancel", fail_after_native):
			with self.assertRaisesRegex(frappe.ValidationError, "Injected failure"):
				close_work_order(case[0].name, "Closed")
			self.assertNotIn(_CLOSE_SRE_VOUCHER_FLAG, frappe.flags)
		# The HTTP request owns rollback; no transaction is committed by the service.
		frappe.db.rollback(save_point="close_failure")
		self.assertEqual(self._snapshot(*case), before)
