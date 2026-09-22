from contextlib import ExitStack
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from process_simplification.api import shortage
from process_simplification.production_exceptions import service as exception_service
from process_simplification.production_exceptions.material_routes import aggregate_routes, stock_rows
from process_simplification.production_workflow.replenishment import target_item_for_supply


class TestSemiFinishedCoverage(UnitTestCase):
	def setUp(self):
		super().setUp()
		self.stack = ExitStack()
		self.addCleanup(self.stack.close)
		self.defaults = frappe._dict(
			source_warehouse="RAW", wip_warehouse="WIP", fg_warehouse="FG",
			configured_semi_finished_warehouse="SEMI", semi_finished_warehouse="SEMI",
			semi_finished_warehouse_error=None,
		)
		self.boms = {
			"BOM-FG": {
				"SA": frappe._dict(item_code="SA", qty=1, stock_uom="Nos", bom_no="BOM-SA"),
				"DIRECT": frappe._dict(item_code="DIRECT", qty=2, stock_uom="Nos"),
			},
			"BOM-SA": {"RAW-ITEM": frappe._dict(item_code="RAW-ITEM", qty=3, stock_uom="Nos")},
		}
		self.stock = {("SA", "SEMI"): 100, ("DIRECT", "RAW"): 120}
		self.stack.enter_context(patch.object(shortage, "get_quantity_precision", return_value=6))
		self.stack.enter_context(patch.object(shortage, "apply_current_item_names"))
		self.stack.enter_context(patch.object(shortage, "get_default_bom", return_value=None))
		self.stack.enter_context(patch.object(shortage, "_mr_documents", return_value=[]))
		self.stack.enter_context(patch.object(shortage, "_po_documents", return_value=[]))
		self.stack.enter_context(patch(
			"process_simplification.defaults.warehouse_configuration_error", return_value=None,
		))
		self.stack.enter_context(patch.object(
			shortage, "resolve_production_source_warehouse",
			return_value=frappe._dict(warehouse="RAW", can_use=True, reason=None),
		))
		self.bom_reader = self.stack.enter_context(patch.object(
			shortage, "get_bom_items_as_dict", side_effect=lambda bom, *args, **kwargs: self.boms[bom],
		))
		self.stock_reader = self.stack.enter_context(patch.object(
			shortage, "get_material_stock_snapshot", side_effect=self._snapshot,
		))

	def _snapshot(self, item, warehouse):
		qty = self.stock.get((item, warehouse), 0)
		return frappe._dict(can_calculate=bool(warehouse), actual_qty=qty, available_qty=qty, committed_qty=0)

	def _coverage(self, qty=60, prior_demands=None):
		return shortage.calculate_multilevel_material_coverage(
			[{"bom_no": "BOM-FG", "qty": qty, "source": {"finished_item": "FG"}}],
			"Factory", defaults=self.defaults, prior_demands=prior_demands,
		)

	def test_semi_stock_100_covers_60_without_child_production_and_raw_stays_in_raw(self):
		result = self._coverage()
		semi = next(row for row in result.requirements if row.item_code == "SA")
		self.assertEqual((semi.warehouse, semi.available_qty, semi.production_required_qty), ("SEMI", 60, 0))
		self.assertEqual([(row.item_code, row.warehouse) for row in result.materials], [("DIRECT", "RAW")])
		self.assertEqual(result.shortages, [])
		self.assertEqual([call.args[0] for call in self.bom_reader.call_args_list], ["BOM-FG"])
		self.assertNotIn(("SA", "RAW"), [call.args for call in self.stock_reader.call_args_list])

	def test_only_semi_gap_expands_child_raw_requirements(self):
		self.stock[("SA", "SEMI")] = 40
		result = self._coverage()
		rows = {row.item_code: row for row in result.requirements}
		self.assertEqual(rows["SA"].production_required_qty, 20)
		self.assertEqual((rows["RAW-ITEM"].warehouse, rows["RAW-ITEM"].required_qty), ("RAW", 60))
		self.assertEqual([(row.item_code, row.shortage_qty) for row in result.shortages], [("RAW-ITEM", 60)])

	def test_prior_order_consumes_same_semi_stock_once(self):
		result = self._coverage(prior_demands=[{"bom_no": "BOM-FG", "qty": 60}])
		semi = next(row for row in result.requirements if row.item_code == "SA")
		self.assertEqual((semi.available_qty, semi.production_required_qty), (40, 20))

	def test_blank_option_preserves_shared_source(self):
		self.defaults.configured_semi_finished_warehouse = None
		self.defaults.semi_finished_warehouse = "RAW"
		self.stock[("SA", "RAW")] = 100
		result = self._coverage()
		semi = next(row for row in result.requirements if row.item_code == "SA")
		self.assertEqual((semi.warehouse, semi.available_qty, semi.production_required_qty), ("RAW", 60, 0))

	def test_stock_in_wrong_warehouse_does_not_cover_semi_need(self):
		self.stock[("SA", "SEMI")] = 0
		self.stock[("SA", "RAW")] = 100
		result = self._coverage()
		semi = next(row for row in result.requirements if row.item_code == "SA")
		self.assertEqual((semi.available_qty, semi.production_required_qty), (0, 60))

	def test_explicit_no_child_bom_stays_purchased_even_with_item_default_bom(self):
		self.boms["BOM-FG"]["SA"].bom_no = None
		self.stock[("SA", "RAW")] = 60
		with patch.object(shortage, "get_default_bom", return_value="BOM-SA"):
			# Native direct expansion includes an explicit bom_no for every row.
			self.boms["BOM-FG"]["DIRECT"].bom_no = None
			result = self._coverage()
		semi = next(row for row in result.requirements if row.item_code == "SA")
		self.assertEqual((semi.warehouse, semi.supply_type, semi.available_qty), ("RAW", "purchased", 60))
		self.assertEqual([call.args[0] for call in self.bom_reader.call_args_list], ["BOM-FG"])

	def test_invalid_semi_config_blocks_instead_of_using_raw_stock(self):
		self.defaults.semi_finished_warehouse_error = "warehouse_disabled"
		with patch.object(shortage, "resolve_semi_finished_warehouse", return_value=frappe._dict(
			warehouse="SEMI", configured_warehouse="SEMI", can_use=False, reason="warehouse_disabled",
		)):
			result = self._coverage()
		semi = next(row for row in result.requirements if row.item_code == "SA")
		self.assertTrue(semi.blocked)
		self.assertEqual(semi.status, "cannot_calculate")
		self.assertEqual(semi.available_qty, 0)
		self.assertEqual(semi.production_required_qty, 0)
		self.assertNotIn("RAW-ITEM", [row.item_code for row in result.requirements])


class TestPersistedSemiFinishedRoutes(UnitTestCase):
	def test_existing_work_order_readiness_uses_saved_semi_source(self):
		from process_simplification.api.production_readiness import (
			allocate_work_order_readiness, build_work_order_graph,
		)
		graph = build_work_order_graph(
			{"name": "PLAN", "posting_date": "2026-09-22"},
			[{"name": "WO", "company": "Factory", "production_item": "FG", "qty": 60,
				"status": "Not Started", "production_plan_item": "PLAN-ITEM", "source_warehouse": "RAW"}],
			[{"name": "WOI-SA", "parent": "WO", "item_code": "SA", "source_warehouse": "OLD-SEMI",
				"required_qty": 60, "transferred_qty": 0}],
			[], active_bom_items={"FG", "SA"},
		)
		with patch.object(frappe.db, "get_value", side_effect=AssertionError("No live routing lookup")):
			result = allocate_work_order_readiness([graph], {
				("SA", "OLD-SEMI"): {"actual_qty": 100, "available_qty": 100, "can_calculate": True},
				("SA", "NEW-SEMI"): {"actual_qty": 0, "available_qty": 0, "can_calculate": True},
			})[0].work_orders_by_name["WO"]
		self.assertEqual(result.required_items[0].issue_warehouse, "OLD-SEMI")
		self.assertEqual(result.required_items[0].available_qty, 60)
		self.assertFalse(result.required_items[0].replenishment_required)
		self.assertEqual(result.readiness_status, "ready_now")

	def test_receipt_targets_saved_component_source_not_current_company_default(self):
		target = frappe._dict(
			company="Factory", source_warehouse="RAW", wip_warehouse="WIP",
			required_items=[frappe._dict(name="WOI-SA", item_code="SA", source_warehouse="OLD-SEMI")],
		)
		with patch.object(frappe.db, "get_value", side_effect=AssertionError("No current Company lookup")):
			self.assertEqual(target_item_for_supply(
				frappe._dict(company="Factory", production_item="SA", fg_warehouse="OLD-SEMI"), target,
			).name, "WOI-SA")
			self.assertIsNone(target_item_for_supply(
				frappe._dict(company="Factory", production_item="SA", fg_warehouse="NEW-SEMI"), target,
			))

	def test_mixed_issue_good_returns_follow_each_original_source(self):
		entries = [frappe._dict(name="ISSUE", purpose="Material Transfer for Manufacture", docstatus=1, is_return=0)]
		rows = [
			frappe._dict(parent="ISSUE", item_code="SA", s_warehouse="OLD-SEMI", t_warehouse="WIP", stock_uom="Nos", transfer_qty=60),
			frappe._dict(parent="ISSUE", item_code="RAW-ITEM", s_warehouse="RAW", t_warehouse="WIP", stock_uom="Nos", transfer_qty=120),
		]
		routes = {row.item_code: row for row in aggregate_routes(entries, rows)}
		self.assertEqual(stock_rows(routes["SA"], 10, routes["SA"].return_warehouse)[0]["t_warehouse"], "OLD-SEMI")
		self.assertEqual(stock_rows(routes["RAW-ITEM"], 20, routes["RAW-ITEM"].return_warehouse)[0]["t_warehouse"], "RAW")

	def test_scrap_and_quarantine_cannot_equal_persisted_semi_source(self):
		work_order = frappe._dict(name="WO", company="Factory", source_warehouse="RAW", wip_warehouse="WIP")
		def value(doctype, name, fields, **kwargs):
			if doctype == "Company":
				return frappe._dict(default_scrap_warehouse="OLD-SEMI", custom_material_quarantine_warehouse="OLD-SEMI")
			return frappe._dict(company="Factory", is_group=0, disabled=0)
		with patch.object(exception_service.frappe.db, "get_value", side_effect=value), patch.object(
			exception_service.frappe, "get_all", return_value=[frappe._dict(source_warehouse="OLD-SEMI")],
		) as get_items:
			result = exception_service.material_warehouse_settings(work_order)
		self.assertIsNone(result.scrap_warehouse)
		self.assertIsNone(result.quarantine_warehouse)
		get_items.assert_any_call(
			"Work Order Item", filters={"parent": "WO", "parenttype": "Work Order"}, fields=["source_warehouse"],
		)

	def test_distinct_exception_warehouses_remain_available(self):
		work_order = frappe._dict(company="Factory", source_warehouse="RAW", wip_warehouse="WIP",
			required_items=[frappe._dict(source_warehouse="OLD-SEMI")])
		def value(doctype, name, fields, **kwargs):
			if doctype == "Company":
				return frappe._dict(default_scrap_warehouse="SCRAP", custom_material_quarantine_warehouse="QUARANTINE")
			return frappe._dict(company="Factory", is_group=0, disabled=0)
		with patch.object(exception_service.frappe.db, "get_value", side_effect=value):
			result = exception_service.material_warehouse_settings(work_order)
		self.assertEqual((result.scrap_warehouse, result.quarantine_warehouse), ("SCRAP", "QUARANTINE"))
