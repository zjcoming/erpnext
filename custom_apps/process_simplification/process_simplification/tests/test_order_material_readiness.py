"""Tests for per-Sales-Order-Item material readiness.

Coverage is computed per order line (Sales Order Item), not as one shared
material pool. Warehouse stock is allocated across order lines in delivery-date
priority; in-transit purchases count toward an order line only when attributed
to that Sales Order Item. An order line is READY when every material's
allocated stock alone meets the requirement.
"""

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase


class TestOrderMaterialReadiness(UnitTestCase):
	def _demand(self, soi, delivery_date, qty=1, bom="BOM-FG"):
		return {
			"bom_no": bom,
			"qty": qty,
			"source": {
				"demand_key": soi,
				"sales_order": "SO-" + soi,
				"sales_order_item": soi,
				"finished_item": "FG",
				"delivery_date": delivery_date,
				"sales_order_item_warehouse": "仓库 - 恒",
			},
		}

	def _bom_one_material(self, per_unit=10):
		return lambda bom_no, company, qty, fetch_exploded: {
			"RM": frappe._dict(
				{"item_code": "RM", "item_name": "原料", "stock_uom": "Nos",
				 "source_warehouse": "仓库 - 恒", "qty": per_unit * qty}
			)
		}

	def test_earlier_order_gets_stock_first_and_is_ready(self):
		from process_simplification.api import shortage

		# Two orders each need 10 of RM; only 10 in stock. Earliest delivery wins.
		demands = [
			self._demand("SOI-LATE", "2026-09-10"),
			self._demand("SOI-EARLY", "2026-09-01"),
		]
		with (
			patch("process_simplification.api.shortage.resolve_production_source_warehouse",
				return_value=frappe._dict({"warehouse": "仓库 - 恒", "can_use": True, "reason": None})),
			patch("process_simplification.api.shortage.get_bom_items_as_dict", side_effect=self._bom_one_material(10)),
			patch("process_simplification.api.shortage.get_material_stock_snapshot",
				return_value=frappe._dict({"can_calculate": True, "actual_qty": 10, "committed_qty": 0, "available_qty": 10})),
			patch("process_simplification.api.shortage._intransit_purchase_for_soi", return_value=0),
		):
			result = shortage.calculate_material_coverage(demands, "恒算科技")

		by_soi = {}
		for m in result.materials:
			by_soi.setdefault(m["sales_order_item"], []).append(m)

		early = by_soi["SOI-EARLY"][0]
		late = by_soi["SOI-LATE"][0]
		# Earliest order is fully covered by stock -> ready.
		self.assertEqual(early["allocated_qty"], 10)
		self.assertEqual(early["status"], "ready_now")
		# Later order gets nothing, no attributed purchase -> needs purchase.
		self.assertEqual(late["allocated_qty"], 0)
		self.assertEqual(late["shortage_qty"], 10)
		self.assertEqual(late["status"], "new_purchase_required")

	def test_intransit_attributed_to_soi_marks_awaiting_not_ready(self):
		from process_simplification.api import shortage

		demands = [self._demand("SOI-1", "2026-09-01")]

		def intransit(item, warehouse, company, sales_order_item, need_by_date):
			return 10 if sales_order_item == "SOI-1" else 0

		with (
			patch("process_simplification.api.shortage.resolve_production_source_warehouse",
				return_value=frappe._dict({"warehouse": "仓库 - 恒", "can_use": True, "reason": None})),
			patch("process_simplification.api.shortage.get_bom_items_as_dict", side_effect=self._bom_one_material(10)),
			patch("process_simplification.api.shortage.get_material_stock_snapshot",
				return_value=frappe._dict({"can_calculate": True, "actual_qty": 0, "committed_qty": 0, "available_qty": 0})),
			patch("process_simplification.api.shortage._intransit_purchase_for_soi", side_effect=intransit),
		):
			result = shortage.calculate_material_coverage(demands, "恒算科技")

		m = result.materials[0]
		# No stock, but the in-transit PO is attributed to this SOI: awaiting, not short.
		self.assertEqual(m["allocated_qty"], 0)
		self.assertEqual(m["intransit_qty"], 10)
		self.assertEqual(m["shortage_qty"], 0)
		self.assertEqual(m["status"], "awaiting_purchase_receipt")

	def test_readiness_requires_all_materials_on_hand(self):
		from process_simplification.api import shortage

		demands = [self._demand("SOI-1", "2026-09-01")]

		def two_materials(bom_no, company, qty, fetch_exploded):
			return {
				"RM-A": frappe._dict({"item_code": "RM-A", "source_warehouse": "仓库 - 恒", "qty": 5 * qty}),
				"RM-B": frappe._dict({"item_code": "RM-B", "source_warehouse": "仓库 - 恒", "qty": 5 * qty}),
			}

		def snapshot(item_code, warehouse):
			# RM-A fully in stock, RM-B empty.
			qty = 5 if item_code == "RM-A" else 0
			return frappe._dict({"can_calculate": True, "actual_qty": qty, "committed_qty": 0, "available_qty": qty})

		with (
			patch("process_simplification.api.shortage.resolve_production_source_warehouse",
				return_value=frappe._dict({"warehouse": "仓库 - 恒", "can_use": True, "reason": None})),
			patch("process_simplification.api.shortage.get_bom_items_as_dict", side_effect=two_materials),
			patch("process_simplification.api.shortage.get_material_stock_snapshot", side_effect=snapshot),
			patch("process_simplification.api.shortage._intransit_purchase_for_soi", return_value=0),
		):
			result = shortage.calculate_material_coverage(demands, "恒算科技")
			ready = shortage.is_order_item_ready(result, "SOI-1")

		# RM-B is short, so the order line is NOT ready even though RM-A is on hand.
		self.assertFalse(ready)
