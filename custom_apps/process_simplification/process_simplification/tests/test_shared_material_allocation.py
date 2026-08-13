"""Boundary tests for shared scarce raw-material allocation.

When several orders draw on the same raw material and stock is not enough for
all of them, a per-order (partial) coverage check must not let every order
believe the same free stock covers it. The full production overview already
consolidates demands, but the quick-order preflight and ``check_shortage``
only pass the currently selected demands, so shared stock has to be reduced by
the other in-flight demands first.
"""

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase


class TestSharedMaterialAllocation(UnitTestCase):
	def _bom_items(self, per_unit_qty):
		return {
			"RM-SHARED": frappe._dict(
				{
					"item_code": "RM-SHARED",
					"item_name": "共享原料",
					"stock_uom": "Nos",
					"source_warehouse": "_Test Warehouse - _TC",
					"qty": per_unit_qty,
				}
			)
		}

	def setUp(self):
		# Isolate the allocation math from warehouse master-data fixtures: the
		# resolved source warehouse is always usable in these tests.
		patcher = patch(
			"process_simplification.api.shortage.resolve_production_source_warehouse",
			return_value=frappe._dict(
				{"warehouse": "_Test Warehouse - _TC", "can_use": True, "reason": None}
			),
		)
		patcher.start()
		self.addCleanup(patcher.stop)

	@patch("process_simplification.api.shortage._intransit_purchase_for_soi", return_value=0)
	@patch("process_simplification.api.shortage.get_material_stock_snapshot")
	@patch("process_simplification.api.shortage.get_bom_items_as_dict")
	def test_prior_demands_consume_shared_stock_before_reported_demand(
		self, get_bom_items, stock_snapshot, intransit
	):
		from process_simplification.api.shortage import calculate_material_coverage

		# Each finished unit needs 10 of RM-SHARED; only 10 in stock.
		get_bom_items.return_value = self._bom_items(per_unit_qty=10)
		stock_snapshot.return_value = frappe._dict(
			{"can_calculate": True, "actual_qty": 10, "committed_qty": 0, "available_qty": 10}
		)

		# One other in-flight order already needs the whole 10, so the reported
		# order must get zero allocated stock and a full shortage of 10.
		result = calculate_material_coverage(
			[{"bom_no": "BOM-FG-001", "qty": 1, "source": {"sales_order_item": "SOI-1", "finished_item": "FG-001"}}],
			"_Test Company",
			need_by_date="2099-01-10",
			defaults=frappe._dict({"source_warehouse": "_Test Warehouse - _TC"}),
			prior_demands=[{"bom_no": "BOM-FG-001", "qty": 1}],
		)

		material = result.materials[0]
		self.assertEqual(material["item_code"], "RM-SHARED")
		self.assertEqual(material["allocated_qty"], 0)
		self.assertEqual(material["current_gap_qty"], 10)
		self.assertEqual(material["shortage_qty"], 10)
		self.assertEqual(material["status"], "new_purchase_required")

	@patch("process_simplification.api.shortage._intransit_purchase_for_soi", return_value=0)
	@patch("process_simplification.api.shortage.get_material_stock_snapshot")
	@patch("process_simplification.api.shortage.get_bom_items_as_dict")
	def test_without_prior_demands_behaviour_is_unchanged(
		self, get_bom_items, stock_snapshot, intransit
	):
		from process_simplification.api.shortage import calculate_material_coverage

		get_bom_items.return_value = self._bom_items(per_unit_qty=10)
		stock_snapshot.return_value = frappe._dict(
			{"can_calculate": True, "actual_qty": 10, "committed_qty": 0, "available_qty": 10}
		)

		result = calculate_material_coverage(
			[{"bom_no": "BOM-FG-001", "qty": 1, "source": {"sales_order_item": "SOI-1", "finished_item": "FG-001"}}],
			"_Test Company",
			need_by_date="2099-01-10",
			defaults=frappe._dict({"source_warehouse": "_Test Warehouse - _TC"}),
		)

		material = result.materials[0]
		self.assertEqual(material["allocated_qty"], 10)
		self.assertEqual(material["current_gap_qty"], 0)
		self.assertEqual(material["shortage_qty"], 0)
		self.assertEqual(material["status"], "ready_now")

	@patch("process_simplification.api.shortage._intransit_purchase_for_soi", return_value=0)
	@patch("process_simplification.api.shortage.get_material_stock_snapshot")
	@patch("process_simplification.api.shortage.get_bom_items_as_dict")
	def test_prior_demands_only_partially_consume_shared_stock(
		self, get_bom_items, stock_snapshot, intransit
	):
		from process_simplification.api.shortage import calculate_material_coverage

		# 30 in stock; each finished unit needs 10 of RM-SHARED (qty-scaled by
		# the requested finished quantity, like the real BOM explosion).
		get_bom_items.side_effect = lambda bom_no, company, qty, fetch_exploded: {
			"RM-SHARED": frappe._dict(
				{
					"item_code": "RM-SHARED",
					"item_name": "共享原料",
					"stock_uom": "Nos",
					"source_warehouse": "_Test Warehouse - _TC",
					"qty": 10 * qty,
				}
			)
		}
		stock_snapshot.return_value = frappe._dict(
			{"can_calculate": True, "actual_qty": 30, "committed_qty": 0, "available_qty": 30}
		)

		# Prior demand needs 10 (1 unit); reported demand needs 25 (2.5 units).
		result = calculate_material_coverage(
			[{"bom_no": "BOM-FG-001", "qty": 2.5, "source": {"sales_order_item": "SOI-1", "finished_item": "FG-001"}}],
			"_Test Company",
			need_by_date="2099-01-10",
			defaults=frappe._dict({"source_warehouse": "_Test Warehouse - _TC"}),
			prior_demands=[{"bom_no": "BOM-FG-001", "qty": 1}],
		)

		material = result.materials[0]
		# Residual stock after the prior 10 is 20; reported demand needs 25 -> 5 short.
		self.assertEqual(material["required_qty"], 25)
		self.assertEqual(material["allocated_qty"], 20)
		self.assertEqual(material["current_gap_qty"], 5)
		self.assertEqual(material["shortage_qty"], 5)


class TestDeliveryPriorityPriorDemands(UnitTestCase):
	"""口径 2: only demands due strictly earlier (or with no date) consume shared
	stock before the target; equal or later demands do not."""

	def _overview(self, demands):
		return {"demands": demands}

	def _demand(self, item, delivery_date, *, sales_order_item, qty=10):
		return {
			"company": "_Test Company",
			"item_code": item,
			"sales_order": "SO-" + sales_order_item,
			"sales_order_item": sales_order_item,
			"delivery_date": delivery_date,
			"warehouse": "_Test Warehouse - _TC",
			"production_required_qty": qty,
		}

	def test_only_earlier_and_undated_demands_are_prior(self):
		from process_simplification.api import production

		demands = [
			self._demand("EARLY", "2026-08-01", sales_order_item="SOI-EARLY"),
			self._demand("UNDATED", None, sales_order_item="SOI-UNDATED"),
			self._demand("SAME", "2026-08-05", sales_order_item="SOI-SAME"),
			self._demand("LATE", "2026-08-10", sales_order_item="SOI-LATE"),
			self._demand("OTHER-CO", "2026-08-01", sales_order_item="SOI-OTHERCO"),
		]
		demands[4]["company"] = "_Test Company 1"

		with patch.object(
			production, "get_production_overview", return_value=self._overview(demands)
		), patch.object(production, "get_default_bom", side_effect=lambda item: "BOM-" + item):
			prior = production.get_prior_material_demands(
				"_Test Company", target_delivery_date="2026-08-05"
			)

		items = {row["source"]["finished_item"] for row in prior}
		# EARLY (<) and UNDATED (most urgent) qualify; SAME (==) and LATE (>) do
		# not; other-company demand is filtered out.
		self.assertEqual(items, {"EARLY", "UNDATED"})

	def test_excluded_sales_order_item_is_not_prior_to_itself(self):
		from process_simplification.api import production

		demands = [
			self._demand("EARLY", "2026-08-01", sales_order_item="SOI-EARLY"),
			self._demand("SELF", "2026-08-01", sales_order_item="SOI-SELF"),
		]

		with patch.object(
			production, "get_production_overview", return_value=self._overview(demands)
		), patch.object(production, "get_default_bom", side_effect=lambda item: "BOM-" + item):
			prior = production.get_prior_material_demands(
				"_Test Company",
				target_delivery_date="2026-08-05",
				exclude_sales_order_item="SOI-SELF",
			)

		items = {row["source"]["finished_item"] for row in prior}
		self.assertEqual(items, {"EARLY"})
