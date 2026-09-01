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


class TestWorkOrderExecutionFacts(UnitTestCase):
	def test_allocation_conflict_caps_priority_sources_at_the_actual_gap(self):
		from process_simplification.api.production_readiness import build_allocation_conflict

		conflict = build_allocation_conflict(
			current_gap_qty=4,
			gap_without_priority_qty=1,
			other_hard_reserved_qty=5,
			prior_allocations=[
				frappe._dict(
					work_order="WO-EARLY-A", sales_order="SO-A",
					delivery_date="2026-08-10", allocated_qty=2,
				),
				frappe._dict(
					work_order="WO-EARLY-B", sales_order="SO-B",
					delivery_date="2026-08-11", allocated_qty=5,
				),
			],
		)

		self.assertEqual(conflict.priority_impact_qty, 3)
		self.assertEqual(conflict.hard_reserved_impact_qty, 1)
		self.assertEqual(
			[(row["work_order"], row["allocated_qty"], row["impact_qty"]) for row in conflict.sources],
			[("WO-EARLY-A", 2, 2), ("WO-EARLY-B", 5, 1)],
		)
		self.assertEqual(sum(row["impact_qty"] for row in conflict.sources), 3)
		self.assertLessEqual(
			conflict.priority_impact_qty + conflict.hard_reserved_impact_qty,
			4,
		)

	def test_attaches_active_replenishment_to_exact_work_order_item(self):
		from process_simplification.api.production_readiness import (
			attach_replenishment_work_orders,
		)

		items = [
			frappe._dict(name="WOI-TARGET", parent="WO-PARENT", item_code="SA"),
			frappe._dict(name="WOI-OTHER", parent="WO-PARENT", item_code="RM"),
		]
		attach_replenishment_work_orders(
			items,
			[
				frappe._dict(
					name="WO-REPLENISH",
					production_item="SA",
					custom_replenishes_work_order="WO-PARENT",
					custom_replenishes_work_order_item="WOI-TARGET",
				)
			],
		)

		self.assertEqual(items[0].supply_work_orders, ["WO-REPLENISH"])
		self.assertTrue(items[0].replenishment_in_progress)
		self.assertEqual(items[1].supply_work_orders, [])

	def test_source_reservation_uses_exact_detail_item_and_warehouse(self):
		from process_simplification.api.production_readiness import (
			attach_work_order_source_reservations,
		)

		items = [
			frappe._dict(
				name="WOI-RM", parent="WO-001", item_code="RM-001",
				source_warehouse="Stores - TC", stock_reserved_qty=20,
			),
			frappe._dict(
				name="WOI-WIP", parent="WO-SKIP", item_code="RM-WIP",
				source_warehouse="Stores - TC", issue_warehouse="WIP - TC",
			),
		]
		attach_work_order_source_reservations(
			items,
			[
				frappe._dict(
					voucher_detail_no="WOI-RM", item_code="RM-001",
					warehouse="Stores - TC", reserved_qty=8,
					delivered_qty=1, transferred_qty=2, consumed_qty=1,
				),
				frappe._dict(
					voucher_detail_no="WOI-RM", item_code="RM-001",
					warehouse="Stores - TC", reserved_qty=3,
					delivered_qty=0, transferred_qty=0, consumed_qty=0,
				),
				# The aggregate child-row field includes this WIP reservation,
				# but it is no longer stock that can be issued from Stores.
				frappe._dict(
					voucher_detail_no="WOI-RM", item_code="RM-001",
					warehouse="Work In Progress - TC", reserved_qty=10,
					delivered_qty=0, transferred_qty=0, consumed_qty=0,
				),
				frappe._dict(
					voucher_detail_no="WOI-OTHER", item_code="RM-001",
					warehouse="Stores - TC", reserved_qty=7,
				),
				frappe._dict(
					voucher_detail_no="WOI-RM", item_code="RM-OTHER",
					warehouse="Stores - TC", reserved_qty=11,
				),
				frappe._dict(
					voucher_detail_no="WOI-WIP", item_code="RM-WIP",
					warehouse="WIP - TC", reserved_qty=4, transferred_qty=1,
				),
			],
		)

		self.assertEqual(items[0].source_reserved_qty, 7)
		self.assertEqual(items[0].stock_reserved_qty, 20)
		self.assertEqual(items[1].source_reserved_qty, 3)

	def test_attaches_job_card_progress_and_matching_draft_stock_entries(self):
		from process_simplification.api.production_readiness import (
			attach_work_order_execution_facts,
		)

		work_orders = [frappe._dict(name="WO-001")]
		attach_work_order_execution_facts(
			work_orders,
			job_cards=[
				frappe._dict(
					name="JC-002", work_order="WO-001", status="Open",
					creation="2026-08-01 09:00:02",
				),
				frappe._dict(
					name="JC-001", work_order="WO-001", status="Completed", docstatus=1,
					creation="2026-08-01 09:00:01",
				),
			],
			draft_stock_entries=[
				frappe._dict(
					name="STE-ISSUE", work_order="WO-001",
					purpose="Material Transfer for Manufacture",
				),
				frappe._dict(
					name="STE-RECEIPT", work_order="WO-001", purpose="Manufacture",
				),
			],
		)

		work_order = work_orders[0]
		self.assertEqual(work_order.job_card_count, 2)
		self.assertEqual(work_order.completed_job_card_count, 1)
		self.assertEqual(work_order.job_card_names, ["JC-001", "JC-002"])
		self.assertEqual(work_order.incomplete_job_card_names, ["JC-002"])
		self.assertEqual(work_order.draft_issue_stock_entry, "STE-ISSUE")
		self.assertEqual(work_order.draft_receipt_stock_entry, "STE-RECEIPT")

	def test_ready_stock_requires_issue_before_dispatch(self):
		from process_simplification.api.production_readiness import derive_work_order_flow_state

		state = derive_work_order_flow_state(
			frappe._dict(
				name="WO-READY",
				status="Not Started",
				qty=5,
				produced_qty=0,
				process_loss_qty=0,
				readiness_status="ready_now",
				job_card_count=1,
				completed_job_card_count=0,
				required_items=[
					frappe._dict(
						original_required_qty=10,
						remaining_issue_qty=10,
						net_transferred_qty=0,
						available_qty=10,
						current_gap_qty=0,
						status="ready_now",
					)
				],
			)
		)

		self.assertEqual(state["issue_state"]["code"], "ready")
		self.assertEqual(state["issue_state"]["additional_issueable_qty"], 5)
		self.assertEqual(state["flow_status"], "ready_for_issue")
		self.assertTrue(state["can_request_issue"])
		self.assertFalse(state["can_dispatch"])
		self.assertEqual(state["receipt_state"]["code"], "not_ready")

	def test_partial_material_coverage_is_an_explicit_partial_issue_action(self):
		from process_simplification.api.production_readiness import derive_work_order_flow_state

		state = derive_work_order_flow_state(
			frappe._dict(
				name="WO-PARTIAL",
				status="Not Started",
				qty=5,
				readiness_status="purchase_shortage",
				required_items=[
					frappe._dict(
						original_required_qty=10,
						remaining_issue_qty=10,
						net_transferred_qty=0,
						available_qty=4,
						current_gap_qty=6,
					)
				],
			)
		)

		self.assertEqual(state["issue_state"]["code"], "partially_ready")
		self.assertAlmostEqual(state["issue_state"]["issueable_fraction"], 0.4)
		self.assertAlmostEqual(state["issue_state"]["additional_issueable_qty"], 2)
		self.assertEqual(state["flow_status"], "partially_ready_for_issue")
		self.assertTrue(state["can_request_issue"])
		self.assertFalse(state["can_dispatch"])

	def test_remaining_finished_issue_qty_uses_the_limiting_net_material_coverage(self):
		from process_simplification.api.production_readiness import derive_work_order_flow_state

		state = derive_work_order_flow_state(
			frappe._dict(
				status="Not Started",
				qty=10,
				# This aggregate can be stale after returns and must not drive the result.
				material_transferred_for_manufacturing=10,
				required_items=[
					frappe._dict(
						original_required_qty=20,
						remaining_issue_qty=10,
						net_transferred_qty=10,
						available_qty=10,
					),
					frappe._dict(
						original_required_qty=5,
						remaining_issue_qty=0,
						net_transferred_qty=5,
						available_qty=0,
					),
				],
			)
		)

		self.assertAlmostEqual(state["issue_state"]["net_transfer_coverage_fraction"], 0.5)
		self.assertAlmostEqual(state["issue_state"]["additional_issueable_qty"], 5)
		self.assertFalse(state["can_dispatch"])

	def test_operation_split_rows_count_aggregate_transfer_only_once(self):
		from process_simplification.api.production_readiness import derive_work_order_flow_state

		state = derive_work_order_flow_state(
			frappe._dict(
				status="Not Started",
				qty=10,
				material_transferred_for_manufacturing=6,
				required_items=[
					frappe._dict(
						item_code="RM-SPLIT", source_warehouse="Stores - TC",
						original_required_qty=5, net_transferred_qty=6, available_qty=2,
					),
					frappe._dict(
						item_code="RM-SPLIT", source_warehouse="Stores - TC",
						original_required_qty=5, net_transferred_qty=6, available_qty=2,
					),
				],
			)
		)

		self.assertEqual(state["net_issued_qty"], 6)
		self.assertEqual(state["remaining_issue_qty"], 4)
		self.assertAlmostEqual(state["issue_state"]["net_transfer_coverage_fraction"], 0.6)
		self.assertAlmostEqual(state["issue_state"]["issueable_fraction"], 1)
		self.assertAlmostEqual(state["issue_state"]["additional_issueable_qty"], 4)
		self.assertFalse(state["can_dispatch"])

	def test_operation_split_skip_transfer_rows_count_consumption_only_once(self):
		from process_simplification.api.production_readiness import derive_work_order_flow_state

		state = derive_work_order_flow_state(
			frappe._dict(
				status="Not Started",
				qty=10,
				skip_transfer=1,
				operation_count=1,
				job_card_count=1,
				readiness_status="purchase_shortage",
				required_items=[
					frappe._dict(
						item_code="RM-DIRECT", source_warehouse="Stores - TC",
						original_required_qty=5, consumed_qty=6, available_qty=0,
					),
					frappe._dict(
						item_code="RM-DIRECT", source_warehouse="Stores - TC",
						original_required_qty=5, consumed_qty=6, available_qty=0,
					),
				],
			)
		)

		self.assertEqual(state["issue_state"]["code"], "not_required")
		self.assertEqual(state["net_issued_qty"], 6)
		self.assertEqual(state["remaining_issue_qty"], 4)
		self.assertFalse(state["can_dispatch"])
		self.assertNotEqual(state["flow_status"], "issued_waiting_dispatch")

	def test_completed_job_cards_and_net_issue_make_receipt_requestable(self):
		from process_simplification.api.production_readiness import derive_work_order_flow_state

		state = derive_work_order_flow_state(
			frappe._dict(
				name="WO-DONE",
				status="In Process",
				qty=5,
				produced_qty=0,
				process_loss_qty=0,
				material_transferred_for_manufacturing=5,
				readiness_status="in_progress",
				job_card_count=2,
				completed_job_card_count=2,
				required_items=[
					frappe._dict(
						original_required_qty=10,
						remaining_issue_qty=0,
						net_transferred_qty=10,
						current_gap_qty=0,
					)
				],
			)
		)

		self.assertEqual(state["operation_state"]["code"], "completed")
		self.assertEqual(state["issue_state"]["code"], "issued")
		self.assertEqual(state["receipt_state"]["code"], "requestable")
		self.assertEqual(state["flow_status"], "awaiting_receipt_request")
		self.assertTrue(state["can_dispatch"])
		self.assertTrue(state["can_request_receipt"])

	def test_receipt_capacity_uses_net_bom_coverage_after_return_and_reissue(self):
		from process_simplification.api.production_readiness import derive_work_order_flow_state

		state = derive_work_order_flow_state(
			frappe._dict(
				name="WO-RETURN-REISSUE",
				status="In Process",
				qty=100,
				produced_qty=40,
				process_loss_qty=0,
				# ERPNext's header is gross: issue 100, return 20, issue 20.
				material_transferred_for_manufacturing=120,
				job_card_count=1,
				completed_job_card_count=1,
				required_items=[
					frappe._dict(
						item_code="RM-001", source_warehouse="Stores - TC",
						original_required_qty=100, transferred_qty=120, returned_qty=20,
					)
				],
			)
		)

		self.assertEqual(state["issue_state"]["net_issued_qty"], 100)
		self.assertEqual(state["issue_state"]["net_transfer_coverage_fraction"], 1)
		self.assertEqual(state["receipt_state"]["remaining_qty"], 60)
		self.assertEqual(state["production_remaining_qty"], 60)
		self.assertEqual(state["receipt_state"]["code"], "requestable")
		self.assertTrue(state["can_request_receipt"])

	def test_matching_drafts_are_pending_and_disable_duplicate_requests(self):
		from process_simplification.api.production_readiness import derive_work_order_flow_state

		base = frappe._dict(
			status="In Process",
			qty=5,
			produced_qty=0,
			process_loss_qty=0,
			material_transferred_for_manufacturing=5,
			job_card_count=1,
			completed_job_card_count=1,
			required_items=[
				frappe._dict(
					original_required_qty=10,
					remaining_issue_qty=0,
					net_transferred_qty=10,
					current_gap_qty=0,
				)
			],
		)
		receipt_pending = derive_work_order_flow_state(
			frappe._dict(base, draft_receipt_stock_entry="STE-RECEIPT")
		)
		issue_pending = derive_work_order_flow_state(
			frappe._dict(
				base,
				draft_receipt_stock_entry=None,
				draft_issue_stock_entry="STE-ISSUE",
				required_items=[
					frappe._dict(
						original_required_qty=10,
						remaining_issue_qty=10,
						net_transferred_qty=0,
						current_gap_qty=0,
						status="ready_now",
					)
				],
			)
		)

		self.assertEqual(receipt_pending["receipt_state"]["code"], "draft_pending")
		self.assertFalse(receipt_pending["can_request_receipt"])
		self.assertEqual(issue_pending["issue_state"]["code"], "draft_pending")
		self.assertFalse(issue_pending["can_request_issue"])


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

	def test_operation_split_uses_submitted_group_fact_once_during_allocation(self):
		from process_simplification.api.production_readiness import (
			allocate_work_order_readiness,
			attach_work_order_item_issue_warehouses,
			attach_work_order_stock_facts,
		)

		work_orders = [
			frappe._dict(
				name="WO-SPLIT", production_item="FG", production_plan_item="PPI-1",
				status="Not Started", qty=10, skip_transfer=0,
			)
		]
		required_items = [
			frappe._dict(
				name="WOI-1", parent="WO-SPLIT", idx=1, item_code="RM",
				source_warehouse="Stores", required_qty=5, transferred_qty=10,
			),
			frappe._dict(
				name="WOI-2", parent="WO-SPLIT", idx=2, item_code="RM",
				source_warehouse="Stores", required_qty=5, transferred_qty=10,
			),
		]
		attach_work_order_item_issue_warehouses(work_orders, required_items)
		attach_work_order_stock_facts(
			work_orders,
			required_items,
			{
				("WO-SPLIT", "RM", "Stores"): frappe._dict(
					gross_issued_qty=6, returned_qty=0, net_issued_qty=6,
				)
			},
		)
		graph = self._graph(
			plan_name="PP-001",
			planned_date="2026-08-20",
			creation="2026-08-01 09:00:00",
			work_orders=work_orders,
			required_items=required_items,
			active_bom_items={"FG"},
		)

		work_order = allocate_work_order_readiness(
			[graph],
			{("RM", "Stores"): {"actual_qty": 4, "available_qty": 4}},
		)[0].work_orders_by_name["WO-SPLIT"]

		self.assertEqual(sum(item.required_qty for item in work_order.required_items), 4)
		self.assertEqual(sum(item.net_transferred_qty for item in work_order.required_items), 6)
		self.assertEqual(work_order.readiness_status, "ready_now")
		self.assertNotEqual(work_order.readiness_status, "materials_transferred")
		self.assertEqual(work_order.net_issued_qty, 6)
		self.assertEqual(work_order.remaining_issue_qty, 4)
		self.assertAlmostEqual(work_order.issue_state.net_transfer_coverage_fraction, 0.6)
		self.assertFalse(work_order.can_dispatch)

	def test_submitted_issue_for_one_source_does_not_cover_the_other_source(self):
		from process_simplification.api.production_readiness import (
			allocate_work_order_readiness,
			attach_work_order_item_issue_warehouses,
			attach_work_order_stock_facts,
		)

		work_orders = [
			frappe._dict(
				name="WO-TWO-SOURCES", production_item="FG", production_plan_item="PPI-1",
				status="Not Started", qty=10, skip_transfer=0,
			)
		]
		required_items = [
			frappe._dict(
				name="WOI-A", parent="WO-TWO-SOURCES", item_code="RM",
				source_warehouse="Stores-A", required_qty=5, transferred_qty=5,
			),
			frappe._dict(
				name="WOI-B", parent="WO-TWO-SOURCES", item_code="RM",
				source_warehouse="Stores-B", required_qty=5, transferred_qty=5,
			),
		]
		attach_work_order_item_issue_warehouses(work_orders, required_items)
		attach_work_order_stock_facts(
			work_orders,
			required_items,
			{
				("WO-TWO-SOURCES", "RM", "Stores-A"): frappe._dict(
					gross_issued_qty=5, returned_qty=0, net_issued_qty=5,
				)
			},
		)
		graph = self._graph(
			plan_name="PP-001",
			planned_date="2026-08-20",
			creation="2026-08-01 09:00:00",
			work_orders=work_orders,
			required_items=required_items,
			active_bom_items={"FG"},
		)

		work_order = allocate_work_order_readiness(
			[graph],
			{
				("RM", "Stores-A"): {"actual_qty": 0, "available_qty": 0},
				("RM", "Stores-B"): {"actual_qty": 0, "available_qty": 0},
			},
		)[0].work_orders_by_name["WO-TWO-SOURCES"]

		by_warehouse = {
			item.issue_warehouse: item for item in work_order.required_items
		}
		self.assertEqual(by_warehouse["Stores-A"].required_qty, 0)
		self.assertEqual(by_warehouse["Stores-B"].required_qty, 5)
		self.assertEqual(work_order.readiness_status, "purchase_shortage")
		self.assertEqual(work_order.net_issued_qty, 5)
		self.assertEqual(work_order.remaining_issue_qty, 5)

	def test_split_direct_consumption_uses_wip_fact_once(self):
		from process_simplification.api.production_readiness import (
			allocate_work_order_readiness,
			attach_work_order_item_issue_warehouses,
			attach_work_order_stock_facts,
		)

		work_orders = [
			frappe._dict(
				name="WO-DIRECT", production_item="FG", production_plan_item="PPI-1",
				status="Not Started", qty=10, skip_transfer=1,
				from_wip_warehouse=1, wip_warehouse="WIP",
			)
		]
		required_items = [
			frappe._dict(
				name="WOI-D1", parent="WO-DIRECT", idx=1, item_code="RM",
				source_warehouse="Stores", required_qty=5, consumed_qty=10,
			),
			frappe._dict(
				name="WOI-D2", parent="WO-DIRECT", idx=2, item_code="RM",
				source_warehouse="Stores", required_qty=5, consumed_qty=10,
			),
		]
		attach_work_order_item_issue_warehouses(work_orders, required_items)
		attach_work_order_stock_facts(
			work_orders,
			required_items,
			{
				("WO-DIRECT", "RM", "WIP"): frappe._dict(
					consumed_qty=6, gross_issued_qty=0, returned_qty=0,
					net_issued_qty=0,
				)
			},
		)
		graph = self._graph(
			plan_name="PP-001",
			planned_date="2026-08-20",
			creation="2026-08-01 09:00:00",
			work_orders=work_orders,
			required_items=required_items,
			active_bom_items={"FG"},
		)

		work_order = allocate_work_order_readiness(
			[graph],
			{
				("RM", "WIP"): {"actual_qty": 4, "available_qty": 4},
				("RM", "Stores"): {"actual_qty": 0, "available_qty": 0},
			},
		)[0].work_orders_by_name["WO-DIRECT"]

		self.assertEqual(sum(item.required_qty for item in work_order.required_items), 4)
		self.assertEqual(sum(item.net_transferred_qty for item in work_order.required_items), 6)
		self.assertTrue(all(item.issue_warehouse == "WIP" for item in work_order.required_items))
		self.assertEqual(work_order.remaining_issue_qty, 4)
		self.assertEqual(work_order.issue_state.code, "not_required")
		self.assertTrue(work_order.can_dispatch)

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

	def test_completed_child_with_remaining_gap_requires_a_replenishment_task(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		graph = self._graph(
			plan_name="PP-REPLENISH",
			planned_date="2026-08-20",
			creation="2026-08-01 09:00:00",
			work_orders=[
				{
					"name": "WO-FG", "production_item": "FG", "production_plan_item": "PPI-1",
					"status": "Not Started",
				},
				{
					"name": "WO-SA", "production_item": "SA",
					"production_plan_sub_assembly_item": "PPSA-1", "status": "Completed",
				},
			],
			required_items=[{
				"name": "WOI-SA", "parent": "WO-FG", "item_code": "SA",
				"source_warehouse": "Stores - TC", "required_qty": 5, "transferred_qty": 0,
			}],
			sub_assemblies=[{
				"name": "PPSA-1", "production_item": "SA", "parent_item_code": "FG",
				"bom_level": 0, "schedule_date": "2026-08-20",
			}],
			active_bom_items={"FG", "SA"},
		)

		parent = allocate_work_order_readiness(
			[graph], {("SA", "Stores - TC"): {"actual_qty": 0, "available_qty": 0}},
		)[0].work_orders_by_name["WO-FG"]
		item = parent.required_items[0]

		self.assertIsNone(item.child_work_order)
		self.assertEqual(item.completed_child_work_orders, ["WO-SA"])
		self.assertTrue(item.replenishment_required)
		self.assertEqual(parent.readiness_status, "replenishment_required")

	def test_active_replenishment_keeps_manufactured_gap_waiting(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		graph = self._graph(
			plan_name="PP-REPLENISH-ACTIVE",
			planned_date="2026-08-20",
			creation="2026-08-01 09:00:00",
			work_orders=[{
				"name": "WO-FG", "production_item": "FG", "production_plan_item": "PPI-1",
				"status": "Not Started",
			}],
			required_items=[{
				"name": "WOI-SA", "parent": "WO-FG", "item_code": "SA",
				"source_warehouse": "Stores - TC", "required_qty": 5, "transferred_qty": 0,
				"supply_work_orders": ["WO-REPLENISH"], "replenishment_in_progress": True,
			}],
			active_bom_items={"FG", "SA"},
		)

		parent = allocate_work_order_readiness(
			[graph], {("SA", "Stores - TC"): {"actual_qty": 0, "available_qty": 0}},
		)[0].work_orders_by_name["WO-FG"]

		self.assertEqual(parent.required_items[0].supply_work_orders, ["WO-REPLENISH"])
		self.assertTrue(parent.required_items[0].replenishment_in_progress)
		self.assertFalse(parent.required_items[0].replenishment_required)
		self.assertEqual(parent.readiness_status, "waiting_subassembly")

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
		conflict = late_work_order.required_items[0].allocation_conflict
		self.assertEqual(conflict.priority_impact_qty, 4)
		self.assertEqual(conflict.hard_reserved_impact_qty, 0)
		self.assertEqual(
			conflict.sources,
			[
				{
					"source_type": "priority_allocation",
					"work_order": "WO-EARLY-DELIVERY",
					"sales_order": "SO-EARLY-DELIVERY",
					"delivery_date": "2026-08-10",
					"allocated_qty": 7,
					"impact_qty": 4,
				}
			],
		)

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

	def test_wip_reservation_does_not_make_source_stock_issueable(self):
		from process_simplification.api.production_readiness import (
			allocate_work_order_readiness,
			attach_work_order_source_reservations,
		)

		required_items = [
			frappe._dict(
				name="WOI-RM", parent="WO-WIP", item_code="RM-WIP",
				source_warehouse="Stores - TC", required_qty=10,
				transferred_qty=4, stock_reserved_qty=4,
			)
		]
		attach_work_order_source_reservations(
			required_items,
			[
				frappe._dict(
					voucher_detail_no="WOI-RM", item_code="RM-WIP",
					warehouse="Work In Progress - TC", reserved_qty=4,
				)
			],
		)
		graph = self._graph(
			plan_name="PP-WIP",
			planned_date="2026-08-10",
			creation="2026-08-01 08:00:00",
			work_orders=[{
				"name": "WO-WIP", "production_item": "FG-WIP",
				"production_plan_item": "PPI-WIP", "status": "Not Started",
			}],
			required_items=required_items,
			active_bom_items={"FG-WIP"},
		)

		item = allocate_work_order_readiness(
			[graph],
			{("RM-WIP", "Stores - TC"): {"actual_qty": 0, "available_qty": 0}},
		)[0].work_orders_by_name["WO-WIP"].required_items[0]

		self.assertEqual(item.source_reserved_qty, 0)
		self.assertEqual(item.available_qty, 0)
		self.assertEqual(item.current_gap_qty, 6)
		self.assertEqual(item.shortage_qty, 6)

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
		self.assertEqual(early.allocation_conflict.priority_impact_qty, 0)
		self.assertEqual(early.allocation_conflict.hard_reserved_impact_qty, 10)
		self.assertEqual(early.allocation_conflict.sources, [])
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

	def test_skip_transfer_shortage_is_not_dispatchable_without_on_site_coverage(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		graph = self._graph(
			plan_name="PP-SKIP-SHORT",
			planned_date="2026-08-10",
			creation="2026-08-01 08:00:00",
			work_orders=[{
				"name": "WO-SKIP-SHORT", "production_item": "FG-SKIP",
				"production_plan_item": "PPI-SKIP", "status": "Not Started",
				"skip_transfer": 1, "qty": 5, "operation_count": 1,
				"job_card_count": 1, "completed_job_card_count": 0,
			}],
			required_items=[{
				"parent": "WO-SKIP-SHORT", "item_code": "RM-SKIP",
				"source_warehouse": "Stores - TC", "required_qty": 10,
				"consumed_qty": 0,
			}],
			active_bom_items={"FG-SKIP"},
		)

		work_order = allocate_work_order_readiness(
			[graph], {("RM-SKIP", "Stores - TC"): {"actual_qty": 4, "available_qty": 4}},
		)[0].work_orders_by_name["WO-SKIP-SHORT"]

		self.assertEqual(work_order.readiness_status, "purchase_shortage")
		self.assertEqual(work_order.issue_state.code, "not_required")
		self.assertFalse(work_order.can_dispatch)
		self.assertNotEqual(work_order.flow_status, "issued_waiting_dispatch")

	def test_skip_transfer_from_wip_uses_wip_stock_for_dispatch(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		graph = self._graph(
			plan_name="PP-SKIP-WIP",
			planned_date="2026-08-10",
			creation="2026-08-01 08:00:00",
			work_orders=[{
				"name": "WO-SKIP-WIP", "production_item": "FG-SKIP",
				"production_plan_item": "PPI-SKIP", "status": "Not Started",
				"skip_transfer": 1, "from_wip_warehouse": 1,
				"wip_warehouse": "WIP - TC", "qty": 5,
				"operation_count": 1, "job_card_count": 1,
			}],
			required_items=[{
				"parent": "WO-SKIP-WIP", "item_code": "RM-SKIP",
				"source_warehouse": "Stores - TC", "required_qty": 10,
				"consumed_qty": 0,
			}],
			active_bom_items={"FG-SKIP"},
		)
		stock = {
			("RM-SKIP", "Stores - TC"): {"actual_qty": 10, "available_qty": 10},
			("RM-SKIP", "WIP - TC"): {"actual_qty": 0, "available_qty": 0},
		}
		blocked = allocate_work_order_readiness([graph], stock)[0].work_orders_by_name[
			"WO-SKIP-WIP"
		]

		self.assertEqual(blocked.required_items[0].source_warehouse, "Stores - TC")
		self.assertEqual(blocked.required_items[0].issue_warehouse, "WIP - TC")
		self.assertEqual(blocked.required_items[0].available_qty, 0)
		self.assertFalse(blocked.can_dispatch)

		stock[("RM-SKIP", "Stores - TC")] = {"actual_qty": 0, "available_qty": 0}
		stock[("RM-SKIP", "WIP - TC")] = {"actual_qty": 10, "available_qty": 10}
		ready = allocate_work_order_readiness([graph], stock)[0].work_orders_by_name[
			"WO-SKIP-WIP"
		]
		self.assertEqual(ready.required_items[0].available_qty, 10)
		self.assertTrue(ready.can_dispatch)

	def test_skip_transfer_ready_stock_can_dispatch_without_claiming_it_was_issued(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		graph = self._graph(
			plan_name="PP-SKIP-READY",
			planned_date="2026-08-10",
			creation="2026-08-01 08:00:00",
			work_orders=[{
				"name": "WO-SKIP-READY", "production_item": "FG-SKIP",
				"production_plan_item": "PPI-SKIP", "status": "Not Started",
				"skip_transfer": 1, "qty": 5, "operation_count": 1,
				"job_card_count": 1, "completed_job_card_count": 0,
			}],
			required_items=[{
				"parent": "WO-SKIP-READY", "item_code": "RM-SKIP",
				"source_warehouse": "Stores - TC", "required_qty": 10,
				"consumed_qty": 0,
			}],
			active_bom_items={"FG-SKIP"},
		)

		work_order = allocate_work_order_readiness(
			[graph], {("RM-SKIP", "Stores - TC"): {"actual_qty": 10, "available_qty": 10}},
		)[0].work_orders_by_name["WO-SKIP-READY"]

		self.assertEqual(work_order.readiness_status, "ready_now")
		self.assertEqual(work_order.issue_state.code, "not_required")
		self.assertTrue(work_order.can_dispatch)
		self.assertEqual(work_order.flow_status, "materials_ready_waiting_dispatch")

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

	def test_returned_material_is_removed_from_net_issue_and_must_be_issued_again(self):
		from process_simplification.api.production_readiness import allocate_work_order_readiness

		graph = self._graph(
			plan_name="PP-RETURN",
			planned_date="2026-08-20",
			creation="2026-08-01 09:00:00",
			work_orders=[{
				"name": "WO-RETURN", "production_item": "FG", "production_plan_item": "PPI-1",
				"status": "Not Started", "qty": 5, "produced_qty": 0,
				"material_transferred_for_manufacturing": 5,
				"job_card_count": 1, "completed_job_card_count": 0,
			}],
			required_items=[{
				"parent": "WO-RETURN", "item_code": "RM", "source_warehouse": "Stores - TC",
				"required_qty": 10, "transferred_qty": 10, "returned_qty": 4,
			}],
			active_bom_items={"FG"},
		)

		work_order = allocate_work_order_readiness(
			[graph], {("RM", "Stores - TC"): {"actual_qty": 4, "available_qty": 4}},
		)[0].work_orders_by_name["WO-RETURN"]
		item = work_order.required_items[0]

		self.assertEqual(item.required_qty, 4)
		self.assertEqual(item.net_transferred_qty, 6)
		self.assertEqual(work_order.readiness_status, "ready_now")
		self.assertEqual(work_order.issue_state.code, "ready")
		self.assertAlmostEqual(work_order.issue_state.net_transfer_coverage_fraction, 0.6)
		self.assertAlmostEqual(work_order.issue_state.additional_issueable_qty, 2)
		self.assertTrue(work_order.can_request_issue)
		self.assertFalse(work_order.can_dispatch)


class TestProductionReadinessLoading(UnitTestCase):
	def test_loads_plan_work_orders_and_current_stock_as_one_order_item_snapshot(self):
		from process_simplification.api import production_readiness

		work_order_field_queries = []
		work_order_filter_queries = []
		execution_fact_queries = []
		reservation_queries = []

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
				frappe._dict(name="WOI-FG-SA", parent="WO-FG", item_code="SA", item_name="半成品", stock_uom="Nos", source_warehouse="Stores - TC", required_qty=5, transferred_qty=0, consumed_qty=0),
				frappe._dict(name="WOI-SA-RM", parent="WO-SA", item_code="RM", item_name="原料", stock_uom="Nos", source_warehouse="Stores - TC", required_qty=10, transferred_qty=0, consumed_qty=0, stock_reserved_qty=10),
			],
			"Stock Reservation Entry": [
				frappe._dict(
					voucher_no="WO-SA", voucher_detail_no="WOI-SA-RM",
					item_code="RM", warehouse="Stores - TC", reserved_qty=4,
					delivered_qty=1, transferred_qty=1, consumed_qty=0,
				),
				frappe._dict(
					voucher_no="WO-SA", voucher_detail_no="WOI-SA-RM",
					item_code="RM", warehouse="Work In Progress - TC", reserved_qty=8,
					delivered_qty=0, transferred_qty=0, consumed_qty=0,
				),
			],
			"Job Card": [
				frappe._dict(
					name="JC-SA", work_order="WO-SA", status="Completed", docstatus=1,
					creation="2026-08-20 10:00:00",
				),
			],
			"Work Order Operation": [
				frappe._dict(name="WOO-SA", parent="WO-SA", idx=1),
			],
			"Stock Entry": [
				frappe._dict(
					name="STE-SA-ISSUE", work_order="WO-SA",
					purpose="Material Transfer for Manufacture", docstatus=0,
					creation="2026-08-20 11:00:00",
				),
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
			if doctype == "Stock Reservation Entry":
				reservation_queries.append(kwargs)
			if doctype in {"Job Card", "Work Order Operation", "Stock Entry"}:
				execution_fact_queries.append((doctype, kwargs.get("filters") or {}))
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
			patch.object(
				production_readiness,
				"load_work_order_stock_facts",
				return_value={
					("WO-SA", "RM", "Stores - TC"): frappe._dict(
						gross_issued_qty=4,
						returned_qty=0,
						net_issued_qty=4,
					)
				},
			) as load_stock_facts,
			patch.object(production_readiness, "_load_active_replenishment_work_orders", return_value=[]),
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
		self.assertEqual(plans[0]["work_orders"][0]["operation_state"]["code"], "completed")
		self.assertEqual(plans[0]["work_orders"][0]["issue_state"]["code"], "draft_pending")
		self.assertEqual(plans[0]["work_orders"][0]["draft_issue_stock_entry"], "STE-SA-ISSUE")
		self.assertEqual(plans[0]["work_orders"][0]["required_items"][0]["required_qty"], 6)
		self.assertEqual(
			plans[0]["work_orders"][0]["required_items"][0]["net_transferred_qty"],
			4,
		)
		self.assertEqual(plans[0]["work_orders"][0]["required_items"][0]["committed_qty"], 2)
		self.assertEqual(
			plans[0]["work_orders"][0]["required_items"][0]["source_reserved_qty"],
			2,
		)
		self.assertEqual(plans[0]["work_orders"][1]["readiness_status"], "waiting_subassembly")
		self.assertEqual(plans[0]["summary"]["ready_work_order_count"], 1)
		self.assertEqual(len(work_order_field_queries), 1)
		self.assertTrue(all("bom_no" in fields for fields in work_order_field_queries))
		self.assertTrue(all("skip_transfer" in fields for fields in work_order_field_queries))
		self.assertTrue(all("from_wip_warehouse" in fields for fields in work_order_field_queries))
		self.assertTrue(all("sales_order_item" not in filters for filters in work_order_filter_queries))
		self.assertEqual(len(reservation_queries), 1)
		load_stock_facts.assert_called_once_with(["WO-FG", "WO-SA"])
		self.assertEqual(
			reservation_queries[0]["filters"],
			{
				"docstatus": 1,
				"voucher_type": "Work Order",
				"voucher_no": ["in", ["WO-FG", "WO-SA"]],
			},
		)
		self.assertEqual(reservation_queries[0]["limit"], 0)
		self.assertTrue(
			{
				"voucher_detail_no", "item_code", "warehouse", "reserved_qty",
				"delivered_qty", "transferred_qty", "consumed_qty",
			}.issubset(reservation_queries[0]["fields"])
		)
		self.assertEqual(
			[doctype for doctype, _filters in execution_fact_queries],
			["Job Card", "Work Order Operation", "Stock Entry"],
		)
		for doctype, filters in execution_fact_queries:
			link_field = "parent" if doctype == "Work Order Operation" else "work_order"
			self.assertEqual(filters[link_field], ["in", ["WO-FG", "WO-SA"]])

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
			patch.object(production_readiness, "load_work_order_stock_facts", return_value={}),
			patch.object(production_readiness, "_load_active_replenishment_work_orders", return_value=[]),
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
			patch.object(production_readiness, "load_work_order_stock_facts", return_value={}),
			patch.object(production_readiness, "_load_active_replenishment_work_orders", return_value=[]),
			patch("process_simplification.api.shortage.get_material_stock_snapshot", return_value=frappe._dict(actual_qty=0, available_qty=0)),
			patch("process_simplification.api.shortage._mr_documents", return_value=[]),
			patch("process_simplification.api.shortage._po_documents", return_value=[]),
		):
			result = production_readiness.get_production_plan_readiness(company="_Test Company")

		self.assertEqual(set(result), {"SOI-ALLOWED"})
		self.assertEqual(permission_queries, ["Work Order", "Production Plan", "Sales Order"])
