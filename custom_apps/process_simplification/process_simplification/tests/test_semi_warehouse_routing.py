"""Work Order routing follows physical BOM branches, not item groups."""

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from process_simplification.api import production_plan_adapter as adapter


class TestSemiWarehouseRouting(UnitTestCase):
	def _plan(self):
		return frappe._dict(
			sub_assembly_warehouse="Semi",
			sub_assembly_items=[
				frappe._dict(name="A", production_plan_item="ROOT-1", bom_level=0,
					production_item="UPPER", fg_warehouse="Semi", qty=20),
				frappe._dict(name="B", production_plan_item="ROOT-1", bom_level=1,
					production_item="LOWER", fg_warehouse="Semi", qty=0),
				frappe._dict(name="C", production_plan_item="ROOT-1", bom_level=0,
					production_item="OTHER", fg_warehouse="Semi", qty=0),
				frappe._dict(name="D", production_plan_item="ROOT-2", bom_level=0,
					production_item="LOWER", fg_warehouse="Semi", qty=0),
			],
		)

	def test_zero_quantity_children_remain_sources_and_branches_do_not_leak(self):
		sources = adapter._sub_assembly_sources(self._plan())
		self.assertEqual(sources[("root", "ROOT-1")], {"UPPER": "Semi", "OTHER": "Semi"})
		self.assertEqual(sources[("sub", "A")], {"LOWER": "Semi"})
		self.assertEqual(sources[("root", "ROOT-2")], {"LOWER": "Semi"})
		self.assertNotIn("LOWER", sources[("root", "ROOT-1")])

	def test_mixed_materials_and_snapshot_are_saved_before_submission(self):
		class WorkOrder(frappe._dict):
			def set_required_items(self, *, reset_source_warehouse=False):
				self.required_items = [frappe._dict(item_code=code, source_warehouse=self.source_warehouse)
					for code in self.components]

			def save(self):
				self.saved_sources = {row.item_code: row.source_warehouse for row in self.required_items}

		orders = {
			"TOP": WorkOrder(production_plan_item="ROOT-1", components=["UPPER", "RAW", "LOWER"]),
			"SUB": WorkOrder(production_plan_item="ROOT-1", production_plan_sub_assembly_item="A",
				components=["LOWER", "RAW"]),
		}
		with patch.object(adapter.frappe, "get_doc", side_effect=lambda doctype, name: orders[name]):
			adapter._apply_guided_source_warehouse(list(orders), "Raw", plan=self._plan())
		self.assertEqual(orders["TOP"].saved_sources, {"UPPER": "Semi", "RAW": "Raw", "LOWER": "Raw"})
		self.assertEqual(orders["SUB"].saved_sources, {"LOWER": "Semi", "RAW": "Raw"})
		for order in orders.values():
			self.assertEqual(order.custom_semi_finished_warehouse, "Semi")
			self.assertEqual(order.source_warehouse, "Raw")

	def test_same_item_mixed_purchased_and_manufactured_cannot_silently_change_source(self):
		order = frappe._dict(required_items=[frappe._dict(item_code="X", required_qty=30)])
		manufactured = [frappe._dict(production_item="X", fg_warehouse="Semi", required_qty=20)]
		with self.assertRaises(frappe.ValidationError):
			adapter._validate_distinct_component_sources(order, manufactured, "Raw")
		# Existing common-source configurations remain valid because no split is needed.
		adapter._validate_distinct_component_sources(order, manufactured, "Semi")
