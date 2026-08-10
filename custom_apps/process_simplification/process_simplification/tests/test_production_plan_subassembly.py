"""Tests for multi-level sub-assembly Work Order creation via Production Plan.

The custom create_work_order only built a single Work Order for the finished
good. Factories with self-made semi-finished goods also need a Work Order for
each in-house sub-assembly level. These tests lock in the adapter that drives
ERPNext's Production Plan sub-assembly engine while keeping the finished-good
Work Order linked back to its Sales Order.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import add_days, nowdate


class TestProductionPlanSubassemblyAdapter(UnitTestCase):
	"""Unit tests: the adapter builds a valid Production Plan and drives the
	sub-assembly + work-order engine, without hitting warehouse fixtures."""

	def _adapter(self):
		from process_simplification.api import production_plan_adapter

		return production_plan_adapter

	def test_adapter_builds_po_item_with_delivery_priority_net_qty_and_so_link(self):
		adapter = self._adapter()

		captured = {}

		class FakePP:
			def __init__(self):
				self.po_items = []
				self.sub_assembly_items = []
				self.name = None
				self.flags = frappe._dict()

			def append(self, table, row):
				child = frappe._dict(row)
				child.name = "new-" + frappe.generate_hash(length=6)
				getattr(self, table).append(child)
				return child

			def insert(self, *args, **kwargs):
				self.name = "PP-0001"
				captured["inserted"] = True

			def get_sub_assembly_items(self):
				captured["sub_called"] = True

			def make_work_order(self):
				captured["wo_called"] = True

		fake = FakePP()

		with patch.object(adapter.frappe, "new_doc", return_value=fake) as new_doc:
			result = adapter.create_work_orders_via_production_plan(
				sales_order="SO-001",
				sales_order_item="SOI-001",
				company="_Test Company",
				item_code="FG-001",
				bom_no="BOM-FG-001",
				planned_qty=30,
				fg_warehouse="FG - TC",
				sub_assembly_warehouse="Stores - TC",
				delivery_date="2026-09-01",
			)

		new_doc.assert_called_once_with("Production Plan")
		# One finished-good po_items row carrying the SO link and net qty.
		self.assertEqual(len(fake.po_items), 1)
		po = fake.po_items[0]
		self.assertEqual(po.item_code, "FG-001")
		self.assertEqual(po.bom_no, "BOM-FG-001")
		self.assertEqual(po.planned_qty, 30)
		self.assertEqual(po.sales_order, "SO-001")
		self.assertEqual(po.sales_order_item, "SOI-001")
		self.assertEqual(po.warehouse, "FG - TC")
		self.assertEqual(po.include_exploded_items, 1)
		# Sub-assembly engine driven with skip-available and a warehouse, and
		# combine disabled so per-order Work Order links survive.
		self.assertEqual(fake.skip_available_sub_assembly_item, 1)
		self.assertEqual(fake.sub_assembly_warehouse, "Stores - TC")
		self.assertEqual(fake.combine_sub_items, 0)
		# Engine calls happen after the PP is persisted (so back-references resolve).
		self.assertTrue(captured.get("inserted"))
		self.assertTrue(captured.get("sub_called"))
		self.assertTrue(captured.get("wo_called"))
		self.assertEqual(result["production_plan"], "PP-0001")

	def test_adapter_mutes_engine_msgprint(self):
		adapter = self._adapter()

		class FakePP:
			def __init__(self):
				self.po_items = []
				self.sub_assembly_items = []
				self.name = "PP-0002"
				self.flags = frappe._dict()

			def append(self, table, row):
				child = frappe._dict(row)
				child.name = "n1"
				getattr(self, table).append(child)
				return child

			def insert(self, *a, **k):
				pass

			def get_sub_assembly_items(self):
				pass

			def make_work_order(self):
				# The engine would surface an English "N created" message here.
				frappe.msgprint("2 created")

		with patch.object(adapter.frappe, "new_doc", return_value=FakePP()), patch.object(
			adapter.frappe, "msgprint"
		) as msgprint:
			adapter.create_work_orders_via_production_plan(
				sales_order="SO-001",
				sales_order_item="SOI-001",
				company="_Test Company",
				item_code="FG-001",
				bom_no="BOM-FG-001",
				planned_qty=10,
				fg_warehouse="FG - TC",
				sub_assembly_warehouse="Stores - TC",
				delivery_date=None,
			)

		# Engine messages must not surface to the simplified UI.
		msgprint.assert_not_called()

	def test_adapter_reports_created_work_orders_with_subassembly_count(self):
		adapter = self._adapter()

		class FakePP:
			def __init__(self):
				self.po_items = []
				self.sub_assembly_items = []
				self.name = "PP-0003"
				self.flags = frappe._dict()

			def append(self, table, row):
				child = frappe._dict(row)
				child.name = "n"
				getattr(self, table).append(child)
				return child

			def insert(self, *a, **k):
				pass

			def get_sub_assembly_items(self):
				# Two in-house sub-assembly levels resolved by the engine.
				self.sub_assembly_items = [frappe._dict(production_item="SA-1"), frappe._dict(production_item="SA-2")]

			def make_work_order(self):
				pass

		created = ["WO-FG", "WO-SA-1", "WO-SA-2"]
		with patch.object(adapter.frappe, "new_doc", return_value=FakePP()), patch.object(
			adapter, "_work_orders_for_plan", return_value=created
		):
			result = adapter.create_work_orders_via_production_plan(
				sales_order="SO-001",
				sales_order_item="SOI-001",
				company="_Test Company",
				item_code="FG-001",
				bom_no="BOM-FG-001",
				planned_qty=10,
				fg_warehouse="FG - TC",
				sub_assembly_warehouse="Stores - TC",
				delivery_date=None,
			)

		self.assertEqual(result["work_orders"], created)
		self.assertEqual(result["sub_assembly_count"], 2)


class TestProductionPlanSubassemblyIntegration(IntegrationTestCase):
	"""End-to-end: a finished good whose BOM contains a self-made sub-assembly
	must produce a Work Order for the finished good AND the sub-assembly, both
	linked back to the Sales Order."""

	def _fresh_company(self, suffix):
		abbr = "S{0}".format(suffix[:4]).upper()
		company = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": "PP SubAsm {0}".format(suffix),
				"abbr": abbr,
				"default_currency": "INR",
				"country": "India",
				"create_chart_of_accounts_based_on": "Standard Template",
			}
		).insert()
		return company, abbr

	def _item(self, code, item_group, **flags):
		doc = {
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": item_group,
			"stock_uom": "Nos",
			"is_stock_item": 1,
		}
		doc.update(flags)
		return frappe.get_doc(doc).insert()

	def _bom(self, company, item_code, components):
		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": item_code,
				"company": company,
				"currency": "INR",
				"quantity": 1,
				"is_active": 1,
				"is_default": 1,
				"items": components,
			}
		)
		# Same PyPika compatibility scope used by the other integration tests.
		with patch("erpnext.manufacturing.doctype.bom.bom.BOM.check_recursion"):
			bom.insert()
			bom.submit()
		return bom

	def test_creates_work_orders_for_finished_good_and_sub_assembly(self):
		from process_simplification.api.production_plan_adapter import (
			create_work_orders_via_production_plan,
		)

		suffix = frappe.generate_hash(length=8)
		company, abbr = self._fresh_company(suffix)
		warehouse = "Stores - {0}".format(abbr)
		group = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "PP SubAsm Group {0}".format(suffix),
				"parent_item_group": "All Item Groups",
				"is_group": 0,
			}
		).insert()

		raw = self._item("PP-RM-{0}".format(suffix), group.name, is_purchase_item=1, valuation_rate=5)
		# Self-made sub-assembly (has its own BOM -> in-house manufacture).
		sub = self._item("PP-SA-{0}".format(suffix), group.name, is_purchase_item=0)
		fg = self._item("PP-FG-{0}".format(suffix), group.name, is_sales_item=1)

		self._bom(company.name, sub.name, [
			{"item_code": raw.name, "qty": 2, "uom": "Nos", "stock_uom": "Nos", "rate": 5, "source_warehouse": warehouse},
		])
		fg_bom = self._bom(company.name, fg.name, [
			{"item_code": sub.name, "qty": 1, "uom": "Nos", "stock_uom": "Nos", "rate": 10, "source_warehouse": warehouse},
		])

		# Company WIP/FG defaults so Work Order insert has required warehouses.
		frappe.db.set_value("Company", company.name, {
			"default_wip_warehouse": "Work In Progress - {0}".format(abbr),
			"default_fg_warehouse": "Finished Goods - {0}".format(abbr),
		})

		# A real submitted Sales Order: Production Plan validates the po_items
		# sales_order link on insert.
		customer_group = frappe.get_doc({
			"doctype": "Customer Group",
			"customer_group_name": "PP SubAsm Cust {0}".format(suffix),
			"parent_customer_group": "All Customer Groups",
			"is_group": 0,
		}).insert()
		territory = frappe.get_doc({
			"doctype": "Territory",
			"territory_name": "PP SubAsm Terr {0}".format(suffix),
			"parent_territory": "All Territories",
			"is_group": 0,
		}).insert()
		customer = frappe.get_doc({
			"doctype": "Customer",
			"customer_name": "PP SubAsm Customer {0}".format(suffix),
			"customer_type": "Company",
			"customer_group": customer_group.name,
			"territory": territory.name,
		}).insert()
		sales_order = frappe.get_doc({
			"doctype": "Sales Order",
			"company": company.name,
			"customer": customer.name,
			"currency": "INR",
			"delivery_date": add_days(nowdate(), 5),
			"items": [{
				"item_code": fg.name,
				"qty": 5,
				"rate": 25,
				"delivery_date": add_days(nowdate(), 5),
				"warehouse": "Finished Goods - {0}".format(abbr),
			}],
		})
		sales_order.insert()
		sales_order.submit()

		result = create_work_orders_via_production_plan(
			sales_order=sales_order.name,
			sales_order_item=sales_order.items[0].name,
			company=company.name,
			item_code=fg.name,
			bom_no=fg_bom.name,
			planned_qty=5,
			fg_warehouse="Finished Goods - {0}".format(abbr),
			sub_assembly_warehouse=warehouse,
			delivery_date=add_days(nowdate(), 5),
		)

		# One sub-assembly level resolved; a WO for the finished good and the
		# sub-assembly, both linked to the Sales Order.
		self.assertEqual(result["sub_assembly_count"], 1)
		self.assertGreaterEqual(len(result["work_orders"]), 2)
		produced = frappe.get_all(
			"Work Order",
			filters={"production_plan": result["production_plan"]},
			fields=["production_item", "sales_order", "sales_order_item"],
		)
		items = {row.production_item for row in produced}
		self.assertIn(fg.name, items)
		self.assertIn(sub.name, items)
		for row in produced:
			self.assertEqual(row.sales_order, sales_order.name)
			self.assertEqual(row.sales_order_item, sales_order.items[0].name)
