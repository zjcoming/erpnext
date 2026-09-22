from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from process_simplification.api.production_readiness import build_work_order_graph
from process_simplification.production_workflow import service


class TestSemiFinishedBranchIdentity(UnitTestCase):
	def setUp(self):
		super().setUp()
		self.rows = [
			frappe._dict(name="A", idx=1, production_plan_item="ROOT", production_item="UPPER",
				parent_item_code="FG", bom_level=0, bom_no="BOM-UPPER-A"),
			frappe._dict(name="AX", idx=2, production_plan_item="ROOT", production_item="X",
				parent_item_code="UPPER", bom_level=1, bom_no="BOM-X-A", qty=0),
			frappe._dict(name="B", idx=3, production_plan_item="ROOT", production_item="UPPER",
				parent_item_code="FG", bom_level=0, bom_no="BOM-UPPER-B"),
			frappe._dict(name="BX", idx=4, production_plan_item="ROOT", production_item="X",
				parent_item_code="UPPER", bom_level=1, bom_no="BOM-X-B", qty=10),
		]

	def _order(self, name, subrow=None, *, snapshot="Semi"):
		return frappe._dict(name=name, production_plan="PLAN", production_plan_item=None if subrow else "ROOT",
			production_plan_sub_assembly_item=subrow, production_item="UPPER" if subrow else "FG",
			custom_semi_finished_warehouse=snapshot)

	def test_new_parent_classifies_only_its_persisted_direct_children(self):
		orders = [self._order("TOP"), self._order("A-WO", "A"), self._order("B-WO", "B")]
		items = [
			frappe._dict(name="TOP-X", parent="TOP", item_code="X"),
			frappe._dict(name="TOP-UPPER", parent="TOP", item_code="UPPER"),
			frappe._dict(name="A-X", parent="A-WO", item_code="X"),
			frappe._dict(name="B-X", parent="B-WO", item_code="X"),
		]
		graph = build_work_order_graph({"name": "PLAN"}, orders, items, self.rows,
			active_bom_items={"X", "UPPER"})
		flags = {item.name: item.is_manufactured for wo in graph.work_orders_by_name.values() for item in wo.required_items}
		self.assertEqual(flags, {"TOP-X": False, "TOP-UPPER": True, "A-X": True, "B-X": True})

	def test_zero_qty_subassembly_stays_manufactured_even_if_no_longer_default_bom(self):
		graph = build_work_order_graph({"name": "PLAN"}, [self._order("A-WO", "A")],
			[frappe._dict(name="A-X", parent="A-WO", item_code="X")], self.rows, active_bom_items=set())
		self.assertTrue(graph.work_orders_by_name["A-WO"].required_items[0].is_manufactured)

	def test_legacy_order_retains_global_bom_fallback(self):
		graph = build_work_order_graph({"name": "PLAN"}, [self._order("OLD", snapshot=None)],
			[frappe._dict(name="OLD-X", parent="OLD", item_code="X")], [], active_bom_items={"X"})
		self.assertTrue(graph.work_orders_by_name["OLD"].required_items[0].is_manufactured)

	def test_replenishment_uses_exact_branch_bom_instead_of_latest_same_item(self):
		with patch.object(service.frappe, "get_all", return_value=self.rows), patch.object(
			service.frappe.db, "get_value", side_effect=AssertionError("must not choose latest WO BOM"),
		):
			self.assertEqual(service._replenishment_bom(self._order("A-WO", "A"), "X"), "BOM-X-A")
			self.assertEqual(service._replenishment_bom(self._order("B-WO", "B"), "X"), "BOM-X-B")

	def test_new_purchased_occurrence_cannot_turn_into_replenishment_from_item_default(self):
		with patch.object(service.frappe, "get_all", return_value=self.rows), patch.object(
			service, "get_default_bom", side_effect=AssertionError("must not change purchased occurrence"),
		):
			self.assertIsNone(service._replenishment_bom(self._order("TOP"), "X"))

	def test_ambiguous_boms_in_one_branch_are_rejected(self):
		self.rows.insert(2, frappe._dict(name="AX2", idx=3, production_plan_item="ROOT", production_item="X",
			parent_item_code="UPPER", bom_level=1, bom_no="BOM-X-OTHER"))
		for idx, row in enumerate(self.rows, 1):
			row.idx = idx
		with patch.object(service.frappe, "get_all", return_value=self.rows):
			with self.assertRaises(frappe.ValidationError):
				service._replenishment_bom(self._order("A-WO", "A"), "X")

	def test_old_replenishment_keeps_preexisting_bom_resolution(self):
		with patch.object(service.frappe.db, "get_value", return_value="OLD-BOM"), patch.object(
			service.frappe, "get_all", side_effect=AssertionError("legacy route unchanged"),
		):
			self.assertEqual(service._replenishment_bom(self._order("OLD", snapshot=None), "X"), "OLD-BOM")
