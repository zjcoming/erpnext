"""Native SO production projection: exclude PP subassemblies without losing root totals.

Fixtures are isolated to the test runner and rolled back; no production method is mocked.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, nowdate

from erpnext.accounts.utils import get_fiscal_year
from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry as make_wo_stock_entry
from erpnext.selling.doctype.sales_order.sales_order import update_produced_qty_in_so_item
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from process_simplification.api.production_plan_adapter import create_work_orders_via_production_plan


class TestSalesOrderProducedQty(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.assertTrue(frappe.in_test)
		self.assertTrue(frappe.conf.get("allow_tests"))
		self.enterContext(self.set_user("Administrator"))
		self.addCleanup(self._rollback_fixture)
		self.prefix = "C75-" + frappe.generate_hash(length=8)
		self.company = None
		abbr = "Q" + frappe.generate_hash(length=4).upper()
		company = frappe.get_doc({
			"doctype": "Company", "company_name": self.prefix, "abbr": abbr,
			"default_currency": "INR", "country": "India",
			"create_chart_of_accounts_based_on": "Standard Template",
		}).insert()
		self.company = company.name
		if not get_fiscal_year(nowdate(), company=self.company, verbose=0, raise_on_missing=False):
			year = getdate(nowdate()).year
			frappe.get_doc({
				"doctype": "Fiscal Year", "year": self.prefix + "-FY",
				"year_start_date": f"{year}-01-01", "year_end_date": f"{year}-12-31",
				"companies": [{"company": self.company}],
			}).insert()
		self.stores, self.wip, self.finished = (
			self._warehouse(name, "All Warehouses - " + abbr)
			for name in ("Stores", "WIP", "Finished")
		)
		frappe.db.set_value("Company", self.company, {
			"default_wip_warehouse": self.wip, "default_fg_warehouse": self.finished,
		})
		frappe.clear_document_cache("Company", self.company)
		self.item_group = frappe.get_doc({"doctype": "Item Group", "item_group_name": self.prefix,
			"parent_item_group": "All Item Groups", "is_group": 0}).insert().name
		self.raw, self.semi, self.fg = (
			frappe.get_doc({"doctype": "Item", "item_code": self.prefix + "-" + label,
				"item_name": self.prefix + "-" + label, "item_group": self.item_group,
				"stock_uom": "Nos", "is_stock_item": 1, "valuation_rate": 1,
				"is_purchase_item": int(label == "RAW"), "is_sales_item": int(label == "FG"),
				"include_item_in_manufacturing": 1,
				"item_defaults": [{"company": self.company, "default_warehouse": self.stores}],
			}).insert().name for label in ("RAW", "SEMI", "FG")
		)
		self.semi_bom = self._bom(self.semi, self.raw)
		self.fg_bom = self._bom(self.fg, self.semi, self.semi_bom.name)
		group = frappe.get_doc({"doctype": "Customer Group", "customer_group_name": self.prefix + "-Group",
			"parent_customer_group": "All Customer Groups", "is_group": 0}).insert()
		territory = frappe.get_doc({"doctype": "Territory", "territory_name": self.prefix,
			"parent_territory": "All Territories", "is_group": 0}).insert()
		self.price_list = frappe.get_doc({"doctype": "Price List", "price_list_name": self.prefix,
			"currency": "INR", "selling": 1, "buying": 0, "enabled": 1}).insert().name
		self.customer = frappe.get_doc({"doctype": "Customer", "customer_name": self.prefix,
			"customer_type": "Company", "customer_group": group.name,
			"territory": territory.name}).insert().name


	def _warehouse(self, label, parent):
		return frappe.get_doc({"doctype": "Warehouse", "warehouse_name": self.prefix + "-" + label,
			"company": self.company, "parent_warehouse": parent, "is_group": 0}).insert().name

	def _bom(self, item, component, component_bom=None):
		bom = frappe.get_doc({"doctype": "BOM", "item": item, "company": self.company,
			"currency": "INR", "quantity": 1, "is_default": 1, "is_active": 1,
			"with_operations": 0, "rm_cost_as_per": "Valuation Rate"})
		bom.append("items", {"item_code": component, "qty": 1, "uom": "Nos", "stock_uom": "Nos",
			"rate": 1, "source_warehouse": self.stores, "bom_no": component_bom})
		bom.insert().submit()
		return bom

	def _rollback_fixture(self):
		frappe.db.rollback()
		if self.company:
			frappe.clear_document_cache("Company", self.company)
		frappe.cache().delete_key("fiscal_years")

	def _planned_case(self, order_qty=2, planned_qty=2):
		make_stock_entry(item_code=self.raw, qty=planned_qty, company=self.company,
			to_warehouse=self.stores, rate=1)
		so = frappe.new_doc("Sales Order")
		so.update({"company": self.company, "customer": self.customer, "currency": "INR",
			"transaction_date": nowdate(), "delivery_date": add_days(nowdate(), 10),
			"selling_price_list": self.price_list, "price_list_currency": "INR",
			"plc_conversion_rate": 1, "conversion_rate": 1})
		so.append("items", {"item_code": self.fg, "qty": order_qty, "rate": 10,
			"uom": "Nos", "conversion_factor": 1, "warehouse": self.finished,
			"bom_no": self.fg_bom.name})
		so.insert().submit()
		result = create_work_orders_via_production_plan(
			sales_order=so.name, sales_order_item=so.items[0].name, company=self.company,
			item_code=self.fg, bom_no=self.fg_bom.name, planned_qty=planned_qty,
			source_warehouse=self.stores, sub_assembly_warehouse=self.stores,
			fg_warehouse=self.finished, delivery_date=add_days(nowdate(), 10),
		)
		orders = [frappe.get_doc("Work Order", name) for name in result["work_orders"]]
		self.assertEqual(len(orders), 2)
		root = next(wo for wo in orders if wo.production_item == self.fg)
		semi = next(wo for wo in orders if wo.production_item == self.semi)
		self.assertEqual(root.docstatus, 1)
		self.assertEqual(semi.docstatus, 1)
		self.assertFalse(root.production_plan_sub_assembly_item)
		self.assertTrue(semi.production_plan_sub_assembly_item)
		for wo in orders:
			self.assertEqual(wo.production_plan, result["production_plan"])
			self.assertEqual(wo.sales_order, so.name)
			self.assertEqual(wo.sales_order_item, so.items[0].name)
		return so, root, semi

	def _stock(self, wo, purpose, qty):
		entry = frappe.get_doc(make_wo_stock_entry(wo.name, purpose, qty))
		entry.insert().submit()
		wo.reload()
		return entry

	def _assert_projection(self, so, expected):
		self.assertEqual(frappe.db.get_value("Sales Order Item", so.items[0].name,
			"produced_qty"), expected)

	def test_multilevel_stock_partial_manufacture_and_cancel_count_only_root(self):
		so, root, semi = self._planned_case()
		self._stock(semi, "Material Transfer for Manufacture", 2)
		self._stock(semi, "Manufacture", 2)
		self.assertEqual(semi.produced_qty, 2)
		self._assert_projection(so, 0)

		self._stock(root, "Material Transfer for Manufacture", 2)
		first = self._stock(root, "Manufacture", 1)
		self.assertEqual(root.produced_qty, 1)
		# Unpatched native SUM includes semi2 + root1 and fails here with 3 != 1.
		self._assert_projection(so, 1)
		second = self._stock(root, "Manufacture", 1)
		self.assertNotEqual(first.name, second.name)
		self.assertEqual(root.produced_qty, 2)
		self._assert_projection(so, 2)

		second.cancel()
		root.reload()
		semi.reload()
		self.assertEqual(second.docstatus, 2)
		self.assertEqual(root.produced_qty, 1)
		self.assertEqual(semi.produced_qty, 2)
		self._assert_projection(so, 1)
		self.assertEqual(frappe.db.get_value("Bin", {"item_code": self.fg,
			"warehouse": self.finished}, "actual_qty"), 1)


	def test_closed_plan_stays_closed_during_existing_work_order_production(self):
		so, root, semi = self._planned_case(order_qty=4, planned_qty=4)
		self._stock(semi, "Material Transfer for Manufacture", 2)
		plan = frappe.get_doc("Production Plan", root.production_plan)
		plan.set_status(close=True)
		self.assertEqual(plan.reload().status, "Closed")

		# Work already issued before closing the plan can still finish. Its real
		# stock posting must not silently reopen the supervisor's closed plan.
		self._stock(semi, "Manufacture", 2)
		self.assertEqual(semi.produced_qty, 2)
		self._assert_projection(so, 0)
		self.assertEqual(plan.reload().status, "Closed")
		self.assertEqual(frappe.db.get_value("Bin", {"item_code": self.semi,
			"warehouse": self.stores}, "actual_qty"), 2)

		# An explicit reopen remains available; the guard is only for automatic
		# quantity/status updates rather than a permanently locked plan.
		self.assertEqual(frappe.db.get_value("Bin", {"item_code": self.raw,
			"warehouse": self.stores}, "actual_qty"), 2)
		self.assertEqual(frappe.db.get_value("Bin", {"item_code": self.raw,
			"warehouse": self.stores}, "reserved_qty_for_production_plan"), 0)

		plan.set_status(close=False, update_bin=True)
		self.assertNotEqual(plan.reload().status, "Closed")

	def test_projection_sums_all_submitted_roots_and_excludes_other_states(self):
		so, root, semi = self._planned_case(order_qty=8)

		def extra_root(submit):
			wo = frappe.new_doc("Work Order")
			wo.update({"company": self.company, "production_item": self.fg,
				"bom_no": self.fg_bom.name, "qty": 2, "sales_order": so.name,
				"sales_order_item": so.items[0].name, "stock_uom": "Nos",
				"source_warehouse": self.stores, "wip_warehouse": self.wip,
				"fg_warehouse": self.finished, "use_multi_level_bom": 0,
				"transfer_material_against": "Work Order", "planned_start_date": nowdate()})
			wo.get_items_and_operations_from_bom()
			for item in wo.required_items:
				item.source_warehouse = self.stores
			wo.insert()
			if submit:
				wo.submit()
			return wo

		second = extra_root(True)
		draft = extra_root(False)
		cancelled = extra_root(True)
		cancelled.cancel()
		# Query-boundary fixtures only: unlike the lifecycle test above these
		# nonzero counters intentionally have no stock entries, to expose filters.
		for wo, qty in [(root, 1), (second, 2), (semi, 2), (draft, 1), (cancelled, 1)]:
			frappe.db.set_value("Work Order", wo.name, "produced_qty", qty)
		# Both null and empty PP-subassembly links identify valid root WOs.
		frappe.db.set_value("Work Order", second.name, "production_plan_sub_assembly_item", "")
		self.assertEqual(frappe.db.get_value("Work Order", draft.name, "docstatus"), 0)
		self.assertEqual(frappe.db.get_value("Work Order", cancelled.name, "docstatus"), 2)
		update_produced_qty_in_so_item(so.name, so.items[0].name)
		self._assert_projection(so, 3)
