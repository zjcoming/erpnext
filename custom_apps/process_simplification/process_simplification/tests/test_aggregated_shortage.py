"""Tests for cross-order aggregated shortage checking.

check_all_shortages pulls every open production demand for the company, merges
raw-material need across orders by (item, warehouse), and returns the shortages
so one Material Request can be raised for the combined quantity.
"""

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase


class TestAggregatedShortage(UnitTestCase):
	def test_plan_shortages_purchase_only_leaf_materials_even_if_subassembly_is_purchasable(self):
		from process_simplification.api import shortage

		readiness = {
			"SOI-001": [
				{
					"name": "PP-001",
					"planned_date": "2026-08-20",
					"work_orders": [
						{
							"name": "WO-FG",
							"sales_order": "SO-001",
							"sales_order_item": "SOI-001",
							"production_item": "FG",
							"required_items": [
								{
									"item_code": "SA",
									"item_name": "可采购半成品",
									"source_warehouse": "Stores - TC",
									"required_qty": 5,
									"current_gap_qty": 5,
									"shortage_qty": 0,
									"supply_type": "manufactured",
									"is_purchase_item": 1,
								},
							],
						},
						{
							"name": "WO-SA",
							"sales_order": "SO-001",
							"sales_order_item": "SOI-001",
							"production_item": "SA",
							"required_items": [
								{
									"item_code": "RM",
									"item_name": "底层原料",
									"stock_uom": "Nos",
									"source_warehouse": "Stores - TC",
									"required_qty": 10,
									"current_gap_qty": 10,
									"shortage_qty": 10,
									"status": "new_purchase_required",
									"supply_type": "purchased",
								},
							],
						},
					],
				}
			]
		}

		rows = shortage.calculate_plan_purchase_shortages(readiness, {"SOI-001"})

		self.assertEqual([row["item_code"] for row in rows], ["RM"])
		self.assertEqual(rows[0]["shortage_qty"], 10)
		self.assertEqual(rows[0]["sources"][0]["work_order"], "WO-SA")

	def test_check_all_shortages_merges_demand_across_orders(self):
		from process_simplification.api import shortage

		def plan(plan_name, work_order, sales_order, sales_order_item, available_qty, shortage_qty):
			return {
				"name": plan_name,
				"planned_date": "2026-08-20",
				"work_orders": [
					{
						"name": work_order,
						"sales_order": sales_order,
						"sales_order_item": sales_order_item,
						"production_item": f"FG-{sales_order_item}",
						"required_items": [
							{
								"item_code": "RM-SHARED",
								"item_name": "共享原料",
								"stock_uom": "Nos",
								"source_warehouse": "Stores - TC",
								"required_qty": 1000,
								"available_qty": available_qty,
								"current_gap_qty": shortage_qty,
								"shortage_qty": shortage_qty,
								"status": "new_purchase_required",
								"supply_type": "purchased",
							}
						],
					},
				],
			}

		readiness = {
			"SOI-A": [plan("PP-A", "WO-A", "SO-A", "SOI-A", 500, 500)],
			"SOI-B": [plan("PP-B", "WO-B", "SO-B", "SOI-B", 0, 1000)],
		}

		with (
			patch(
				"process_simplification.api.production_readiness.get_production_plan_readiness",
				return_value=readiness,
			),
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
			patch(
				"process_simplification.api.production_readiness.get_production_plan_readiness",
				return_value={},
			),
			patch("process_simplification.api.shortage.frappe.has_permission", return_value=True),
		):
			result = shortage.check_all_shortages(company="_Test Company")

		self.assertEqual(result["shortages"], [])
		self.assertIn("message", result)

	def test_material_request_rows_are_rejected_when_plan_shortage_has_changed(self):
		from process_simplification.api import shortage
		from process_simplification.api.utils import SimplifiedFlowError

		with self.assertRaises(SimplifiedFlowError):
			shortage.revalidate_purchase_rows(
				[
					{
						"item_code": "RM",
						"warehouse": "Stores - TC",
						"purchase_qty": 10,
						"shortage_qty": 10,
					}
				],
				[
					{
						"item_code": "RM",
						"warehouse": "Stores - TC",
						"shortage_qty": 4,
					}
				],
			)
