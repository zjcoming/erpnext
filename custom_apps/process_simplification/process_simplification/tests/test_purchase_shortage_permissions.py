"""Purchase-only projection, real native scopes and write-time shortage checks."""

from copy import deepcopy
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from process_simplification.api import shortage
from process_simplification.api.utils import SimplifiedFlowError
from process_simplification.tests import test_planning_stock_integration as planning_fixture
from process_simplification.tests.test_replenishment_chain_integration import ManufacturingStockFixture


class TestPurchaseShortageValidation(UnitTestCase):
	def test_duplicate_material_rows_cannot_purchase_the_same_gap_twice(self):
		row = {"item_code": "RM", "warehouse": "Stores", "purchase_qty": 3,
			"shortage_qty": 3, "sources": [{"sales_order": "SO", "sales_order_item": "SOI", "shortage_qty": 3}]}
		with self.assertRaises(SimplifiedFlowError):
			shortage.revalidate_purchase_rows([row, deepcopy(row)], [row])

	def test_source_notes_are_rebuilt_from_current_facts(self):
		current = {"item_code": "RM", "warehouse": "Stores", "shortage_qty": 3,
			"sources": [{"sales_order": "REAL-SO", "sales_order_item": "SOI", "shortage_qty": 3}]}
		request = deepcopy(current)
		request["sources"][0]["sales_order"] = "FOREIGN-SO"
		validated = shortage.revalidate_purchase_rows([request], [current])
		self.assertEqual(validated[0].sources, current["sources"])
		request["sources"][0]["sales_order_item"] = "FOREIGN-SOI"
		with self.assertRaises(SimplifiedFlowError):
			shortage.revalidate_purchase_rows([request], [current])

	def test_purchase_readiness_query_cannot_escape_company(self):
		from process_simplification.api import production_readiness

		def exercise(company, *, query):
			for doctype in ("Sales Order", "Work Order", "Production Plan"):
				query(doctype, filters={"company": "FOREIGN", "name": ["in", ["REF"]]}, fields=["name"])
			return {}

		with patch.object(shortage, "_require_purchase_company") as require, \
			patch.object(production_readiness, "_get_production_plan_readiness", side_effect=exercise), \
			patch.object(frappe, "get_all", return_value=[]) as get_all:
			production_readiness._get_purchase_plan_readiness("ALLOWED")
		require.assert_called_once_with("ALLOWED")
		self.assertEqual(len(get_all.call_args_list), 3)
		for call in get_all.call_args_list:
			self.assertEqual(call.kwargs["filters"]["company"], "ALLOWED")


class TestPurchaseShortagePermissions(ManufacturingStockFixture):
	_sales_order = planning_fixture.TestPlanningStockIntegration._sales_order
	_plan = planning_fixture.TestPlanningStockIntegration._plan
	_stock = planning_fixture.TestPlanningStockIntegration._stock

	def setUp(self):
		# Reuse the isolated site's configured company; every scenario creates
		# uniquely named documents and rolls them back.
		self.fixture_company = frappe.get_all("Company", pluck="name", limit=1)[0]
		super().setUp()
		frappe.db.set_single_value("Stock Settings", "default_warehouse", self.stores)
		frappe.db.set_value("Company", self.company, "custom_default_semi_finished_warehouse", self.stores)
		frappe.clear_document_cache("Company", self.company)
		self.buyer = self._buyer()
		self._grant("Company", self.company)
		self._grant("Warehouse", self.stores)
		self.addCleanup(frappe.set_user, "Administrator")

	def _buyer(self):
		user = frappe.get_doc({"doctype": "User", "email": frappe.generate_hash(length=12) + "@example.test",
			"first_name": "Purchase scope test", "send_welcome_email": 0,
			"roles": [{"role": "Purchase User"}]}).insert().name
		self.addCleanup(frappe.clear_cache, user=user)
		return user

	def _grant(self, doctype, value, **kwargs):
		frappe.get_doc({"doctype": "User Permission", "user": self.buyer,
			"allow": doctype, "for_value": value, **kwargs}).insert()
		frappe.clear_cache(user=self.buyer)

	def _own_shortages(self, result):
		return [row for row in result if row.get("item_code") in self.items]

	def _assert_matches_admin(self):
		from process_simplification.api.production import get_production_overview

		frappe.set_user("Administrator")
		legacy_demands = get_production_overview(page_size=0)["demands"]
		expected = self._own_shortages(shortage.calculate_company_purchase_shortages(
			self.company, production_demands=legacy_demands,
		))
		frappe.set_user(self.buyer)
		self.assertFalse(frappe.has_permission("Sales Order", "read"))
		self.assertFalse(frappe.has_permission("Work Order", "read"))
		actual = self._own_shortages(shortage.check_all_shortages(self.company)["shortages"])
		self.assertEqual(actual, expected)
		for row in actual:
			self.assertFalse({"customer", "customer_name", "rate", "amount", "grand_total"}.intersection(row))
		return actual

	def test_planned_and_unplanned_shared_stock_mr_and_po_match_admin_and_create_once(self):
		first = self._sales_order(3)
		self._plan(3, first)
		second = self._sales_order(4)
		rows = self._assert_matches_admin()
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["shortage_qty"], 5, "7 raw demand minus 2 physical stock")
		self.assertEqual({row["sales_order"] for row in rows[0]["sources"]}, {first.name, second.name})
		# Existing methods commit under a company lock; prevent commits on this
		# rollback-only test site while exercising native buyer insert and submit.
		with patch.object(frappe.db, "commit"):
			created = shortage.create_material_request(rows, company=self.company)
			self.assertEqual(created["docstatus"], 1)
			self.assertEqual(frappe.db.get_value("Material Request", created["material_request"], "owner"), self.buyer)
			with self.assertRaises(SimplifiedFlowError):
				shortage.create_material_request(rows, company=self.company)
		self.assertEqual(self._assert_matches_admin(), [])
		frappe.set_user("Administrator")
		# Move part of this submitted MR to a native PO: the same supply must not
		# be counted both as an unconverted MR and as inbound PO quantity.
		from erpnext.stock.doctype.material_request.material_request import make_purchase_order

		supplier = frappe.get_doc({"doctype": "Supplier", "supplier_name": self.prefix,
			"supplier_type": "Company", "supplier_group": "All Supplier Groups"}).insert()
		po = make_purchase_order(created["material_request"])
		po.supplier = supplier.name
		po.items[0].qty = 2
		po.insert().submit()
		self.assertEqual(self._assert_matches_admin(), [])

	def test_company_warehouse_and_selected_source_scopes_use_native_permissions(self):
		order = self._sales_order(5)
		no_scope_buyer = self._buyer()
		frappe.set_user(no_scope_buyer)
		with self.assertRaises(frappe.PermissionError):
			shortage.check_all_shortages(self.company)
		frappe.set_user(self.buyer)
		with self.assertRaises(frappe.PermissionError):
			shortage.check_all_shortages("OUTSIDE-COMPANY")
		self.assertFalse(shortage._can_read_purchase_material(self.company, {
			"item_code": self.items[0], "warehouse": self.wip,
		}))
		self.assertTrue(shortage._can_read_purchase_material(self.company, {
			"item_code": self.items[0], "warehouse": self.stores,
		}))
		selected = [{"sales_order": order.name, "sales_order_item": order.items[0].name}]
		self.assertEqual(shortage.check_shortage(selected, self.company)["shortages"][0]["shortage_qty"], 3)
		for foreign in [{"sales_order": "FOREIGN-SO", "sales_order_item": order.items[0].name},
			{"sales_order": order.name, "sales_order_item": "FOREIGN-SOI"}]:
			with self.assertRaises(SimplifiedFlowError):
				shortage.check_shortage([foreign], self.company)
		rows = shortage.check_all_shortages(self.company)["shortages"]
		rows = self._own_shortages(rows)
		rows[0]["warehouse"] = self.wip
		with patch.object(frappe, "new_doc") as new_doc:
			with self.assertRaises(frappe.PermissionError):
				shortage.create_material_request(rows, self.company)
			new_doc.assert_not_called()

	def test_finished_reservations_and_completed_production_do_not_repurchase(self):
		from process_simplification.api.actions import reserve_stock

		order = self._sales_order(3)
		self._stock(self.items[-1], 3)
		reserve_stock(order.name, order.items[0].name, qty=3)
		self.assertEqual(self._assert_matches_admin(), [])
		frappe.set_user("Administrator")
		other = self._sales_order(2)
		_, orders = self._plan(2, other)
		for item in self.items[1:]:
			self._issue(orders[item])
			self._receipt(orders[item])
		self.assertEqual(self._assert_matches_admin(), [])

	def test_duplicate_unplanned_product_lines_do_not_offer_purchase(self):
		order = self._sales_order(3)
		order.cancel()
		duplicate = frappe.copy_doc(order)
		duplicate.docstatus = 0
		duplicate.amended_from = order.name
		duplicate.append("items", dict(duplicate.items[0].as_dict()))
		duplicate.insert().submit()
		frappe.set_user(self.buyer)
		self.assertEqual(self._own_shortages(shortage.check_all_shortages(self.company)["shortages"]), [])
