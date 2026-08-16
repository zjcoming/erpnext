from __future__ import annotations

from unittest.mock import patch

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


class TestWorkOrderReadiness(UnitTestCase):
	def _graph(self, *, plan_name, planned_date, creation, work_orders, required_items, sub_assemblies=None, active_bom_items=None):
		from process_simplification.api.production_readiness import build_work_order_graph

		return build_work_order_graph(
			{
				"name": plan_name,
				"planned_date": planned_date,
				"posting_date": "2026-08-16",
				"creation": creation,
			},
			work_orders,
			required_items,
			sub_assemblies or [],
			active_bom_items=active_bom_items or set(),
		)

	def test_only_deepest_work_order_is_ready_when_parent_waits_for_its_output(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		graph = self._graph(
			plan_name="PP-001",
			planned_date="2026-08-20",
			creation="2026-08-01 09:00:00",
			work_orders=[
				{"name": "WO-FG", "production_item": "FG", "production_plan_item": "PPI-1", "status": "Not Started"},
				{
					"name": "WO-SA",
					"production_item": "SA",
					"production_plan_sub_assembly_item": "PPSA-1",
					"status": "Not Started",
				},
			],
			required_items=[
				{
					"parent": "WO-FG",
					"item_code": "SA",
					"source_warehouse": "Stores - TC",
					"required_qty": 5,
					"transferred_qty": 0,
					"is_purchase_item": 1,
				},
				{
					"parent": "WO-SA",
					"item_code": "RM",
					"source_warehouse": "Stores - TC",
					"required_qty": 10,
					"transferred_qty": 0,
					"is_purchase_item": 1,
				},
			],
			sub_assemblies=[
				{
					"name": "PPSA-1",
					"production_item": "SA",
					"parent_item_code": "FG",
					"bom_level": 0,
					"schedule_date": "2026-08-20",
				}
			],
			active_bom_items={"FG", "SA"},
		)

		result = allocate_work_order_readiness(
			[graph],
			{
				("RM", "Stores - TC"): {"available_qty": 10, "actual_qty": 10},
				("SA", "Stores - TC"): {"available_qty": 0, "actual_qty": 0},
			},
		)[0]
		by_name = result.work_orders_by_name

		self.assertEqual(by_name["WO-SA"].readiness_status, "ready_now")
		self.assertEqual(by_name["WO-FG"].readiness_status, "waiting_subassembly")
		self.assertEqual(by_name["WO-FG"].required_items[0].supply_type, "manufactured")
		self.assertEqual(by_name["WO-FG"].required_items[0].child_work_order, "WO-SA")

	def test_earlier_plan_date_consumes_shared_raw_material_first(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		def plan(name, planned_date, creation):
			return self._graph(
				plan_name=f"PP-{name}",
				planned_date=planned_date,
				creation=creation,
				work_orders=[{"name": f"WO-{name}", "production_item": f"FG-{name}", "production_plan_item": f"PPI-{name}", "status": "Not Started"}],
				required_items=[
					{
						"parent": f"WO-{name}",
						"item_code": "RM-SHARED",
						"source_warehouse": "Stores - TC",
						"required_qty": 7,
						"transferred_qty": 0,
						"is_purchase_item": 1,
					}
				],
				active_bom_items={f"FG-{name}"},
			)

		late = plan("LATE", "2026-08-25", "2026-08-01 08:00:00")
		early = plan("EARLY", "2026-08-20", "2026-08-02 08:00:00")
		result = allocate_work_order_readiness(
			[late, early],
			{("RM-SHARED", "Stores - TC"): {"available_qty": 10, "actual_qty": 10}},
		)
		by_plan = {row.name: row for row in result}

		self.assertEqual(by_plan["PP-EARLY"].work_orders_by_name["WO-EARLY"].readiness_status, "ready_now")
		late_work_order = by_plan["PP-LATE"].work_orders_by_name["WO-LATE"]
		self.assertEqual(late_work_order.readiness_status, "purchase_shortage")
		self.assertEqual(late_work_order.required_items[0].available_qty, 3)
		self.assertEqual(late_work_order.required_items[0].current_gap_qty, 4)

	def test_manufactured_item_without_child_task_is_not_a_purchase_shortage(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		graph = self._graph(
			plan_name="PP-001",
			planned_date="2026-08-20",
			creation="2026-08-01 09:00:00",
			work_orders=[{"name": "WO-FG", "production_item": "FG", "production_plan_item": "PPI-1", "status": "Not Started"}],
			required_items=[
				{
					"parent": "WO-FG",
					"item_code": "SA-MISSING",
					"source_warehouse": "Stores - TC",
					"required_qty": 5,
					"transferred_qty": 0,
					"is_purchase_item": 1,
				}
			],
			active_bom_items={"FG", "SA-MISSING"},
		)

		work_order = allocate_work_order_readiness(
			[graph],
			{("SA-MISSING", "Stores - TC"): {"available_qty": 0, "actual_qty": 0}},
		)[0].work_orders_by_name["WO-FG"]

		self.assertEqual(work_order.readiness_status, "production_task_missing")
		self.assertEqual(work_order.required_items[0].supply_type, "manufactured")

	def test_fully_transferred_direct_materials_do_not_consume_stock_again(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		graph = self._graph(
			plan_name="PP-001",
			planned_date="2026-08-20",
			creation="2026-08-01 09:00:00",
			work_orders=[{"name": "WO-FG", "production_item": "FG", "production_plan_item": "PPI-1", "status": "Not Started"}],
			required_items=[
				{
					"parent": "WO-FG",
					"item_code": "RM",
					"source_warehouse": "Stores - TC",
					"required_qty": 10,
					"transferred_qty": 10,
				}
			],
			active_bom_items={"FG"},
		)

		work_order = allocate_work_order_readiness([graph], {})[0].work_orders_by_name["WO-FG"]

		self.assertEqual(work_order.readiness_status, "materials_transferred")
		self.assertEqual(work_order.required_items[0].required_qty, 0)


class TestProductionReadinessLoading(UnitTestCase):
	def test_loads_plan_work_orders_and_current_stock_as_one_order_item_snapshot(self):
		from process_simplification.api import production_readiness

		rows = {
			"Work Order": [
				frappe._dict(
					name="WO-FG",
					production_item="FG",
					production_plan="PP-001",
					production_plan_item="PPI-1",
					production_plan_sub_assembly_item=None,
					sales_order="SO-001",
					sales_order_item="SOI-001",
					company="_Test Company",
					status="Not Started",
					qty=5,
					produced_qty=0,
					planned_start_date="2026-08-20 08:00:00",
					creation="2026-08-01 09:00:01",
				),
				frappe._dict(
					name="WO-SA",
					production_item="SA",
					production_plan="PP-001",
					production_plan_item=None,
					production_plan_sub_assembly_item="PPSA-1",
					sales_order="SO-001",
					sales_order_item="SOI-001",
					company="_Test Company",
					status="Not Started",
					qty=5,
					produced_qty=0,
					planned_start_date="2026-08-20 08:00:00",
					creation="2026-08-01 09:00:02",
				),
			],
			"Work Order Item": [
				frappe._dict(parent="WO-FG", item_code="SA", item_name="半成品", stock_uom="Nos", source_warehouse="Stores - TC", required_qty=5, transferred_qty=0, consumed_qty=0),
				frappe._dict(parent="WO-SA", item_code="RM", item_name="原料", stock_uom="Nos", source_warehouse="Stores - TC", required_qty=10, transferred_qty=0, consumed_qty=0),
			],
			"Production Plan": [frappe._dict(name="PP-001", company="_Test Company", posting_date="2026-08-16", creation="2026-08-01 09:00:00", status="In Process")],
			"Production Plan Item": [frappe._dict(name="PPI-1", parent="PP-001", item_code="FG", planned_start_date="2026-08-20 08:00:00", sales_order_item="SOI-001")],
			"Production Plan Sub Assembly Item": [frappe._dict(name="PPSA-1", parent="PP-001", production_item="SA", parent_item_code="FG", bom_level=0, schedule_date="2026-08-20", type_of_manufacturing="In House")],
			"Item": [frappe._dict(name="SA", is_purchase_item=1), frappe._dict(name="RM", is_purchase_item=1)],
		}

		def get_all(doctype, **kwargs):
			if doctype == "BOM":
				return ["FG", "SA"]
			return rows.get(doctype, [])

		def stock(item_code, warehouse):
			qty = 10 if item_code == "RM" else 0
			return frappe._dict(can_calculate=True, actual_qty=qty, committed_qty=0, available_qty=qty)

		with (
			patch.object(production_readiness.frappe, "get_all", side_effect=get_all),
			patch("process_simplification.api.shortage.get_material_stock_snapshot", side_effect=stock),
		):
			result = production_readiness.get_production_plan_readiness(
				company="_Test Company",
				sales_order_items=["SOI-001"],
			)

		plans = result["SOI-001"]
		self.assertEqual(len(plans), 1)
		self.assertEqual(plans[0]["name"], "PP-001")
		self.assertEqual(plans[0]["planned_date"], "2026-08-20 08:00:00")
		self.assertEqual([row["name"] for row in plans[0]["work_orders"]], ["WO-SA", "WO-FG"])
		self.assertEqual(plans[0]["work_orders"][0]["readiness_status"], "ready_now")
		self.assertEqual(plans[0]["work_orders"][1]["readiness_status"], "waiting_subassembly")
		self.assertEqual(plans[0]["summary"]["ready_work_order_count"], 1)
