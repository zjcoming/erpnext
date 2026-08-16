from __future__ import annotations

import frappe
from frappe.tests import UnitTestCase


class TestProductionPlanGraph(UnitTestCase):
	def test_earlier_production_plan_date_has_priority(self):
		from process_simplification.api.production_readiness import plan_priority_key

		early = {
			"name": "PP-EARLY",
			"planned_date": "2026-08-20",
			"posting_date": "2026-08-16",
			"creation": "2026-08-02 09:00:00",
		}
		late = {
			"name": "PP-LATE",
			"planned_date": "2026-08-25",
			"posting_date": "2026-08-01",
			"creation": "2026-08-01 09:00:00",
		}

		self.assertLess(plan_priority_key(early), plan_priority_key(late))

	def test_plan_date_falls_back_to_posting_date_then_creation_and_name(self):
		from process_simplification.api.production_readiness import plan_priority_key

		first = {"name": "PP-A", "posting_date": "2026-08-20", "creation": "2026-08-01 09:00:00"}
		second = {"name": "PP-B", "posting_date": "2026-08-20", "creation": "2026-08-01 09:00:00"}

		self.assertLess(plan_priority_key(first), plan_priority_key(second))

	def test_builds_deepest_first_work_order_graph_from_plan_rows(self):
		from process_simplification.api.production_readiness import build_work_order_graph

		plan = {
			"name": "PP-001",
			"planned_date": "2026-08-20",
			"posting_date": "2026-08-16",
			"creation": "2026-08-01 09:00:00",
		}
		work_orders = [
			{
				"name": "WO-FG",
				"production_item": "FG",
				"production_plan_item": "PPI-1",
				"production_plan_sub_assembly_item": None,
				"creation": "2026-08-01 09:00:01",
			},
			{
				"name": "WO-SA",
				"production_item": "SA",
				"production_plan_item": None,
				"production_plan_sub_assembly_item": "PPSA-1",
				"creation": "2026-08-01 09:00:02",
			},
			{
				"name": "WO-LEAF",
				"production_item": "LEAF-SA",
				"production_plan_item": None,
				"production_plan_sub_assembly_item": "PPSA-2",
				"creation": "2026-08-01 09:00:03",
			},
		]
		required_items = [
			{"parent": "WO-FG", "item_code": "SA", "source_warehouse": "Stores - TC", "required_qty": 5},
			{"parent": "WO-SA", "item_code": "LEAF-SA", "source_warehouse": "Stores - TC", "required_qty": 5},
			{"parent": "WO-LEAF", "item_code": "RM", "source_warehouse": "Stores - TC", "required_qty": 10},
		]
		sub_assemblies = [
			{
				"name": "PPSA-1",
				"production_item": "SA",
				"parent_item_code": "FG",
				"bom_level": 0,
				"schedule_date": "2026-08-20",
			},
			{
				"name": "PPSA-2",
				"production_item": "LEAF-SA",
				"parent_item_code": "SA",
				"bom_level": 1,
				"schedule_date": "2026-08-20",
			},
		]

		graph = build_work_order_graph(
			plan,
			work_orders,
			required_items,
			sub_assemblies,
			active_bom_items={"FG", "SA", "LEAF-SA"},
		)

		self.assertEqual(graph.work_orders_by_name["WO-FG"].child_work_orders, ["WO-SA"])
		self.assertEqual(graph.work_orders_by_name["WO-SA"].parent_work_order, "WO-FG")
		self.assertEqual(graph.work_orders_by_name["WO-SA"].child_work_orders, ["WO-LEAF"])
		self.assertEqual(graph.work_orders_by_name["WO-LEAF"].parent_work_order, "WO-SA")
		self.assertEqual(graph.execution_order, ["WO-LEAF", "WO-SA", "WO-FG"])
		self.assertEqual(graph.work_orders_by_name["WO-LEAF"].bom_level, 2)
		self.assertTrue(graph.work_orders_by_name["WO-FG"].is_finished_good)
		self.assertIsInstance(graph.work_orders_by_name["WO-FG"], frappe._dict)
