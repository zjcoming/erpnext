from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase


class TestProductionPlanGraph(UnitTestCase):
	def test_attaches_item_master_names_without_replacing_work_order_codes(self):
		from process_simplification.api.production_readiness import attach_work_order_item_names

		work_orders = [frappe._dict(name="WO-SA", production_item="301008201014")]
		attach_work_order_item_names(
			work_orders,
			{"301008201014": frappe._dict(item_name="插针骨架半成品")},
		)

		self.assertEqual(work_orders[0].production_item, "301008201014")
		self.assertEqual(work_orders[0].production_item_name, "插针骨架半成品")

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

	def test_parent_matching_uses_plan_row_and_bom_level(self):
		from process_simplification.api.production_readiness import build_work_order_graph

		graph = build_work_order_graph(
			{"name": "PP-MULTI", "posting_date": "2026-08-17"},
			[
				{
					"name": "WO-FG-A", "production_item": "FG", "production_plan_item": "PPI-A",
					"sales_order_item": "SOI-A", "creation": "2026-08-01 09:00:00",
				},
				{
					"name": "WO-FG-B", "production_item": "FG", "production_plan_item": "PPI-B",
					"sales_order_item": "SOI-B", "creation": "2026-08-01 09:00:01",
				},
				{
					"name": "WO-SA-B", "production_item": "SA",
					"production_plan_sub_assembly_item": "PPSA-B",
					"sales_order_item": "SOI-B", "creation": "2026-08-01 09:00:02",
				},
			],
			[
				{"parent": "WO-FG-A", "item_code": "SA", "required_qty": 1},
				{"parent": "WO-FG-B", "item_code": "SA", "required_qty": 1},
			],
			[
				{
					"name": "PPSA-B", "production_item": "SA", "parent_item_code": "FG",
					"production_plan_item": "PPI-B", "sales_order_item": "SOI-B", "bom_level": 0,
				}
			],
			active_bom_items={"FG", "SA"},
		)

		self.assertEqual(graph.work_orders_by_name["WO-SA-B"].parent_work_order, "WO-FG-B")
		self.assertEqual(graph.work_orders_by_name["WO-FG-A"].child_work_orders, [])

	def test_ambiguous_parent_is_not_linked_arbitrarily(self):
		from process_simplification.api.production_readiness import build_work_order_graph

		graph = build_work_order_graph(
			{"name": "PP-AMBIGUOUS", "posting_date": "2026-08-17"},
			[
				{"name": "WO-FG-1", "production_item": "FG", "production_plan_item": "PPI-1"},
				{"name": "WO-FG-2", "production_item": "FG", "production_plan_item": "PPI-1"},
				{
					"name": "WO-SA", "production_item": "SA",
					"production_plan_sub_assembly_item": "PPSA-1",
				},
			],
			[
				{"parent": "WO-FG-1", "item_code": "SA", "required_qty": 1},
				{"parent": "WO-FG-2", "item_code": "SA", "required_qty": 1},
			],
			[
				{
					"name": "PPSA-1", "production_item": "SA", "parent_item_code": "FG",
					"production_plan_item": "PPI-1", "bom_level": 0,
				}
			],
			active_bom_items={"FG", "SA"},
		)

		child = graph.work_orders_by_name["WO-SA"]
		self.assertIsNone(child.parent_work_order)
		self.assertTrue(child.graph_link_ambiguous)
		self.assertEqual(graph.work_orders_by_name["WO-FG-1"].child_work_orders, [])
		self.assertEqual(graph.work_orders_by_name["WO-FG-2"].child_work_orders, [])

	def test_serialized_plan_is_projected_to_one_sales_order_item(self):
		from process_simplification.api.production_readiness import _serialize_readiness_plan

		plan = frappe._dict(
			name="PP-MIXED",
			company="_Test Company",
			planned_date="2026-08-20",
			status="In Process",
			execution_order=["WO-A", "WO-B"],
			work_orders_by_name={
				"WO-A": frappe._dict(
					name="WO-A", sales_order_item="SOI-A", order_delivery_date="2026-08-10",
					readiness_status="ready_now",
				),
				"WO-B": frappe._dict(
					name="WO-B", sales_order_item="SOI-B", order_delivery_date="2026-08-20",
					readiness_status="purchase_shortage",
				),
			},
		)

		serialized = _serialize_readiness_plan(plan, sales_order_item="SOI-A")

		self.assertEqual([row["name"] for row in serialized["work_orders"]], ["WO-A"])
		self.assertEqual(serialized["summary"]["total_work_order_count"], 1)
		self.assertEqual(serialized["summary"]["purchase_shortage_work_order_count"], 0)
		self.assertEqual(serialized["material_priority_date"], "2026-08-10")


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

	def test_earlier_order_delivery_consumes_shared_raw_material_despite_later_plan_date(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		def plan(name, planned_date, creation, delivery_date):
			graph = self._graph(
				plan_name=f"PP-{name}",
				planned_date=planned_date,
				creation=creation,
				work_orders=[
					{
						"name": f"WO-{name}",
						"production_item": f"FG-{name}",
						"production_plan_item": f"PPI-{name}",
						"status": "Not Started",
						"sales_order": f"SO-{name}",
						"sales_order_item": f"SOI-{name}",
						"order_delivery_date": delivery_date,
						"order_creation": creation,
						"sales_order_item_idx": 1,
					}
				],
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
			return graph

		early_delivery = plan(
			"EARLY-DELIVERY",
			"2026-09-01",
			"2026-08-02 08:00:00",
			"2026-08-10",
		)
		late_delivery = plan(
			"LATE-DELIVERY",
			"2026-08-01",
			"2026-08-01 08:00:00",
			"2026-08-20",
		)
		result = allocate_work_order_readiness(
			[late_delivery, early_delivery],
			{("RM-SHARED", "Stores - TC"): {"available_qty": 10, "actual_qty": 10}},
		)
		by_plan = {row.name: row for row in result}

		self.assertEqual(
			by_plan["PP-EARLY-DELIVERY"].work_orders_by_name["WO-EARLY-DELIVERY"].readiness_status,
			"ready_now",
		)
		late_work_order = by_plan["PP-LATE-DELIVERY"].work_orders_by_name["WO-LATE-DELIVERY"]
		self.assertEqual(late_work_order.readiness_status, "purchase_shortage")
		self.assertEqual(late_work_order.required_items[0].available_qty, 3)
		self.assertEqual(late_work_order.required_items[0].current_gap_qty, 4)

	def test_supply_deadline_uses_order_delivery_instead_of_plan_date(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		def plan(name, planned_date, delivery_date):
			graph = self._graph(
				plan_name=f"PP-{name}",
				planned_date=planned_date,
				creation="2026-08-01 08:00:00",
				work_orders=[
					{
						"name": f"WO-{name}",
						"production_item": f"FG-{name}",
							"production_plan_item": f"PPI-{name}",
							"status": "Not Started",
							"sales_order": f"SO-{name}",
							"sales_order_item": f"SOI-{name}",
							"order_delivery_date": delivery_date,
							"order_creation": "2026-08-01 08:00:00",
							"sales_order_item_idx": 1,
						}
				],
				required_items=[
					{
						"parent": f"WO-{name}",
						"item_code": "RM-SHARED",
						"source_warehouse": "Stores - TC",
						"required_qty": 10,
						"transferred_qty": 0,
					}
				],
				active_bom_items={f"FG-{name}"},
			)
			return graph

		supply = {
			("RM-SHARED", "Stores - TC"): [
				{
					"doctype": "Purchase Order",
					"name": "PO-001",
					"detail_name": "POI-001",
					"outstanding_qty": 10,
					"schedule_date": "2026-08-15",
				}
			]
		}
		result = allocate_work_order_readiness(
			[
				plan("EARLY-DELIVERY", "2026-09-01", "2026-08-10"),
				plan("LATE-DELIVERY", "2026-08-01", "2026-08-20"),
			],
			{("RM-SHARED", "Stores - TC"): {"actual_qty": 0, "available_qty": 0}},
			supply,
		)
		by_plan = {row.name: row for row in result}
		early_item = by_plan["PP-EARLY-DELIVERY"].work_orders_by_name["WO-EARLY-DELIVERY"].required_items[0]
		later_item = by_plan["PP-LATE-DELIVERY"].work_orders_by_name["WO-LATE-DELIVERY"].required_items[0]

		self.assertEqual(early_item.status, "new_purchase_required")
		self.assertTrue(early_item.supply_documents[0].is_late)
		self.assertEqual(early_item.shortage_qty, 10)
		self.assertEqual(later_item.status, "awaiting_purchase_receipt")
		self.assertFalse(later_item.supply_documents[0].is_late)
		self.assertEqual(later_item.shortage_qty, 0)

	def test_work_order_reservation_is_not_reassigned_to_an_earlier_order(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		def graph(name, delivery_date, reserved_qty):
			return self._graph(
				plan_name=f"PP-{name}",
				planned_date=delivery_date,
				creation="2026-08-01 08:00:00",
				work_orders=[{
					"name": f"WO-{name}",
					"production_item": f"FG-{name}",
					"production_plan_item": f"PPI-{name}",
					"status": "Not Started",
					"sales_order": f"SO-{name}",
					"sales_order_item": f"SOI-{name}",
					"order_delivery_date": delivery_date,
					"order_creation": "2026-08-01 08:00:00",
					"sales_order_item_idx": 1,
				}],
				required_items=[{
					"parent": f"WO-{name}",
					"item_code": "RM-RESERVED",
					"source_warehouse": "Stores - TC",
					"required_qty": 10,
					"transferred_qty": 0,
					"stock_reserved_qty": reserved_qty,
				}],
				active_bom_items={f"FG-{name}"},
			)

		result = allocate_work_order_readiness(
			[graph("EARLY", "2026-08-10", 0), graph("LATE", "2026-08-20", 10)],
			{("RM-RESERVED", "Stores - TC"): {"actual_qty": 10, "available_qty": 10}},
		)
		by_plan = {row.name: row for row in result}
		early = by_plan["PP-EARLY"].work_orders_by_name["WO-EARLY"].required_items[0]
		late = by_plan["PP-LATE"].work_orders_by_name["WO-LATE"].required_items[0]

		self.assertEqual(early.available_qty, 0)
		self.assertEqual(early.shortage_qty, 10)
		self.assertEqual(late.available_qty, 10)
		self.assertEqual(late.shortage_qty, 0)

	def test_external_production_reservation_is_not_reallocated_to_plan_work_order(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		graph = self._graph(
			plan_name="PP-ORDER",
			planned_date="2026-08-10",
			creation="2026-08-01 08:00:00",
			work_orders=[
				{
					"name": "WO-ORDER",
					"production_item": "FG-ORDER",
					"production_plan_item": "PPI-ORDER",
					"status": "Not Started",
				}
			],
			required_items=[
				{
					"parent": "WO-ORDER",
					"item_code": "RM-SHARED",
					"source_warehouse": "Stores - TC",
					"required_qty": 8,
					"transferred_qty": 0,
					"stock_reserved_qty": 4,
				}
			],
			active_bom_items={"FG-ORDER"},
		)

		item = allocate_work_order_readiness(
			[graph],
			{
				("RM-SHARED", "Stores - TC"): {
					"can_calculate": True,
					"actual_qty": 10,
					"available_qty": 10,
					"free_qty": 0,
					# Eight belong to the loaded Work Order; three belong to an
					# external Work Order that must keep its priority.
					"production_committed_qty": 11,
				}
			},
		)[0].work_orders_by_name["WO-ORDER"].required_items[0]

		self.assertEqual(item.available_qty, 7)
		self.assertEqual(item.current_gap_qty, 1)
		self.assertEqual(item.shortage_qty, 1)

	def test_v16_implicit_work_order_commitment_is_available_to_its_loaded_plan(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		graph = self._graph(
			plan_name="PP-V16",
			planned_date="2026-08-10",
			creation="2026-08-01 08:00:00",
			work_orders=[{
				"name": "WO-V16", "production_item": "FG-V16",
				"production_plan_item": "PPI-V16", "status": "Not Started",
			}],
			required_items=[{
				"parent": "WO-V16", "item_code": "RM-V16",
				"source_warehouse": "Stores - TC", "required_qty": 10,
				"transferred_qty": 0, "stock_reserved_qty": 0,
			}],
			active_bom_items={"FG-V16"},
		)

		work_order = allocate_work_order_readiness(
			[graph],
			{
				("RM-V16", "Stores - TC"): {
					"actual_qty": 10,
					"available_qty": 10,
					"free_qty": 0,
					"production_committed_qty": 10,
				}
			},
		)[0].work_orders_by_name["WO-V16"]

		self.assertEqual(work_order.readiness_status, "ready_now")
		self.assertEqual(work_order.required_items[0].available_qty, 10)
		self.assertEqual(work_order.required_items[0].current_gap_qty, 0)

	def test_loaded_subassembly_plan_reservation_does_not_hide_existing_stock(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		graph = self._graph(
			plan_name="PP-SUBASSEMBLY",
			planned_date="2026-08-10",
			creation="2026-08-01 08:00:00",
			work_orders=[
				{
					"name": "WO-FG",
					"production_item": "FG",
					"production_plan_item": "PPI-FG",
					"status": "Not Started",
				},
				{
					"name": "WO-SA",
					"production_item": "SA",
					"production_plan_sub_assembly_item": "PPSA-SA",
					"status": "Not Started",
				},
			],
			required_items=[{
				"parent": "WO-FG",
				"item_code": "SA",
				"source_warehouse": "Stores - TC",
				"required_qty": 2,
				"transferred_qty": 0,
				"stock_reserved_qty": 0,
			}],
			sub_assemblies=[{
				"name": "PPSA-SA",
				"production_item": "SA",
				"parent_item_code": "FG",
				"bom_level": 0,
				"qty": 1,
				"required_qty": 2,
				"wo_produced_qty": 0,
				"fg_warehouse": "Stores - TC",
			}],
			active_bom_items={"FG", "SA"},
		)

		work_order = allocate_work_order_readiness(
			[graph],
			{
				("SA", "Stores - TC"): {
					"actual_qty": 1,
					"available_qty": 1,
					"free_qty": 0,
					# Parent Work Order requires two.  ERPNext v16 also
					# records the loaded plan's one-unit child output in the
					# same aggregate production commitment.
					"production_committed_qty": 3,
				}
			},
		)[0].work_orders_by_name["WO-FG"]

		self.assertEqual(graph.plan_reservations[("SA", "Stores - TC")], 1)
		self.assertEqual(work_order.required_items[0].available_qty, 1)
		self.assertEqual(work_order.required_items[0].current_gap_qty, 1)
		self.assertEqual(work_order.readiness_status, "waiting_subassembly")

	def test_unloaded_subassembly_plan_reservation_is_not_reallocated(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		graph = self._graph(
			plan_name="PP-LOADED",
			planned_date="2026-08-10",
			creation="2026-08-01 08:00:00",
			work_orders=[
				{
					"name": "WO-FG",
					"production_item": "FG",
					"production_plan_item": "PPI-FG",
					"status": "Not Started",
				},
				{
					"name": "WO-SA",
					"production_item": "SA",
					"production_plan_sub_assembly_item": "PPSA-SA",
					"status": "Not Started",
				},
			],
			required_items=[{
				"parent": "WO-FG",
				"item_code": "SA",
				"source_warehouse": "Stores - TC",
				"required_qty": 2,
			}],
			sub_assemblies=[{
				"name": "PPSA-SA",
				"production_item": "SA",
				"parent_item_code": "FG",
				"bom_level": 0,
				"qty": 1,
				"wo_produced_qty": 0,
				"fg_warehouse": "Stores - TC",
			}],
			active_bom_items={"FG", "SA"},
		)

		item = allocate_work_order_readiness(
			[graph],
			{
				("SA", "Stores - TC"): {
					"actual_qty": 1,
					"available_qty": 1,
					"free_qty": 0,
					# One additional unit belongs to an unloaded plan and
					# must remain unavailable to this graph.
					"production_committed_qty": 4,
				}
			},
		)[0].work_orders_by_name["WO-FG"].required_items[0]

		self.assertEqual(item.available_qty, 0)
		self.assertEqual(item.current_gap_qty, 2)

	def test_terminal_work_order_does_not_consume_shared_stock(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		def graph(name, status, delivery_date):
			return self._graph(
				plan_name=f"PP-{name}", planned_date=delivery_date, creation="2026-08-01 08:00:00",
				work_orders=[{
					"name": f"WO-{name}", "production_item": f"FG-{name}",
					"production_plan_item": f"PPI-{name}", "status": status,
					"sales_order": f"SO-{name}", "sales_order_item": f"SOI-{name}",
					"order_delivery_date": delivery_date, "order_creation": "2026-08-01 08:00:00",
					"sales_order_item_idx": 1,
				}],
				required_items=[{
					"parent": f"WO-{name}", "item_code": "RM-TERMINAL",
					"source_warehouse": "Stores - TC", "required_qty": 10, "transferred_qty": 0,
				}], active_bom_items={f"FG-{name}"},
			)

		result = allocate_work_order_readiness(
			[graph("STOPPED", "Stopped", "2026-08-10"), graph("ACTIVE", "Not Started", "2026-08-20")],
			{("RM-TERMINAL", "Stores - TC"): {"actual_qty": 10, "available_qty": 10}},
		)
		by_plan = {row.name: row for row in result}

		self.assertEqual(by_plan["PP-STOPPED"].work_orders_by_name["WO-STOPPED"].readiness_status, "blocked")
		self.assertEqual(by_plan["PP-ACTIVE"].work_orders_by_name["WO-ACTIVE"].required_items[0].available_qty, 10)

	def test_skip_transfer_uses_consumed_quantity_for_remaining_requirement(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		graph = self._graph(
			plan_name="PP-SKIP", planned_date="2026-08-10", creation="2026-08-01 08:00:00",
			work_orders=[{
				"name": "WO-SKIP", "production_item": "FG-SKIP", "production_plan_item": "PPI-SKIP",
				"status": "Not Started", "skip_transfer": 1,
			}],
			required_items=[{
				"parent": "WO-SKIP", "item_code": "RM-SKIP", "source_warehouse": "Stores - TC",
				"required_qty": 10, "transferred_qty": 0, "consumed_qty": 6,
			}], active_bom_items={"FG-SKIP"},
		)

		item = allocate_work_order_readiness(
			[graph], {("RM-SKIP", "Stores - TC"): {"actual_qty": 4, "available_qty": 4}},
		)[0].work_orders_by_name["WO-SKIP"].required_items[0]

		self.assertEqual(item.required_qty, 4)
		self.assertEqual(item.available_qty, 4)
		self.assertEqual(item.shortage_qty, 0)

	def test_missing_source_warehouse_blocks_readiness_instead_of_creating_purchase_shortage(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		graph = self._graph(
			plan_name="PP-MISSING-WAREHOUSE",
			planned_date="2026-08-10",
			creation="2026-08-01 08:00:00",
			work_orders=[
				{
					"name": "WO-MISSING-WAREHOUSE",
					"production_item": "FG-MISSING-WAREHOUSE",
					"production_plan_item": "PPI-MISSING-WAREHOUSE",
					"status": "Not Started",
				}
			],
			required_items=[
				{
					"parent": "WO-MISSING-WAREHOUSE",
					"item_code": "RM-MISSING-WAREHOUSE",
					"source_warehouse": None,
					"required_qty": 5,
					"transferred_qty": 0,
				}
			],
			active_bom_items={"FG-MISSING-WAREHOUSE"},
		)

		work_order = allocate_work_order_readiness(
			[graph],
			{("RM-MISSING-WAREHOUSE", None): {"can_calculate": False, "available_qty": 0}},
		)[0].work_orders_by_name["WO-MISSING-WAREHOUSE"]
		item = work_order.required_items[0]

		self.assertTrue(item.blocked)
		self.assertEqual(item.status, "cannot_calculate")
		self.assertEqual(item.shortage_qty, 0)
		self.assertEqual(work_order.readiness_status, "blocked")

	def test_unverifiable_supply_does_not_cover_order_deadline(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		graph = self._graph(
			plan_name="PP-UNKNOWN", planned_date="2026-08-10", creation="2026-08-01 08:00:00",
			work_orders=[{
				"name": "WO-UNKNOWN", "production_item": "FG-UNKNOWN",
				"production_plan_item": "PPI-UNKNOWN", "status": "Not Started",
				"sales_order": "SO-UNKNOWN", "sales_order_item": "SOI-UNKNOWN",
				"order_delivery_date": None, "order_creation": "2026-08-01 08:00:00",
				"sales_order_item_idx": 1,
			}],
			required_items=[{
				"parent": "WO-UNKNOWN", "item_code": "RM-UNKNOWN", "source_warehouse": "Stores - TC",
				"required_qty": 10, "transferred_qty": 0,
			}], active_bom_items={"FG-UNKNOWN"},
		)
		supply = {("RM-UNKNOWN", "Stores - TC"): [{
			"doctype": "Purchase Order", "name": "PO-UNKNOWN", "detail_name": "POI-UNKNOWN",
			"outstanding_qty": 10, "schedule_date": None,
		}]}

		item = allocate_work_order_readiness(
			[graph], {("RM-UNKNOWN", "Stores - TC"): {"actual_qty": 0, "available_qty": 0}}, supply,
		)[0].work_orders_by_name["WO-UNKNOWN"].required_items[0]

		self.assertEqual(item.open_purchase_order_qty, 0)
		self.assertEqual(item.shortage_qty, 10)
		self.assertTrue(item.supply_documents[0].deadline_unknown)

	def test_missing_order_delivery_does_not_consume_stock_before_dated_order(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		def graph(name, planned_date, delivery_date):
			return self._graph(
				plan_name=f"PP-{name}",
				planned_date=planned_date,
				creation="2026-08-01 08:00:00",
				work_orders=[
					{
						"name": f"WO-{name}",
						"production_item": f"FG-{name}",
						"production_plan_item": f"PPI-{name}",
						"status": "Not Started",
						"sales_order": f"SO-{name}",
						"sales_order_item": f"SOI-{name}",
						"order_delivery_date": delivery_date,
						"order_creation": "2026-08-01 08:00:00",
						"sales_order_item_idx": 1,
					}
				],
				required_items=[
					{
						"parent": f"WO-{name}",
						"item_code": "RM-SHARED",
						"source_warehouse": "Stores - TC",
						"required_qty": 7,
						"transferred_qty": 0,
					}
				],
				active_bom_items={f"FG-{name}"},
			)

		result = allocate_work_order_readiness(
			[
				graph("MISSING", "2026-08-01", None),
				graph("DATED", "2026-09-01", "2026-08-10"),
			],
			{("RM-SHARED", "Stores - TC"): {"available_qty": 7, "actual_qty": 7}},
		)
		by_plan = {row.name: row for row in result}

		self.assertEqual(by_plan["PP-DATED"].work_orders_by_name["WO-DATED"].readiness_status, "ready_now")
		self.assertEqual(
			by_plan["PP-MISSING"].work_orders_by_name["WO-MISSING"].readiness_status,
			"purchase_shortage",
		)

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

		work_order_field_queries = []
		work_order_filter_queries = []

		rows = {
			"Work Order": [
				frappe._dict(
					name="WO-FG",
					production_item="FG",
					bom_no="BOM-FG-001",
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
					bom_no="BOM-SA-001",
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
			"Sales Order Item": [frappe._dict(name="SOI-001", parent="SO-001", delivery_date="2026-08-10", idx=1)],
			"Sales Order": [frappe._dict(name="SO-001", creation="2026-08-01 08:00:00")],
			"Item": [frappe._dict(name="SA", is_purchase_item=1), frappe._dict(name="RM", is_purchase_item=1)],
		}

		def get_all(doctype, **kwargs):
			if doctype == "BOM":
				return ["FG", "SA"]
			if doctype == "Work Order":
				work_order_field_queries.append(kwargs.get("fields") or [])
				work_order_filter_queries.append(kwargs.get("filters") or {})
			return rows.get(doctype, [])

		def stock(item_code, warehouse):
			if item_code == "RM":
				return frappe._dict(can_calculate=True, actual_qty=12, committed_qty=2, available_qty=10)
			return frappe._dict(can_calculate=True, actual_qty=0, committed_qty=0, available_qty=0)

		with (
			patch.object(production_readiness.frappe, "get_list", side_effect=get_all),
			patch.object(production_readiness.frappe, "get_all", side_effect=get_all),
			patch("process_simplification.api.shortage.get_material_stock_snapshot", side_effect=stock),
			patch("process_simplification.api.shortage._mr_documents", return_value=[]),
			patch("process_simplification.api.shortage._po_documents", return_value=[]),
		):
			result = production_readiness.get_production_plan_readiness(
				company="_Test Company",
				sales_order_items=["SOI-001"],
			)

		plans = result["SOI-001"]
		self.assertEqual(len(plans), 1)
		self.assertEqual(plans[0]["name"], "PP-001")
		self.assertEqual(plans[0]["planned_date"], "2026-08-20 08:00:00")
		self.assertEqual(plans[0]["material_priority_date"], "2026-08-10")
		self.assertEqual([row["name"] for row in plans[0]["work_orders"]], ["WO-SA", "WO-FG"])
		self.assertEqual([row["bom_no"] for row in plans[0]["work_orders"]], ["BOM-SA-001", "BOM-FG-001"])
		self.assertEqual(plans[0]["work_orders"][0]["readiness_status"], "ready_now")
		self.assertEqual(plans[0]["work_orders"][0]["required_items"][0]["committed_qty"], 2)
		self.assertEqual(plans[0]["work_orders"][1]["readiness_status"], "waiting_subassembly")
		self.assertEqual(plans[0]["summary"]["ready_work_order_count"], 1)
		self.assertEqual(len(work_order_field_queries), 1)
		self.assertTrue(all("bom_no" in fields for fields in work_order_field_queries))
		self.assertTrue(all("skip_transfer" in fields for fields in work_order_field_queries))
		self.assertTrue(all("sales_order_item" not in filters for filters in work_order_filter_queries))

	def test_selected_order_items_are_filtered_only_after_company_wide_allocation(self):
		from process_simplification.api import production_readiness

		def work_order(name, plan, sales_order, sales_order_item):
			return frappe._dict(
				name=name, production_item=f"FG-{name}", bom_no=f"BOM-{name}",
				production_plan=plan, production_plan_item=f"PPI-{name}",
				production_plan_sub_assembly_item=None, sales_order=sales_order,
				sales_order_item=sales_order_item, company="_Test Company",
				status="Not Started", skip_transfer=0, qty=1, produced_qty=0,
				planned_start_date="2026-08-20", creation="2026-08-01 09:00:00",
			)

		rows = {
			"Work Order": [
				work_order("WO-EARLY", "PP-EARLY", "SO-EARLY", "SOI-EARLY"),
				work_order("WO-LATE", "PP-LATE", "SO-LATE", "SOI-LATE"),
			],
			"Work Order Item": [
				frappe._dict(parent="WO-EARLY", item_code="RM-SHARED", source_warehouse="Stores - TC", required_qty=7, transferred_qty=0, consumed_qty=0, stock_reserved_qty=0),
				frappe._dict(parent="WO-LATE", item_code="RM-SHARED", source_warehouse="Stores - TC", required_qty=7, transferred_qty=0, consumed_qty=0, stock_reserved_qty=0),
			],
			"Production Plan": [
				frappe._dict(name="PP-EARLY", company="_Test Company", posting_date="2026-08-01", creation="2026-08-01 08:00:00", status="In Process"),
				frappe._dict(name="PP-LATE", company="_Test Company", posting_date="2026-08-01", creation="2026-08-01 08:00:01", status="In Process"),
			],
			"Production Plan Item": [
				frappe._dict(name="PPI-WO-EARLY", parent="PP-EARLY", item_code="FG-WO-EARLY", planned_start_date="2026-08-20", sales_order="SO-EARLY", sales_order_item="SOI-EARLY"),
				frappe._dict(name="PPI-WO-LATE", parent="PP-LATE", item_code="FG-WO-LATE", planned_start_date="2026-08-20", sales_order="SO-LATE", sales_order_item="SOI-LATE"),
			],
			"Production Plan Sub Assembly Item": [],
			"Sales Order Item": [
				frappe._dict(name="SOI-EARLY", parent="SO-EARLY", delivery_date="2026-08-10", idx=1),
				frappe._dict(name="SOI-LATE", parent="SO-LATE", delivery_date="2026-08-20", idx=1),
			],
			"Sales Order": [
				frappe._dict(name="SO-EARLY", creation="2026-08-01 07:00:00"),
				frappe._dict(name="SO-LATE", creation="2026-08-01 07:00:01"),
			],
			"Item": [frappe._dict(name="RM-SHARED", is_purchase_item=1)],
		}

		def get_all(doctype, **kwargs):
			if doctype == "BOM":
				return ["FG-WO-EARLY", "FG-WO-LATE"]
			return rows.get(doctype, [])

		with (
			patch.object(production_readiness.frappe, "get_list", side_effect=get_all),
			patch.object(production_readiness.frappe, "get_all", side_effect=get_all),
			patch("process_simplification.api.shortage.get_material_stock_snapshot", return_value=frappe._dict(actual_qty=7, committed_qty=0, available_qty=7)),
			patch("process_simplification.api.shortage._mr_documents", return_value=[]),
			patch("process_simplification.api.shortage._po_documents", return_value=[]),
		):
			result = production_readiness.get_production_plan_readiness(
				company="_Test Company", sales_order_items=["SOI-LATE"],
			)

		self.assertEqual(set(result), {"SOI-LATE"})
		item = result["SOI-LATE"][0]["work_orders"][0]["required_items"][0]
		self.assertEqual(item["available_qty"], 0)
		self.assertEqual(item["shortage_qty"], 7)

	def test_parent_documents_are_loaded_with_user_permissions(self):
		from process_simplification.api import production_readiness

		rows = {
			"Work Order": [frappe._dict(
				name="WO-ALLOWED", production_item="FG", bom_no="BOM-FG", production_plan="PP-ALLOWED",
				production_plan_item="PPI-ALLOWED", production_plan_sub_assembly_item=None,
				sales_order="SO-ALLOWED", sales_order_item="SOI-ALLOWED", company="_Test Company",
				status="Not Started", skip_transfer=0, qty=1, produced_qty=0,
				planned_start_date="2026-08-20", creation="2026-08-01 09:00:00",
			)],
			"Production Plan": [frappe._dict(
				name="PP-ALLOWED", company="_Test Company", posting_date="2026-08-17",
				creation="2026-08-01 08:00:00", status="In Process",
			)],
			"Sales Order": [frappe._dict(name="SO-ALLOWED", creation="2026-08-01 07:00:00")],
			"Work Order Item": [],
			"Production Plan Item": [frappe._dict(
				name="PPI-ALLOWED", parent="PP-ALLOWED", item_code="FG", planned_start_date="2026-08-20",
				sales_order="SO-ALLOWED", sales_order_item="SOI-ALLOWED",
			)],
			"Production Plan Sub Assembly Item": [],
			"Sales Order Item": [frappe._dict(
				name="SOI-ALLOWED", parent="SO-ALLOWED", delivery_date="2026-08-20", idx=1,
			)],
			"Item": [frappe._dict(name="FG", is_purchase_item=0)],
		}
		permission_queries = []

		def get_list(doctype, **kwargs):
			permission_queries.append(doctype)
			return rows.get(doctype, [])

		def get_all(doctype, **kwargs):
			if doctype in {"Work Order", "Production Plan", "Sales Order"}:
				raise AssertionError(f"{doctype} must be loaded with get_list")
			if doctype == "BOM":
				return ["FG"]
			return rows.get(doctype, [])

		with (
			patch.object(production_readiness.frappe, "get_list", side_effect=get_list),
			patch.object(production_readiness.frappe, "get_all", side_effect=get_all),
			patch("process_simplification.api.shortage.get_material_stock_snapshot", return_value=frappe._dict(actual_qty=0, available_qty=0)),
			patch("process_simplification.api.shortage._mr_documents", return_value=[]),
			patch("process_simplification.api.shortage._po_documents", return_value=[]),
		):
			result = production_readiness.get_production_plan_readiness(company="_Test Company")

		self.assertEqual(set(result), {"SOI-ALLOWED"})
		self.assertEqual(permission_queries, ["Work Order", "Production Plan", "Sales Order"])
