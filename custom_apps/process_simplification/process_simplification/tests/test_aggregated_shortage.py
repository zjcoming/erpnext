"""Tests for cross-order aggregated shortage checking.

check_all_shortages pulls every open production demand for the company, merges
raw-material need across orders by (item, warehouse), and returns the shortages
so one Material Request can be raised for the combined quantity.
"""

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase


class TestAggregatedShortage(UnitTestCase):
	def test_all_material_demands_disable_workbench_pagination(self):
		from process_simplification.api import production

		overview = {
			"demands": [
				{"company": "_Test Company", "demand_key": f"SOI-{index}"}
				for index in range(25)
			]
		}
		with (
			patch.object(production, "get_production_overview", return_value=overview) as get_overview,
			patch.object(production, "_material_demands", side_effect=lambda demands: demands),
		):
			demands = production.get_all_material_demands("_Test Company")

		get_overview.assert_called_once_with(page_size=0)
		self.assertEqual(len(demands), 25)

	def test_check_all_shortages_merges_demand_across_orders(self):
		from process_simplification.api import shortage

		# Two orders, each needing 100 of finished goods that share RM-SHARED
		# (10 per unit) from the same warehouse.
		all_demands = [
			{
				"bom_no": "BOM-A",
				"qty": 100,
				"source": {
					"sales_order": "SO-A",
					"sales_order_item": "SOI-A",
					"finished_item": "FG-A",
					"sales_order_item_warehouse": "Stores - TC",
				},
			},
			{
				"bom_no": "BOM-B",
				"qty": 100,
				"source": {
					"sales_order": "SO-B",
					"sales_order_item": "SOI-B",
					"finished_item": "FG-B",
					"sales_order_item_warehouse": "Stores - TC",
				},
			},
		]

		def bom_items(bom_no, company, qty, fetch_exploded):
			return {
				"RM-SHARED": frappe._dict(
					{
						"item_code": "RM-SHARED",
						"item_name": "共享原料",
						"stock_uom": "Nos",
						"source_warehouse": "Stores - TC",
						"qty": 10 * qty,
					}
				)
			}

		with (
			patch.object(shortage, "get_all_material_demands", return_value=all_demands),
			patch("process_simplification.api.shortage.resolve_production_source_warehouse",
				return_value=frappe._dict({"warehouse": "Stores - TC", "can_use": True, "reason": None})),
			patch("process_simplification.api.shortage.get_bom_items_as_dict", side_effect=bom_items),
			patch("process_simplification.api.shortage.get_material_stock_snapshot",
				return_value=frappe._dict({"can_calculate": True, "actual_qty": 500, "committed_qty": 0, "available_qty": 500})),
			patch("process_simplification.api.shortage._mr_documents", return_value=[]),
			patch("process_simplification.api.shortage._po_documents", return_value=[]),
			patch("process_simplification.api.shortage.frappe.has_permission", return_value=True),
		):
			result = shortage.check_all_shortages(company="_Test Company")

		shortages = result["shortages"]
		self.assertEqual(len(shortages), 1)
		row = shortages[0]
		# Both orders' need for RM-SHARED merged: 100*10 + 100*10 = 2000.
		self.assertEqual(row["item_code"], "RM-SHARED")
		self.assertEqual(row["required_qty"], 2000)
		# 500 in stock -> 1500 short.
		self.assertEqual(row["shortage_qty"], 1500)
		# The merged row credits both source orders.
		source_orders = {s.get("sales_order") for s in row["sources"]}
		self.assertEqual(source_orders, {"SO-A", "SO-B"})

	def test_check_all_shortages_reports_when_nothing_short(self):
		from process_simplification.api import shortage

		with (
			patch.object(shortage, "get_all_material_demands", return_value=[]),
			patch("process_simplification.api.shortage.frappe.has_permission", return_value=True),
		):
			result = shortage.check_all_shortages(company="_Test Company")

		self.assertEqual(result["shortages"], [])
		self.assertIn("message", result)
