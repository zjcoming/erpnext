from copy import deepcopy
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from process_simplification.api.production_readiness import (
	_attach_replenishment_progress,
	_serialize_readiness_plan,
	allocate_work_order_readiness,
	build_work_order_graph,
	work_order_priority_key,
)
from process_simplification.production_workflow.replenishment import (
	attach_replenishment_context,
	resolve_internal_receipt_target,
)


def chain_graphs():
	def work_order(name, item, plan, **values):
		return frappe._dict(
			name=name,
			production_item=item,
			production_plan=plan,
			company="Factory",
			fg_warehouse="Stores",
			source_warehouse="Stores",
			qty=1,
			produced_qty=0,
			status="Stock Reserved",
			**values,
		)

	def material(parent, item, **values):
		return frappe._dict(
			name=f"{parent}-ITEM",
			parent=parent,
			item_code=item,
			source_warehouse="Stores",
			required_qty=1,
			include_item_in_manufacturing=1,
			**values,
		)

	original = build_work_order_graph(
		{"name": "PP-ORIGINAL"},
		[
			work_order(
				"TARGET",
				"FG",
				"PP-ORIGINAL",
				sales_order="SO-1",
				sales_order_item="SOI-1",
				order_delivery_date="2026-09-02",
				order_creation="2026-08-01",
				sales_order_item_idx=1,
			)
		],
		[material("TARGET", "SEMI")],
		[],
		{"SEMI"},
	)
	orders = [
		work_order(
			"ROOT",
			"SEMI",
			"PP-SUPPLEMENT",
			production_plan_item="PPI",
			custom_replenishes_work_order="TARGET",
			custom_replenishes_work_order_item="TARGET-ITEM",
		),
		work_order("MIDDLE", "COIL", "PP-SUPPLEMENT", production_plan_sub_assembly_item="SUB-1"),
		work_order("LEAF", "FRAME", "PP-SUPPLEMENT", production_plan_sub_assembly_item="SUB-2"),
	]
	items = [material("ROOT", "COIL"), material("MIDDLE", "FRAME"), material("LEAF", "RESIN")]
	subassemblies = [
		{"name": "SUB-1", "production_plan_item": "PPI", "parent_item_code": "SEMI", "bom_level": 0},
		{"name": "SUB-2", "production_plan_item": "PPI", "parent_item_code": "COIL", "bom_level": 1},
	]
	supplement = build_work_order_graph(
		{"name": "PP-SUPPLEMENT"},
		orders,
		items,
		subassemblies,
		{"SEMI", "COIL", "FRAME"},
	)
	return [original, supplement]


class TestReplenishmentChain(UnitTestCase):
	def test_existing_plan_branch_inherits_order_context_without_changing_sales_links(self):
		graphs = chain_graphs()
		by_name = attach_replenishment_context(graphs)
		for name, target in [("ROOT", "TARGET"), ("MIDDLE", "ROOT"), ("LEAF", "MIDDLE")]:
			row = by_name[name]
			self.assertEqual(row.context_sales_order_item, "SOI-1")
			self.assertEqual(row.supply_target_work_order, target)
			self.assertEqual(row.supply_target_work_order_item, target + "-ITEM")
			self.assertFalse(row.sales_order)
			self.assertFalse(row.sales_order_item)
			self.assertEqual(
				work_order_priority_key(graphs[1], row), work_order_priority_key(graphs[0], by_name["TARGET"])
			)
		projection = _serialize_readiness_plan(graphs[1], "SOI-1")
		self.assertEqual([row.name for row in projection["work_orders"]], ["LEAF", "MIDDLE", "ROOT"])
		self.assertTrue(projection["is_replenishment"])
		self.assertEqual(_serialize_readiness_plan(graphs[1], "SOI-OTHER")["work_orders"], [])

	def test_missing_or_incompatible_target_does_not_guess_order_context(self):
		for change in ("missing", "warehouse", "company", "detail", "cycle"):
			with self.subTest(change=change):
				graphs = chain_graphs()
				root = graphs[1].work_orders_by_name["ROOT"]
				if change == "missing":
					graphs = graphs[1:]
				elif change == "warehouse":
					root.fg_warehouse = "Different Stores"
				elif change == "company":
					root.company = "Other Factory"
				elif change == "detail":
					root.custom_replenishes_work_order_item = "OTHER-ITEM"
				else:
					graphs[0].work_orders_by_name["TARGET"].update(
						custom_replenishes_work_order="ROOT", custom_replenishes_work_order_item="ROOT-ITEM"
					)
				by_name = attach_replenishment_context(graphs)
				self.assertFalse(by_name["ROOT"].get("context_sales_order_item"))
				self.assertFalse(by_name["LEAF"].get("context_sales_order_item"))

	def test_ambiguous_parent_is_not_attached_to_an_arbitrary_supplement(self):
		graphs = chain_graphs()
		leaf = graphs[1].work_orders_by_name["LEAF"]
		leaf.parent_work_order = None
		leaf.graph_link_ambiguous = True
		attach_replenishment_context(graphs)
		self.assertFalse(leaf.get("is_replenishment"))

	def test_supplement_of_a_supplement_resolves_to_original_order(self):
		graphs = chain_graphs()
		other = deepcopy(graphs[1])
		other.name = "PP-NESTED"
		root = other.work_orders_by_name["ROOT"]
		root.update(
			name="NESTED",
			production_plan="PP-NESTED",
			production_item="FRAME",
			custom_replenishes_work_order="MIDDLE",
			custom_replenishes_work_order_item="MIDDLE-ITEM",
		)
		other.work_orders_by_name = {"NESTED": root}
		other.execution_order = ["NESTED"]
		by_name = attach_replenishment_context([*graphs, other])
		self.assertEqual(by_name["NESTED"].context_sales_order_item, "SOI-1")

	def test_stock_is_allocated_once_and_stock_reserved_is_not_production_progress(self):
		graphs = chain_graphs()
		later = deepcopy(graphs[1])
		later.name = "PP-LATER"
		later.work_orders_by_name = {
			"COMPETITOR": frappe._dict(
				name="COMPETITOR",
				production_item="OTHER",
				qty=1,
				status="Not Started",
				sales_order="SO-2",
				sales_order_item="SOI-2",
				order_delivery_date="2026-09-03",
				bom_level=0,
				child_work_orders=[],
				required_items=[frappe._dict(item_code="RESIN", source_warehouse="Stores", required_qty=1)],
			)
		}
		later.execution_order = ["COMPETITOR"]
		graphs.append(later)
		attach_replenishment_context(graphs)
		allocated = allocate_work_order_readiness(
			graphs, {("RESIN", "Stores"): {"actual_qty": 1, "available_qty": 1}}
		)
		leaf = allocated[1].work_orders_by_name["LEAF"]
		competitor = allocated[2].work_orders_by_name["COMPETITOR"]
		self.assertEqual(leaf.required_items[0].available_qty, 1)
		self.assertEqual(competitor.required_items[0].available_qty, 0)
		self.assertEqual(leaf.readiness_status, "ready_now")
		self.assertEqual(allocated[1].work_orders_by_name["MIDDLE"].readiness_status, "waiting_subassembly")

	def test_internal_receipt_uses_exact_parent_without_sales_link_or_direct_root_marker(self):
		graphs = chain_graphs()
		graph = graphs[1]
		rows = list(graph.work_orders_by_name.values())
		items = [item for row in rows for item in row.required_items]
		subs = [
			frappe._dict(name="SUB-1", production_plan_item="PPI", parent_item_code="SEMI", bom_level=0),
			frappe._dict(name="SUB-2", production_plan_item="PPI", parent_item_code="COIL", bom_level=1),
		]
		supply = frappe._dict(
			graph.work_orders_by_name["LEAF"],
			custom_replenishes_work_order=None,
			custom_replenishes_work_order_item=None,
		)
		with patch(
			"process_simplification.production_workflow.replenishment.frappe.get_all",
			side_effect=[rows, items, subs],
		):
			target = resolve_internal_receipt_target(supply)
		self.assertEqual(target.custom_replenishes_work_order, "MIDDLE")
		self.assertEqual(target.custom_replenishes_work_order_item, "MIDDLE-ITEM")
		self.assertIsNone(supply.custom_replenishes_work_order)

	def test_ordinary_finished_good_receipt_keeps_sales_handoff(self):
		with patch(
			"process_simplification.production_workflow.replenishment.frappe.get_all",
			side_effect=[[frappe._dict(name="ORDINARY")], [], []],
		):
			self.assertIsNone(
				resolve_internal_receipt_target(frappe._dict(name="ORDINARY", production_plan="PP"))
			)

	def test_associated_supplements_do_not_also_appear_in_other_production(self):
		from process_simplification.api.production import _other_work_orders

		with patch(
			"process_simplification.api.production.frappe.get_list",
			return_value=[
				frappe._dict(name="ROOT"),
				frappe._dict(name="LEAF"),
				frappe._dict(name="OTHER"),
			],
		):
			self.assertEqual([row["name"] for row in _other_work_orders(["ROOT", "LEAF"])], ["OTHER"])

	def test_receipt_reservation_and_feed_are_separate_and_cancelled_receipts_do_not_close_task(self):
		graphs = chain_graphs()
		by_name = attach_replenishment_context(graphs)
		root = by_name["ROOT"]
		root.update(produced_qty=1, status="Completed")
		entry = frappe._dict(
			from_voucher_type="Stock Entry",
			from_voucher_no="RECEIPT",
			voucher_no="TARGET",
			voucher_detail_no="TARGET-ITEM",
			warehouse="Stores",
			reserved_qty=1,
			transferred_qty=0,
			consumed_qty=0,
			delivered_qty=0,
		)
		with patch(
			"process_simplification.api.production_readiness.frappe.get_all",
			return_value=[frappe._dict(name="RECEIPT", work_order="ROOT")],
		):
			_attach_replenishment_progress(graphs, [entry])
			self.assertEqual(root.replenishment_progress.code, "ready_to_feed")
			self.assertEqual(root.replenishment_progress.fed_qty, 0)
			entry.transferred_qty = 1
			by_name["TARGET"].status = "Completed"
			_attach_replenishment_progress(graphs, [entry])
			self.assertEqual(root.replenishment_progress.code, "fed")
			self.assertEqual(root.replenishment_progress.reserved_qty, 0)
			by_name["TARGET"].status = "Not Started"
			root.update(produced_qty=0, status="Not Started")
		with patch("process_simplification.api.production_readiness.frappe.get_all", return_value=[]):
			_attach_replenishment_progress(graphs, [])
			self.assertEqual(root.replenishment_progress.code, "in_progress")

	def test_unreserved_receipt_and_target_filled_are_not_claimed_as_feed(self):
		graphs = chain_graphs()
		by_name = attach_replenishment_context(graphs)
		root = by_name["ROOT"]
		root.update(produced_qty=1, status="Completed")
		with patch("process_simplification.api.production_readiness.frappe.get_all", return_value=[]):
			_attach_replenishment_progress(graphs, [])
			self.assertEqual(root.replenishment_progress.code, "unreserved_output")
			by_name["TARGET"].required_items[0].required_qty = 0
			_attach_replenishment_progress(graphs, [])
			self.assertEqual(root.replenishment_progress.code, "target_covered")
