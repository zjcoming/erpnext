from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from process_simplification.api.production_readiness import build_work_order_graph
from process_simplification.production_workflow.planning_stock import net_subassemblies


class TestPlanningStock(UnitTestCase):
	def _net(self, rows, available):
		plan = SimpleNamespace(
			po_items=[frappe._dict(name="ROOT", item_code="FG", planned_qty=10)],
			sub_assembly_items=[
				frappe._dict(
					name=f"ROW-{idx}", idx=idx, production_plan_item="ROOT", fg_warehouse="Stores", **row
				)
				for idx, row in enumerate(rows, 1)
			],
			get_sub_assembly_items=lambda: None,
		)
		with patch(
			"process_simplification.production_workflow.planning_stock.locked_available_qty",
			side_effect=lambda item, warehouse: available.get(item, 0),
		):
			commitments = net_subassemblies(plan)
		return plan.sub_assembly_items, commitments

	def test_repeated_bom_branches_share_each_physical_pool_once(self):
		rows, commitments = self._net(
			[
				{"production_item": "A", "bom_level": 0, "required_qty": 10},
				{"production_item": "B", "bom_level": 1, "required_qty": 20},
				{"production_item": "A", "bom_level": 0, "required_qty": 10},
				{"production_item": "B", "bom_level": 1, "required_qty": 30},
			],
			{"A": 6, "B": 20},
		)
		self.assertEqual([(r.required_qty, r.qty) for r in rows], [(10, 4), (8, 0), (10, 10), (30, 18)])
		self.assertEqual(
			[(r.production_item, p.name if p else None, qty) for r, p, qty in commitments],
			[("A", None, 6), ("B", "ROW-1", 8), ("B", "ROW-3", 12)],
		)

	def test_phantom_levels_link_to_real_consuming_parent_and_scale_descendants(self):
		rows, _ = self._net(
			[
				{"production_item": "A", "bom_level": 1, "required_qty": 20},
				{"production_item": "B", "bom_level": 3, "required_qty": 60},
			],
			{"A": 10},
		)
		self.assertEqual(
			[(r.parent_item_code, r.bom_level, r.required_qty, r.qty) for r in rows],
			[("FG", 0, 20, 10), ("A", 1, 30, 30)],
		)

	def test_two_occurrences_of_same_parent_keep_separate_receipt_targets(self):
		rows, _ = self._net(
			[
				{"production_item": "A", "bom_level": 0, "required_qty": 10},
				{"production_item": "B", "bom_level": 1, "required_qty": 10},
				{"production_item": "A", "bom_level": 0, "required_qty": 10},
				{"production_item": "B", "bom_level": 1, "required_qty": 10},
			],
			{},
		)
		orders = [frappe._dict(name="FG-WO", production_item="FG", production_plan_item="ROOT")]
		orders += [
			frappe._dict(
				name=r.name + "-WO",
				production_item=r.production_item,
				production_plan_sub_assembly_item=r.name,
			)
			for r in rows
		]
		items = [frappe._dict(parent="FG-WO", item_code="A")]
		items += [frappe._dict(parent=f"ROW-{i}-WO", item_code="B") for i in [1, 3]]
		graph = build_work_order_graph({"name": "PLAN"}, orders, items, rows)
		self.assertEqual(graph.work_orders_by_name["ROW-2-WO"].parent_work_order, "ROW-1-WO")
		self.assertEqual(graph.work_orders_by_name["ROW-4-WO"].parent_work_order, "ROW-3-WO")

	def test_direct_consumption_gap_uses_effective_wip_and_real_consumption(self):
		from process_simplification.production_workflow.service import _target_group_gap

		items = [
			frappe._dict(
				name="MATERIAL", parent="WO", item_code="SEMI", source_warehouse="Stores", required_qty=10
			)
		]
		work_order = frappe._dict(name="WO", skip_transfer=1, from_wip_warehouse=1, wip_warehouse="WIP")
		facts = {("WO", "SEMI", "WIP"): frappe._dict(consumed_qty=3, net_issued_qty=0)}
		with patch(
			"process_simplification.production_workflow.service._active_work_order_item_reserved_qty",
			return_value=5,
		) as reserved:
			self.assertEqual(_target_group_gap(items, facts, work_order=work_order), (2, 10))
		reserved.assert_called_once_with(items[0], warehouse="WIP")
