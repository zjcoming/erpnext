from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase
from frappe.utils import add_days, nowdate

from process_simplification.api.utils import SimplifiedFlowError


class TestQuickOrderV2(UnitTestCase):
	def setUp(self):
		super().setUp()
		resolver = patch(
			"process_simplification.api.shortage.resolve_production_source_warehouse",
			side_effect=self._resolve_test_source_warehouse,
		)
		resolver.start()
		self.addCleanup(resolver.stop)

	@staticmethod
	def _resolve_test_source_warehouse(company, *, defaults=None, sales_order_item_warehouse=None):
		warehouse = frappe._dict(defaults or {}).get("source_warehouse") or sales_order_item_warehouse
		return frappe._dict(
			warehouse=warehouse,
			can_use=bool(warehouse),
			reason=None if warehouse else "warehouse_missing",
		)

	def _normalized_order(self):
		return frappe._dict(
			{
				"customer": "_Test Customer",
				"delivery_date": add_days(nowdate(), 1),
				"po_no": "PO-1001",
				"remarks": "同批送达",
				"items": [{"item_code": "_Test Item", "qty": 1, "rate": 10}],
			}
		)

	def test_quick_order_context_is_available_without_a_site_rollout_flag(self):
		from process_simplification.api.quick_order import get_quick_order_context

		defaults = frappe._dict(company="_Test Company", fg_warehouse="_Test Warehouse - _TC")
		with (
			patch("process_simplification.api.quick_order.frappe.conf", frappe._dict()),
			patch("process_simplification.api.quick_order.frappe.has_permission", return_value=True),
			patch("process_simplification.api.quick_order.get_company_defaults", return_value=defaults),
			patch("process_simplification.api.quick_order._selling_price_list", return_value="Standard Selling"),
			patch("process_simplification.api.quick_order.frappe.db.get_value", return_value="CNY"),
			patch("process_simplification.api.quick_order.nowdate", return_value="2099-01-01"),
		):
			result = get_quick_order_context()

		self.assertTrue(result["enabled"])
		self.assertEqual(result["company"], "_Test Company")
		self.assertEqual(result["fg_warehouse"], "_Test Warehouse - _TC")

	def test_normalized_payload_keeps_only_supported_quick_order_fields(self):
		from process_simplification.api.quick_order import normalize_quick_order_payload

		result = normalize_quick_order_payload(
			{
				"customer": " _Test Customer ",
				"delivery_date": "2099-01-01",
				"po_no": " PO-1001 ",
				"remarks": "  同批送达  ",
				"items": [{"item_code": " _Test Item ", "qty": "2", "rate": "9.5"}],
			}
		)

		self.assertEqual(result.customer, "_Test Customer")
		self.assertEqual(result.po_no, "PO-1001")
		self.assertEqual(result.remarks, "同批送达")
		self.assertEqual(result.get("items"), [{"item_code": "_Test Item", "qty": 2.0, "rate": 9.5}])

	def test_normalized_payload_rejects_advanced_line_fields(self):
		from process_simplification.api.quick_order import normalize_quick_order_payload

		with self.assertRaises(SimplifiedFlowError):
			normalize_quick_order_payload(
				{
					"customer": "_Test Customer",
					"delivery_date": add_days(nowdate(), 1),
					"items": [
						{
							"item_code": "_Test Item",
							"qty": 1,
							"rate": 10,
							"warehouse": "Stores - _TC",
						}
					],
				}
			)

	def test_normalized_payload_rejects_removed_partial_delivery_field_even_when_false(self):
		from process_simplification.api.quick_order import normalize_quick_order_payload

		with self.assertRaises(SimplifiedFlowError):
			normalize_quick_order_payload(
				{
					"customer": "_Test Customer",
					"delivery_date": add_days(nowdate(), 1),
					"allow_partial_delivery": 0,
					"items": [{"item_code": "_Test Item", "qty": 1, "rate": 10}],
				}
			)

	def test_normalized_payload_rejects_zero_rate(self):
		from process_simplification.api.quick_order import normalize_quick_order_payload

		with self.assertRaises(SimplifiedFlowError):
			normalize_quick_order_payload(
				{
					"customer": "_Test Customer",
					"delivery_date": add_days(nowdate(), 1),
					"items": [{"item_code": "_Test Item", "qty": 1, "rate": 0}],
				}
			)

	def test_normalized_payload_rejects_past_delivery_date(self):
		from process_simplification.api.quick_order import normalize_quick_order_payload

		with self.assertRaises(SimplifiedFlowError):
			normalize_quick_order_payload(
				{
					"customer": "_Test Customer",
					"delivery_date": add_days(nowdate(), -1),
					"items": [{"item_code": "_Test Item", "qty": 1, "rate": 10}],
				}
			)

	@patch("process_simplification.api.quick_order.get_quick_order_item_defaults")
	def test_lightweight_preview_uses_reservable_qty_and_allows_stock_covered_item_without_bom(self, defaults):
		from process_simplification.api.quick_order import preview_quick_order_items

		defaults.return_value = {
			"item_code": "FG-001",
			"item_name": "Finished Good",
			"stock_uom": "Nos",
			"warehouse": "Finished Goods - TC",
			"rate": 5,
			"currency": "CNY",
			"price_list": "Standard Selling",
			"available_to_reserve": 10,
			"bom_no": None,
			"has_bom": False,
		}

		result = preview_quick_order_items([{"item_code": "FG-001", "qty": 8}])

		self.assertEqual(result["rows"][0]["available_to_reserve"], 8)
		self.assertEqual(result["rows"][0]["production_required"], 0)
		self.assertFalse(result["rows"][0]["blocked"])

	@patch("process_simplification.api.quick_order.get_quick_order_item_defaults")
	def test_lightweight_preview_blocks_production_without_bom(self, defaults):
		from process_simplification.api.quick_order import preview_quick_order_items

		defaults.return_value = {
			"item_code": "FG-001",
			"item_name": "Finished Good",
			"stock_uom": "Nos",
			"warehouse": "Finished Goods - TC",
			"rate": 5,
			"currency": "CNY",
			"price_list": "Standard Selling",
			"available_to_reserve": 2,
			"bom_no": None,
			"has_bom": False,
		}

		result = preview_quick_order_items([{"item_code": "FG-001", "qty": 8}])

		self.assertEqual(result["rows"][0]["production_required"], 6)
		self.assertTrue(result["rows"][0]["blocked"])
		self.assertEqual(result["rows"][0]["issues"][0]["code"], "PRODUCTION_BOM_MISSING")

	@patch(
		"process_simplification.api.production.get_prior_finished_stock_allocations",
		return_value={("FG-001", "Finished Goods - TC"): 16},
	)
	@patch("process_simplification.api.quick_order.get_quick_order_item_defaults")
	def test_late_delivery_preview_does_not_reuse_stock_allocated_to_prior_orders(
		self, defaults, prior_allocations
	):
		from process_simplification.api.quick_order import preview_quick_order_items

		defaults.return_value = {
			"item_code": "FG-001",
			"item_name": "Finished Good",
			"stock_uom": "Nos",
			"warehouse": "Finished Goods - TC",
			"available_to_reserve": 16,
			"bom_no": "BOM-FG-001",
		}

		result = preview_quick_order_items(
			[{"item_code": "FG-001", "qty": 3}],
			company="_Test Company",
			delivery_date="2099-01-10",
		)

		prior_allocations.assert_called_once_with(
			"_Test Company", target_delivery_date="2099-01-10"
		)
		self.assertEqual(result["rows"][0]["available_to_reserve"], 0)
		self.assertEqual(result["rows"][0]["production_required"], 3)
		self.assertEqual(result["rows"][0]["issues"][0]["code"], "FINISHED_GOODS_SHORTAGE")

	@patch("process_simplification.api.quick_order.get_quick_order_item_defaults")
	def test_lightweight_preview_blocks_when_finished_goods_warehouse_is_missing(self, defaults):
		from process_simplification.api.quick_order import preview_quick_order_items

		defaults.return_value = {
			"item_code": "FG-001",
			"item_name": "Finished Good",
			"stock_uom": "Nos",
			"warehouse": None,
			"available_to_reserve": 0,
			"bom_no": "BOM-FG-001",
		}

		result = preview_quick_order_items([{"item_code": "FG-001", "qty": 1}])

		self.assertTrue(result["rows"][0]["blocked"])
		self.assertEqual(result["rows"][0]["issues"][0]["code"], "FG_WAREHOUSE_MISSING")

	@patch("process_simplification.api.quick_order.frappe.db.exists", return_value=True)
	@patch("process_simplification.api.quick_order.frappe.get_cached_value")
	@patch("process_simplification.api.quick_order.frappe.has_permission")
	def test_item_defaults_reject_active_product_bundle(self, has_permission, get_cached_value, db_exists):
		from process_simplification.api.quick_order import get_quick_order_item_defaults

		get_cached_value.return_value = frappe._dict(
			{
				"item_code": "BUNDLE-001",
				"item_name": "Bundle",
				"stock_uom": "Nos",
				"is_sales_item": 1,
				"is_stock_item": 0,
				"disabled": 0,
				"has_variants": 0,
				"has_serial_no": 0,
				"has_batch_no": 0,
			}
		)

		with self.assertRaises(SimplifiedFlowError):
			get_quick_order_item_defaults("BUNDLE-001", "_Test Company")

		db_exists.assert_called_once_with(
			"Product Bundle",
			{"new_item_code": "BUNDLE-001", "disabled": 0},
		)

	def test_lightweight_preview_batches_multiple_lines(self):
		from process_simplification.api.quick_order import preview_quick_order_items

		def item_defaults(item_code, company=None):
			return {
				"item_code": item_code,
				"item_name": item_code,
				"stock_uom": "Nos",
				"warehouse": "Finished Goods - TC",
				"available_to_reserve": 3 if item_code == "FG-001" else 0,
				"bom_no": "BOM-{0}".format(item_code),
			}

		with patch(
			"process_simplification.api.quick_order.get_quick_order_item_defaults",
			side_effect=item_defaults,
		):
			result = preview_quick_order_items(
				[
					{"item_code": "FG-001", "qty": 2},
					{"item_code": "FG-002", "qty": 5},
				]
			)

		self.assertEqual(len(result["rows"]), 2)
		self.assertEqual(result["available_to_reserve"], 2)
		self.assertEqual(result["production_required"], 5)

	@patch(
		"process_simplification.api.quick_order.get_quick_order_item_defaults",
		side_effect=frappe.PermissionError,
	)
	def test_lightweight_preview_returns_blocker_for_inaccessible_item(self, defaults):
		from process_simplification.api.quick_order import preview_quick_order_items

		result = preview_quick_order_items([{"item_code": "SECRET-ITEM", "qty": 1}])

		self.assertTrue(result["rows"][0]["blocked"])
		self.assertEqual(result["rows"][0]["issues"][0]["code"], "ITEM_UNAVAILABLE")

	@patch("process_simplification.api.quick_order.frappe.get_single_value", return_value=0)
	@patch("process_simplification.api.quick_order.frappe.db.get_value", return_value="SO-0001")
	def test_duplicate_customer_po_is_a_named_blocker(self, db_get_value, get_single_value):
		from process_simplification.api.quick_order import _customer_po_issue

		issue = _customer_po_issue("_Test Customer", "PO-1001")

		self.assertEqual(issue["code"], "DUPLICATE_CUSTOMER_PO")
		self.assertEqual(issue["severity"], "blocker")

	@patch("process_simplification.api.quick_order.frappe.get_single_value", return_value=1)
	@patch("process_simplification.api.quick_order.frappe.db.get_value", return_value="SO-0001")
	def test_duplicate_customer_po_is_warning_when_erpnext_allows_it(self, db_get_value, get_single_value):
		from process_simplification.api.quick_order import _customer_po_issue

		issue = _customer_po_issue("_Test Customer", "PO-1001")

		self.assertEqual(issue["code"], "DUPLICATE_CUSTOMER_PO_ALLOWED")
		self.assertEqual(issue["severity"], "warning")

	@patch("process_simplification.api.quick_order._standard_validate_sales_order")
	def test_credit_limit_failure_is_a_named_blocker(self, standard_validate):
		from process_simplification.api.quick_order import _validate_commercial_rules

		order = MagicMock()
		order.check_credit_limit.side_effect = frappe.ValidationError("credit exceeded")
		issues = _validate_commercial_rules(order)

		self.assertEqual(issues[0]["code"], "CREDIT_LIMIT")
		self.assertEqual(issues[0]["severity"], "blocker")

	@patch("process_simplification.api.quick_order._validate_commercial_rules", return_value=[])
	@patch("process_simplification.api.quick_order._build_sales_order")
	@patch("process_simplification.api.quick_order.calculate_multilevel_material_coverage")
	@patch("process_simplification.api.quick_order.preview_quick_order_items")
	@patch("process_simplification.api.quick_order._customer_po_issue", return_value=None)
	@patch("process_simplification.api.quick_order._validate_customer", return_value=[])
	@patch("process_simplification.api.quick_order.get_company_defaults")
	@patch("process_simplification.api.quick_order.frappe.has_permission")
	def test_production_and_material_shortages_are_warning_only(
		self,
		has_permission,
		get_company_defaults,
		validate_customer,
		customer_po_issue,
		preview,
		material_coverage,
		build_sales_order,
		commercial_rules,
	):
		from process_simplification.api.quick_order import _evaluate_quick_order

		get_company_defaults.return_value = frappe._dict(
			{"company": "_Test Company", "source_warehouse": "Stores - TC"}
		)
		preview.return_value = {
			"rows": [
				{
					"row": 1,
					"item_code": "_Test Item",
					"item_name": "Test Finished Good",
					"qty": 10,
					"warehouse": "Finished Goods - TC",
					"available_to_reserve": 0,
					"production_required": 1,
					"bom_no": "BOM-_Test Item-001",
					"issues": [
						{
							"code": "FINISHED_GOODS_SHORTAGE",
							"severity": "warning",
							"message": "需要生产 1。",
							"scope": "line",
							"row": 1,
						}
					],
				}
			],
			"available_to_reserve": 0,
			"production_required": 1,
		}
		material_coverage.return_value = frappe._dict(
			{
				"requirements": [
					{
						"item_code": "RM-001",
						"item_name": "Raw Material",
						"warehouse": "Stores - TC",
						"required_qty": 20,
						"available_qty": 15,
						"current_gap_qty": 5,
						"shortage_qty": 5,
						"status": "new_purchase_required",
						"supply_type": "purchased",
						"level": 1,
						"sources": [{"row": 1, "required_qty": 20, "bom_qty_per_unit": 20}],
					}
				],
				"materials": [
					{
						"item_code": "RM-001",
						"item_name": "Raw Material",
						"warehouse": "Stores - TC",
						"required_qty": 20,
						"available_qty": 15,
						"open_material_request_qty": 0,
						"open_purchase_order_qty": 0,
						"shortage_qty": 5,
						"status": "new_purchase_required",
						"sources": [
							{
								"row": 1,
								"finished_item": "_Test Item",
								"production_qty": 1,
								"bom_no": "BOM-_Test Item-001",
								"required_qty": 20,
								"bom_qty_per_unit": 20,
							}
						],
					}
				],
				"shortages": [
					{
						"item_code": "RM-001",
						"shortage_qty": 5,
					}
				],
			}
		)
		order = MagicMock()
		order.grand_total = 10
		order.currency = "CNY"
		build_sales_order.return_value = order

		result = _evaluate_quick_order(self._normalized_order())

		self.assertTrue(result["can_submit"])
		self.assertEqual(
			{issue["code"] for issue in result["warnings"]},
			{"FINISHED_GOODS_SHORTAGE", "RAW_MATERIAL_SHORTAGE"},
		)
		self.assertEqual(result["shortage_item_count"], 1)
		self.assertEqual(result["material_groups"][0]["item_code"], "_Test Item")
		self.assertEqual(result["material_groups"][0]["bom_no"], "BOM-_Test Item-001")
		self.assertEqual(result["material_groups"][0]["materials"][0]["required_qty"], 20)
		self.assertEqual(result["material_coverage"][0]["shortage_qty"], 5)
		coverage_demand = material_coverage.call_args.args[0][0]
		self.assertEqual(coverage_demand["source"]["sales_order_item_warehouse"], "Finished Goods - TC")

	@patch("process_simplification.api.quick_order._validate_commercial_rules", return_value=[])
	@patch("process_simplification.api.quick_order._build_sales_order")
	@patch("process_simplification.api.quick_order.calculate_multilevel_material_coverage")
	@patch("process_simplification.api.quick_order.preview_quick_order_items")
	@patch("process_simplification.api.quick_order._customer_po_issue", return_value=None)
	@patch("process_simplification.api.quick_order._validate_customer", return_value=[])
	@patch("process_simplification.api.quick_order.get_company_defaults")
	@patch("process_simplification.api.quick_order.frappe.has_permission")
	def test_material_without_source_warehouse_blocks_its_finished_good_row(
		self,
		has_permission,
		get_company_defaults,
		validate_customer,
		customer_po_issue,
		preview,
		material_coverage,
		build_sales_order,
		commercial_rules,
	):
		from process_simplification.api.quick_order import _evaluate_quick_order

		get_company_defaults.return_value = frappe._dict({"company": "_Test Company"})
		preview.return_value = {
			"rows": [
				{
					"row": 1,
					"item_code": "FG-001",
					"item_name": "Finished Good",
					"qty": 1,
					"warehouse": "Finished Goods - TC",
					"available_to_reserve": 0,
					"production_required": 1,
					"bom_no": "BOM-FG-001",
					"issues": [],
				}
			],
			"available_to_reserve": 0,
			"production_required": 1,
		}
		material_coverage.return_value = frappe._dict(
			{
				"requirements": [
					{
						"item_code": "RM-001",
						"warehouse": None,
						"status": "cannot_calculate",
						"supply_type": "purchased",
						"level": 1,
						"sources": [{"row": 1, "required_qty": 1}],
					}
				],
				"materials": [
					{
						"item_code": "RM-001",
						"warehouse": None,
						"status": "cannot_calculate",
						"sources": [{"row": 1, "required_qty": 1}],
					}
				],
				"shortages": [],
			}
		)

		result = _evaluate_quick_order(self._normalized_order())

		self.assertFalse(result["can_submit"])
		self.assertIn(
			("RAW_MATERIAL_WAREHOUSE_MISSING", "line", 1),
			{(issue["code"], issue["scope"], issue["row"]) for issue in result["blockers"]},
		)
		self.assertEqual(result["shortage_item_count"], 0)

	@patch("process_simplification.api.quick_order.calculate_multilevel_material_coverage")
	@patch("process_simplification.api.quick_order.preview_quick_order_items")
	@patch("process_simplification.api.quick_order._customer_po_issue", return_value=None)
	@patch("process_simplification.api.quick_order._validate_customer", return_value=[])
	@patch("process_simplification.api.quick_order.get_company_defaults")
	@patch("process_simplification.api.quick_order.frappe.has_permission")
	def test_bom_explosion_failure_blocks_each_affected_production_row(
		self,
		has_permission,
		get_company_defaults,
		validate_customer,
		customer_po_issue,
		preview,
		material_coverage,
	):
		from process_simplification.api.quick_order import _evaluate_quick_order
		from process_simplification.api.shortage import MaterialCoverageBomExpansionError

		get_company_defaults.return_value = frappe._dict({"company": "_Test Company"})
		preview.return_value = {
			"rows": [
				{
					"row": 1,
					"item_code": "FG-001",
					"qty": 1,
					"warehouse": "Finished Goods - TC",
					"available_to_reserve": 0,
					"production_required": 1,
					"bom_no": "BOM-FG-001",
					"issues": [],
				},
				{
					"row": 2,
					"item_code": "FG-002",
					"qty": 2,
					"warehouse": "Finished Goods - TC",
					"available_to_reserve": 0,
					"production_required": 2,
					"bom_no": "BOM-FG-002",
					"issues": [],
				},
			],
			"available_to_reserve": 0,
			"production_required": 3,
		}
		material_coverage.side_effect = MaterialCoverageBomExpansionError("BOM explosion failed")

		result = _evaluate_quick_order(self._normalized_order())

		self.assertFalse(result["can_submit"])
		self.assertEqual(
			{
				(issue["code"], issue["scope"], issue["row"])
				for issue in result["blockers"]
				if issue["code"] == "BOM_EXPLOSION_FAILED"
			},
			{
				("BOM_EXPLOSION_FAILED", "line", 1),
				("BOM_EXPLOSION_FAILED", "line", 2),
			},
		)

	@patch("process_simplification.api.quick_order.calculate_multilevel_material_coverage")
	@patch("process_simplification.api.quick_order.preview_quick_order_items")
	@patch("process_simplification.api.quick_order._customer_po_issue", return_value=None)
	@patch("process_simplification.api.quick_order._validate_customer", return_value=[])
	@patch("process_simplification.api.quick_order.get_company_defaults")
	@patch("process_simplification.api.quick_order.frappe.has_permission")
	def test_non_bom_material_coverage_error_is_not_mislabeled_as_bom_failure(
		self,
		has_permission,
		get_company_defaults,
		validate_customer,
		customer_po_issue,
		preview,
		material_coverage,
	):
		from process_simplification.api.quick_order import _evaluate_quick_order

		get_company_defaults.return_value = frappe._dict({"company": "_Test Company"})
		preview.return_value = {
			"rows": [
				{
					"row": 1,
					"item_code": "FG-001",
					"qty": 1,
					"warehouse": "Finished Goods - TC",
					"available_to_reserve": 0,
					"production_required": 1,
					"bom_no": "BOM-FG-001",
					"issues": [],
				}
			],
			"available_to_reserve": 0,
			"production_required": 1,
		}
		material_coverage.side_effect = RuntimeError("stock snapshot failed")

		with self.assertRaisesRegex(RuntimeError, "stock snapshot failed"):
			_evaluate_quick_order(self._normalized_order())

	@patch(
		"process_simplification.api.shortage.get_bom_items_as_dict",
		side_effect=frappe.ValidationError("BOM explosion failed"),
	)
	def test_material_coverage_marks_only_bom_expansion_errors_for_preflight(self, get_bom_items):
		from process_simplification.api.shortage import (
			MaterialCoverageBomExpansionError,
			calculate_material_coverage,
		)

		with self.assertRaises(MaterialCoverageBomExpansionError) as raised:
			calculate_material_coverage(
				[{"bom_no": "BOM-FG-001", "qty": 1}],
				"_Test Company",
				defaults=frappe._dict({"source_warehouse": "Stores - TC"}),
			)

		self.assertIsInstance(raised.exception.__cause__, frappe.ValidationError)

	def test_review_fingerprint_ignores_check_time_but_detects_material_change(self):
		from process_simplification.api.quick_order import quick_order_review_fingerprint

		base = {
			"intent_digest": "intent-1",
			"company": "_Test Company",
			"currency": "CNY",
			"grand_total": 100,
			"available_to_reserve": 3,
			"production_required": 7,
			"shortage_item_count": 2,
			"blockers": [],
			"warnings": [{"code": "RAW_MATERIAL_SHORTAGE"}],
			"rows": [
				{
					"item_code": "FG-001",
					"qty": 1,
					"warehouse": "Finished Goods - TC",
					"available_to_reserve": 3,
					"production_required": 7,
					"bom_no": "BOM-FG-001",
				}
			],
			"commercial_rows": [
				{
					"item_code": "FG-001",
					"qty": 1,
					"rate": 100,
					"amount": 100,
					"net_rate": 100,
					"net_amount": 100,
					"warehouse": "Finished Goods - TC",
					"bom_no": "BOM-FG-001",
					"uom": "Nos",
					"conversion_factor": 1,
					"delivery_date": "2026-08-31",
				}
			],
			"material_coverage": [
				{
					"item_code": "RM-001",
					"warehouse": "Stores - TC",
					"required_qty": 10,
					"available_qty": 5,
					"open_material_request_qty": 0,
					"open_purchase_order_qty": 0,
					"shortage_qty": 5,
					"status": "new_purchase_required",
				}
			],
			"material_requirements": [
				{
					"item_code": "SA-001",
					"warehouse": "Stores - TC",
					"required_qty": 7,
					"available_qty": 2,
					"current_gap_qty": 5,
					"production_required_qty": 5,
					"shortage_qty": 0,
					"supply_type": "manufactured",
					"status": "production_required",
				}
			],
			"checked_at": "2026-08-01 10:00:00",
		}
		later = {**base, "checked_at": "2026-08-01 10:01:00"}
		changed = {
			**later,
			"material_coverage": [{**later["material_coverage"][0], "available_qty": 4}],
		}
		changed_subassembly = {
			**later,
			"material_requirements": [
				{**later["material_requirements"][0], "production_required_qty": 4}
			],
		}
		changed_company = {**later, "company": "Another Company"}
		changed_currency = {**later, "currency": "USD"}
		changed_warehouse = {
			**later,
			"rows": [{**later["rows"][0], "warehouse": "Overflow - TC"}],
		}
		changed_rate_with_same_total = {
			**later,
			"commercial_rows": [
				{
					**later["commercial_rows"][0],
					"rate": 90,
					"amount": 90,
					"net_rate": 90,
					"net_amount": 90,
				}
			],
		}

		self.assertEqual(quick_order_review_fingerprint(base), quick_order_review_fingerprint(later))
		self.assertNotEqual(quick_order_review_fingerprint(base), quick_order_review_fingerprint(changed))
		self.assertNotEqual(
			quick_order_review_fingerprint(base),
			quick_order_review_fingerprint(changed_subassembly),
		)
		for changed_commercial_fact in (
			changed_company,
			changed_currency,
			changed_warehouse,
			changed_rate_with_same_total,
		):
			self.assertNotEqual(
				quick_order_review_fingerprint(base),
				quick_order_review_fingerprint(changed_commercial_fact),
			)

	def test_standard_sales_order_mapping_preserves_po_remark_and_bom_snapshot(self):
		from process_simplification.api.quick_order import _build_sales_order

		data = self._normalized_order()
		preview = [
			{
				"item_code": "_Test Item",
				"warehouse": "_Test Warehouse - _TC",
				"production_required": 1,
				"bom_no": "BOM-_Test Item-001",
			}
		]
		order = _build_sales_order(data, preview, "_Test Company")

		self.assertEqual(order.doctype, "Sales Order")
		self.assertEqual(order.po_no, "PO-1001")
		self.assertEqual(order.terms, "同批送达")
		self.assertNotIn("分批", order.terms)
		self.assertEqual(order.items[0].delivery_date, data.delivery_date)
		self.assertEqual(order.items[0].warehouse, "_Test Warehouse - _TC")
		self.assertEqual(order.items[0].bom_no, "BOM-_Test Item-001")

	def test_idempotency_record_name_is_user_bound(self):
		from process_simplification.api.quick_order import quick_order_idempotency_name

		first = quick_order_idempotency_name("owner@example.com", "request-1")
		retry = quick_order_idempotency_name("owner@example.com", "request-1")
		other_user = quick_order_idempotency_name("other@example.com", "request-1")

		self.assertEqual(first, retry)
		self.assertNotEqual(first, other_user)
		self.assertEqual(len(first), 40)

	def test_completed_idempotency_record_returns_the_existing_sales_order(self):
		from process_simplification.api.quick_order import _existing_idempotency_result

		record = frappe._dict(
			{"intent_digest": "intent-1", "status": "Completed", "sales_order": "SO-0001"}
		)
		with patch("process_simplification.api.quick_order.frappe.db.exists", return_value=True):
			result = _existing_idempotency_result(record, "intent-1")

		self.assertEqual(result["sales_order"], "SO-0001")
		self.assertTrue(result["idempotent_replay"])

	def test_idempotency_key_cannot_be_reused_for_another_intent(self):
		from process_simplification.api.quick_order import _existing_idempotency_result

		record = frappe._dict(
			{"intent_digest": "intent-1", "status": "Completed", "sales_order": "SO-0001"}
		)
		with self.assertRaises(SimplifiedFlowError):
			_existing_idempotency_result(record, "intent-2")

	@patch("process_simplification.api.quick_order.frappe.db.commit")
	@patch("process_simplification.api.quick_order._create_idempotency_record")
	@patch("process_simplification.api.quick_order._evaluate_quick_order")
	@patch("process_simplification.api.quick_order._get_review_token")
	@patch("process_simplification.api.quick_order.normalize_quick_order_payload")
	@patch("process_simplification.api.quick_order.frappe.has_permission")
	def test_guarded_submission_commits_before_releasing_the_concurrency_lock(
		self,
		has_permission,
		normalize_payload,
		get_review_token,
		evaluate,
		create_record,
		db_commit,
	):
		from process_simplification.api.quick_order import _quick_order_intent_digest, submit_quick_sales_order

		data = self._normalized_order()
		intent_digest = _quick_order_intent_digest(data)
		normalize_payload.return_value = data
		get_review_token.return_value = frappe._dict(
			{"intent_digest": intent_digest, "review_fingerprint": "review-1"}
		)
		order = MagicMock(name="sales_order")
		order.name = "SO-0001"
		order.docstatus = 1
		evaluate.return_value = {
			"can_submit": True,
			"review_fingerprint": "review-1",
			"company": "_Test Company",
			"shortages": [{"item_code": "_Test Item"}],
			"_sales_order": order,
		}
		record = MagicMock(name="idempotency_record")
		create_record.return_value = record

		lock = MagicMock()
		lock.__enter__ = MagicMock(return_value=lock)
		lock.__exit__ = MagicMock(return_value=False)
		with (
			patch("process_simplification.api.quick_order.frappe.cache.lock", return_value=lock),
			patch("process_simplification.api.quick_order.frappe.db.exists", return_value=False),
			patch("process_simplification.api.quick_order.frappe.db.savepoint"),
			patch(
				"process_simplification.notifications.notify_quick_order_shortage"
			) as notify_shortage,
		):
			result = submit_quick_sales_order(data, "review-token", "request-1")

		self.assertEqual(result["sales_order"], "SO-0001")
		order.insert.assert_called_once_with()
		order.submit.assert_called_once_with()
		self.assertEqual(record.status, "Completed")
		notify_shortage.assert_called_once_with(
			"SO-0001",
			"_Test Company",
			[{"item_code": "_Test Item"}],
		)
		db_commit.assert_called_once_with()

	@patch("process_simplification.api.quick_order.frappe.db.commit")
	@patch("process_simplification.api.quick_order._create_idempotency_record")
	@patch("process_simplification.api.quick_order._evaluate_quick_order")
	@patch("process_simplification.api.quick_order._get_review_token")
	@patch("process_simplification.api.quick_order.normalize_quick_order_payload")
	@patch("process_simplification.api.quick_order.frappe.has_permission")
	def test_same_key_retry_replays_completed_order_without_second_insert(
		self,
		has_permission,
		normalize_payload,
		get_review_token,
		evaluate,
		create_record,
		db_commit,
	):
		from process_simplification.api.quick_order import _quick_order_intent_digest, submit_quick_sales_order

		data = self._normalized_order()
		intent_digest = _quick_order_intent_digest(data)
		normalize_payload.return_value = data
		get_review_token.return_value = frappe._dict(
			{"intent_digest": intent_digest, "review_fingerprint": "review-1"}
		)
		order = MagicMock(name="sales_order")
		order.name = "SO-0001"
		order.docstatus = 1
		evaluate.return_value = {
			"can_submit": True,
			"review_fingerprint": "review-1",
			"_sales_order": order,
		}
		create_record.return_value = MagicMock(name="new_idempotency_record")
		completed_record = frappe._dict(
			{"intent_digest": intent_digest, "status": "Completed", "sales_order": "SO-0001"}
		)
		idempotency_lookups = iter([False, True])

		def exists(doctype, name):
			if doctype == "Quick Order Idempotency":
				return next(idempotency_lookups)
			if doctype == "Sales Order":
				return True
			return False

		lock = MagicMock()
		lock.__enter__ = MagicMock(return_value=lock)
		lock.__exit__ = MagicMock(return_value=False)
		with (
			patch("process_simplification.api.quick_order.frappe.cache.lock", return_value=lock) as cache_lock,
			patch("process_simplification.api.quick_order.frappe.db.exists", side_effect=exists),
			patch("process_simplification.api.quick_order.frappe.get_doc", return_value=completed_record),
			patch("process_simplification.api.quick_order.frappe.db.savepoint"),
		):
			first = submit_quick_sales_order(data, "review-token", "request-1")
			retry = submit_quick_sales_order(data, "review-token", "request-1")

		self.assertEqual(first["sales_order"], "SO-0001")
		self.assertTrue(retry["idempotent_replay"])
		self.assertEqual(retry["sales_order"], "SO-0001")
		order.insert.assert_called_once_with()
		order.submit.assert_called_once_with()
		evaluate.assert_called_once_with(data)
		self.assertEqual(cache_lock.call_count, 2)
		self.assertEqual(cache_lock.call_args_list[0], cache_lock.call_args_list[1])

	@patch("process_simplification.api.quick_order._create_idempotency_record")
	@patch("process_simplification.api.quick_order._evaluate_quick_order")
	@patch("process_simplification.api.quick_order._get_review_token")
	@patch("process_simplification.api.quick_order.normalize_quick_order_payload")
	@patch("process_simplification.api.quick_order.frappe.has_permission")
	def test_guarded_submission_rolls_back_order_and_in_progress_record_on_failure(
		self,
		has_permission,
		normalize_payload,
		get_review_token,
		evaluate,
		create_record,
	):
		from process_simplification.api.quick_order import _quick_order_intent_digest, submit_quick_sales_order

		data = self._normalized_order()
		intent_digest = _quick_order_intent_digest(data)
		normalize_payload.return_value = data
		get_review_token.return_value = frappe._dict(
			{"intent_digest": intent_digest, "review_fingerprint": "review-1"}
		)
		order = MagicMock(name="sales_order")
		order.submit.side_effect = frappe.ValidationError("submit failed")
		evaluate.return_value = {
			"can_submit": True,
			"review_fingerprint": "review-1",
			"_sales_order": order,
		}
		create_record.return_value = MagicMock(name="idempotency_record")
		lock = MagicMock()
		lock.__enter__ = MagicMock(return_value=lock)
		lock.__exit__ = MagicMock(return_value=False)

		with (
			patch("process_simplification.api.quick_order.frappe.cache.lock", return_value=lock),
			patch("process_simplification.api.quick_order.frappe.db.exists", return_value=False),
			patch("process_simplification.api.quick_order.frappe.db.savepoint"),
			patch("process_simplification.api.quick_order.frappe.db.rollback") as rollback,
			self.assertRaises(SimplifiedFlowError),
		):
			submit_quick_sales_order(data, "review-token", "request-1")

		rollback.assert_called_once_with(save_point="quick_order_submit")

	@patch("process_simplification.api.quick_order._issue_review_token", return_value="review-token-2")
	@patch("process_simplification.api.quick_order._evaluate_quick_order")
	@patch("process_simplification.api.quick_order._get_review_token")
	@patch("process_simplification.api.quick_order.normalize_quick_order_payload")
	@patch("process_simplification.api.quick_order.frappe.has_permission")
	def test_guarded_submission_requires_reconfirmation_when_material_result_changes(
		self,
		has_permission,
		normalize_payload,
		get_review_token,
		evaluate,
		issue_review_token,
	):
		from process_simplification.api.quick_order import _quick_order_intent_digest, submit_quick_sales_order

		data = self._normalized_order()
		intent_digest = _quick_order_intent_digest(data)
		normalize_payload.return_value = data
		get_review_token.return_value = frappe._dict(
			{"intent_digest": intent_digest, "review_fingerprint": "old-review"}
		)
		evaluate.return_value = {
			"can_submit": True,
			"review_fingerprint": "new-review",
			"checked_at": "2026-08-01 10:01:00",
			"blockers": [],
			"warnings": [],
		}
		lock = MagicMock()
		lock.__enter__ = MagicMock(return_value=lock)
		lock.__exit__ = MagicMock(return_value=False)

		with (
			patch("process_simplification.api.quick_order.frappe.cache.lock", return_value=lock),
			patch("process_simplification.api.quick_order.frappe.db.exists", return_value=False),
			patch("process_simplification.api.quick_order._create_idempotency_record") as create_record,
		):
			result = submit_quick_sales_order(data, "review-token", "request-1")

		self.assertEqual(result["status"], "reconfirmation_required")
		self.assertEqual(result["review_token"], "review-token-2")
		create_record.assert_not_called()

	@patch(
		"process_simplification.api.shortage._po_documents",
		return_value=[
			{
				"doctype": "Purchase Order",
				"name": "PO-001",
				"status": "To Receive",
				"outstanding_qty": 1,
				"schedule_date": "2099-01-01",
				"is_late": False,
			}
		],
	)
	@patch(
		"process_simplification.api.shortage._mr_documents",
		return_value=[
			{
				"doctype": "Material Request",
				"name": "MR-001",
				"status": "Pending",
				"outstanding_qty": 2,
				"schedule_date": "2099-01-01",
				"is_late": False,
			}
		],
	)
	@patch("process_simplification.api.shortage.get_material_stock_snapshot")
	@patch("process_simplification.api.shortage.get_bom_items_as_dict")
	def test_shared_shortage_calculation_nets_stock_and_open_supply(
		self, get_bom_items, stock_snapshot, mr_documents, po_documents
	):
		from process_simplification.api.shortage import calculate_material_shortages

		get_bom_items.return_value = {
			"RM-001": frappe._dict(
				{
					"item_code": "RM-001",
					"item_name": "Raw Material",
					"stock_uom": "Nos",
					"source_warehouse": "_Test Warehouse - _TC",
					"qty": 10,
				}
			)
		}
		stock_snapshot.return_value = frappe._dict(
			{"can_calculate": True, "actual_qty": 3, "committed_qty": 0, "available_qty": 3}
		)
		result = calculate_material_shortages(
			[{"bom_no": "BOM-FG-001", "qty": 2}],
			"_Test Company",
			frappe._dict({"source_warehouse": "_Test Warehouse - _TC"}),
		)

		self.assertEqual(result[0]["required_qty"], 10)
		self.assertEqual(result[0]["shortage_qty"], 4)

	@patch("process_simplification.api.shortage.frappe.db.get_value")
	def test_material_snapshot_deducts_erpnext_commitments(self, get_value):
		from process_simplification.api.shortage import get_material_stock_snapshot

		get_value.return_value = frappe._dict(
			{
				"actual_qty": 100,
				"reserved_qty": 10,
				"reserved_qty_for_production": 20,
				"reserved_qty_for_sub_contract": 5,
				"reserved_qty_for_production_plan": 3,
			}
		)

		result = get_material_stock_snapshot("RM-001", "Stores - TC")

		# Production reservation is NOT deducted: that reservation is this flow's
		# own production demand, so subtracting it would double-count the same
		# need (demand once, reserved-for-that-demand again) and keep a
		# well-stocked material perpetually "short". Only sales and subcontract
		# commitments reduce availability.
		self.assertTrue(result.can_calculate)
		self.assertEqual(result.actual_qty, 100)
		self.assertEqual(result.committed_qty, 15)
		self.assertEqual(result.available_qty, 85)

	@patch("process_simplification.api.shortage.frappe.db.get_value")
	def test_material_snapshot_requires_an_exact_warehouse(self, get_value):
		from process_simplification.api.shortage import get_material_stock_snapshot

		def bin_for_warehouse(doctype, filters, fields, as_dict=False):
			if filters.get("warehouse") == "Stores A - TC":
				return frappe._dict({"actual_qty": 7, "reserved_qty": 2})
			return frappe._dict({"actual_qty": 99, "reserved_qty": 0})

		get_value.side_effect = bin_for_warehouse

		missing = get_material_stock_snapshot("RM-001", None)
		stores_a = get_material_stock_snapshot("RM-001", "Stores A - TC")
		stores_b = get_material_stock_snapshot("RM-001", "Stores B - TC")

		self.assertFalse(missing.can_calculate)
		self.assertEqual((missing.actual_qty, missing.committed_qty, missing.available_qty), (0, 0, 0))
		self.assertEqual(stores_a.available_qty, 5)
		self.assertEqual(stores_b.available_qty, 99)
		self.assertEqual(
			[call.args[1]["warehouse"] for call in get_value.call_args_list],
			["Stores A - TC", "Stores B - TC"],
		)

	@patch(
		"process_simplification.api.shortage.get_default_bom",
		side_effect=lambda item_code: "BOM-SA-001" if item_code == "SA-001" else None,
	)
	@patch("process_simplification.api.shortage._po_documents", return_value=[])
	@patch("process_simplification.api.shortage._mr_documents", return_value=[])
	@patch("process_simplification.api.shortage.get_material_stock_snapshot")
	@patch("process_simplification.api.shortage.get_bom_items_as_dict")
	def test_multilevel_coverage_nets_subassembly_stock_before_expanding_its_bom(
		self, get_bom_items, stock_snapshot, mr_documents, po_documents, default_bom
	):
		from process_simplification.api.shortage import calculate_multilevel_material_coverage

		get_bom_items.side_effect = lambda bom_no, company, qty, fetch_exploded: {
			"BOM-FG-001": {
				"SA-001": frappe._dict(
					item_code="SA-001",
					item_name="半成品",
					stock_uom="Nos",
					qty=4,
				),
				"RM-DIRECT": frappe._dict(
					item_code="RM-DIRECT",
					item_name="直接原料",
					stock_uom="Nos",
					qty=2,
				),
			},
			"BOM-SA-001": {
				"RM-IN-SA": frappe._dict(
					item_code="RM-IN-SA",
					item_name="半成品原料",
					stock_uom="Nos",
					qty=3,
				),
			},
		}[bom_no]
		stock_snapshot.side_effect = lambda item_code, warehouse: frappe._dict(
			can_calculate=True,
			actual_qty={"SA-001": 1, "RM-DIRECT": 2}.get(item_code, 0),
			committed_qty=0,
			available_qty={"SA-001": 1, "RM-DIRECT": 2}.get(item_code, 0),
		)

		result = calculate_multilevel_material_coverage(
			[
				{
					"bom_no": "BOM-FG-001",
					"qty": 1,
					"source": {"row": 1, "finished_item": "FG-001", "production_qty": 1},
				}
			],
			"_Test Company",
			defaults=frappe._dict(source_warehouse="_Test Warehouse - _TC"),
		)

		self.assertTrue(all(call.kwargs["fetch_exploded"] == 0 for call in get_bom_items.call_args_list))
		self.assertEqual(
			[(row["item_code"], row["level"]) for row in result.requirements],
			[("SA-001", 1), ("RM-IN-SA", 2), ("RM-DIRECT", 1)],
		)
		self.assertEqual(result.requirements[0]["supply_type"], "manufactured")
		default_bom.assert_any_call("SA-001")
		self.assertEqual(result.requirements[0]["available_qty"], 1)
		self.assertEqual(result.requirements[0]["production_required_qty"], 3)
		self.assertEqual(result.requirements[1]["required_qty"], 9)
		self.assertEqual(
			[row["item_code"] for row in result.materials], ["RM-DIRECT", "RM-IN-SA"]
		)
		self.assertEqual(
			[row["item_code"] for row in result.shortages], ["RM-IN-SA"]
		)

	@patch("process_simplification.api.shortage._po_documents", return_value=[])
	@patch("process_simplification.api.shortage._mr_documents", return_value=[])
	@patch("process_simplification.api.shortage.get_material_stock_snapshot")
	@patch("process_simplification.api.shortage.get_bom_items_as_dict")
	def test_multilevel_coverage_does_not_expand_stock_covered_subassembly(
		self, get_bom_items, stock_snapshot, mr_documents, po_documents
	):
		from process_simplification.api.shortage import calculate_multilevel_material_coverage

		get_bom_items.return_value = {
			"SA-001": frappe._dict(
				item_code="SA-001",
				item_name="半成品",
				stock_uom="Nos",
				qty=4,
				bom_no="BOM-SA-001",
			)
		}
		stock_snapshot.return_value = frappe._dict(
			can_calculate=True, actual_qty=4, committed_qty=0, available_qty=4
		)

		result = calculate_multilevel_material_coverage(
			[{"bom_no": "BOM-FG-001", "qty": 1, "source": {"row": 1}}],
			"_Test Company",
			defaults=frappe._dict(source_warehouse="_Test Warehouse - _TC"),
		)

		self.assertEqual(get_bom_items.call_count, 1)
		self.assertEqual(result.requirements[0]["production_required_qty"], 0)
		self.assertEqual(result.materials, [])
		self.assertEqual(result.shortages, [])

	@patch("process_simplification.api.shortage._po_documents", return_value=[])
	@patch("process_simplification.api.shortage._mr_documents", return_value=[])
	@patch("process_simplification.api.shortage.get_material_stock_snapshot")
	@patch("process_simplification.api.shortage.get_bom_items_as_dict")
	def test_material_coverage_returns_sufficient_and_short_materials(
		self, get_bom_items, stock_snapshot, mr_documents, po_documents
	):
		from process_simplification.api.shortage import calculate_material_coverage

		get_bom_items.return_value = {
			"RM-ENOUGH": frappe._dict(
				{
					"item_code": "RM-ENOUGH",
					"item_name": "Enough",
					"stock_uom": "Nos",
					"source_warehouse": "_Test Warehouse - _TC",
					"qty": 4,
				}
			),
			"RM-SHORT": frappe._dict(
				{
					"item_code": "RM-SHORT",
					"item_name": "Short",
					"stock_uom": "Nos",
					"source_warehouse": "_Test Warehouse - _TC",
					"qty": 8,
				}
			),
		}
		stock_snapshot.side_effect = lambda item_code, warehouse: frappe._dict(
			{
				"can_calculate": True,
				"actual_qty": 5 if item_code == "RM-ENOUGH" else 2,
				"committed_qty": 0,
				"available_qty": 5 if item_code == "RM-ENOUGH" else 2,
			}
		)

		result = calculate_material_coverage(
			[{"bom_no": "BOM-FG-001", "qty": 10, "source": {"row": 1, "finished_item": "FG-001"}}],
			"_Test Company",
			need_by_date="2099-01-10",
			defaults=frappe._dict({"source_warehouse": "_Test Warehouse - _TC"}),
		)

		self.assertEqual([row["item_code"] for row in result.materials], ["RM-ENOUGH", "RM-SHORT"])
		self.assertEqual([row["item_code"] for row in result.shortages], ["RM-SHORT"])
		self.assertEqual(result.materials[0]["status"], "ready_now")
		self.assertEqual(result.materials[1]["status"], "new_purchase_required")
		self.assertEqual(result.materials[1]["current_gap_qty"], 6)
		self.assertEqual(result.materials[1]["shortage_qty"], 6)

	@patch(
		"process_simplification.api.shortage._po_documents",
		return_value=[
			{
				"doctype": "Purchase Order",
				"name": "PO-001",
				"status": "To Receive",
				"outstanding_qty": 8,
				"schedule_date": "2099-01-01",
				"is_late": False,
			}
		],
	)
	@patch("process_simplification.api.shortage._mr_documents", return_value=[])
	@patch("process_simplification.api.shortage.get_material_stock_snapshot")
	@patch("process_simplification.api.shortage.get_bom_items_as_dict")
	def test_material_coverage_waits_for_an_on_time_purchase_order(
		self, get_bom_items, stock_snapshot, mr_documents, po_documents
	):
		from process_simplification.api.shortage import calculate_material_coverage

		get_bom_items.return_value = {
			"RM-001": frappe._dict(
				{"item_code": "RM-001", "source_warehouse": "_Test Warehouse - _TC", "qty": 10}
			)
		}
		stock_snapshot.return_value = frappe._dict(
			{"can_calculate": True, "actual_qty": 2, "committed_qty": 0, "available_qty": 2}
		)

		result = calculate_material_coverage(
			[{"bom_no": "BOM-FG-001", "qty": 1}],
			"_Test Company",
			need_by_date="2099-01-10",
			defaults=frappe._dict({"source_warehouse": "_Test Warehouse - _TC"}),
		)

		self.assertEqual(result.materials[0]["status"], "awaiting_purchase_receipt")
		self.assertEqual(result.materials[0]["shortage_qty"], 0)

	@patch("process_simplification.api.shortage._po_documents", return_value=[])
	@patch(
		"process_simplification.api.shortage._mr_documents",
		return_value=[
			{
				"doctype": "Material Request",
				"name": "MR-001",
				"status": "Pending",
				"outstanding_qty": 8,
				"schedule_date": "2099-01-01",
				"is_late": False,
			}
		],
	)
	@patch("process_simplification.api.shortage.get_material_stock_snapshot")
	@patch("process_simplification.api.shortage.get_bom_items_as_dict")
	def test_material_coverage_keeps_unconverted_request_pending(
		self, get_bom_items, stock_snapshot, mr_documents, po_documents
	):
		from process_simplification.api.shortage import calculate_material_coverage

		get_bom_items.return_value = {
			"RM-001": frappe._dict(
				{"item_code": "RM-001", "source_warehouse": "_Test Warehouse - _TC", "qty": 10}
			)
		}
		stock_snapshot.return_value = frappe._dict(
			{"can_calculate": True, "actual_qty": 2, "committed_qty": 0, "available_qty": 2}
		)

		result = calculate_material_coverage(
			[{"bom_no": "BOM-FG-001", "qty": 1}],
			"_Test Company",
			need_by_date="2099-01-10",
			defaults=frappe._dict({"source_warehouse": "_Test Warehouse - _TC"}),
		)

		self.assertEqual(result.materials[0]["status"], "purchase_request_pending")
		self.assertEqual(result.materials[0]["shortage_qty"], 0)
		self.assertEqual(result.shortages, [])

	@patch("process_simplification.api.shortage._po_documents", return_value=[])
	@patch("process_simplification.api.shortage._mr_documents", return_value=[])
	@patch("process_simplification.api.shortage.get_material_stock_snapshot")
	@patch("process_simplification.api.shortage.get_bom_items_as_dict")
	def test_material_coverage_recommends_new_purchase_when_supply_is_late(
		self, get_bom_items, stock_snapshot, mr_documents, po_documents
	):
		from process_simplification.api.shortage import calculate_material_coverage

		get_bom_items.return_value = {
			"RM-001": frappe._dict(
				{"item_code": "RM-001", "source_warehouse": "_Test Warehouse - _TC", "qty": 10}
			)
		}
		stock_snapshot.return_value = frappe._dict(
			{"can_calculate": True, "actual_qty": 2, "committed_qty": 0, "available_qty": 2}
		)

		result = calculate_material_coverage(
			[{"bom_no": "BOM-FG-001", "qty": 1}],
			"_Test Company",
			need_by_date="2099-01-10",
			defaults=frappe._dict({"source_warehouse": "_Test Warehouse - _TC"}),
		)

		self.assertEqual(result.materials[0]["status"], "new_purchase_required")
		self.assertEqual(result.materials[0]["shortage_qty"], 8)

	@patch("process_simplification.api.shortage._po_documents", return_value=[])
	@patch("process_simplification.api.shortage._mr_documents", return_value=[])
	@patch("process_simplification.api.shortage.get_material_stock_snapshot")
	@patch("process_simplification.api.shortage.get_bom_items_as_dict")
	def test_material_coverage_aggregates_shared_material_and_preserves_sources(
		self, get_bom_items, stock_snapshot, mr_documents, po_documents
	):
		from process_simplification.api.shortage import calculate_material_coverage

		get_bom_items.side_effect = [
			{
				"RM-SHARED": frappe._dict(
					{"item_code": "RM-SHARED", "source_warehouse": "_Test Warehouse - _TC", "qty": 4}
				)
			},
			{
				"RM-SHARED": frappe._dict(
					{"item_code": "RM-SHARED", "source_warehouse": "_Test Warehouse - _TC", "qty": 6}
				)
			},
		]
		stock_snapshot.return_value = frappe._dict(
			{"can_calculate": True, "actual_qty": 3, "committed_qty": 0, "available_qty": 3}
		)

		result = calculate_material_coverage(
			[
				{"bom_no": "BOM-FG-001", "qty": 2, "source": {"row": 1, "finished_item": "FG-001", "qty": 2}},
				{"bom_no": "BOM-FG-002", "qty": 3, "source": {"row": 2, "finished_item": "FG-002", "qty": 3}},
			],
			"_Test Company",
			defaults=frappe._dict({"source_warehouse": "_Test Warehouse - _TC"}),
		)

		self.assertEqual(len(result.materials), 1)
		self.assertEqual(result.materials[0]["required_qty"], 10)
		self.assertEqual([source["required_qty"] for source in result.materials[0]["sources"]], [4, 6])
		self.assertEqual([source["bom_qty_per_unit"] for source in result.materials[0]["sources"]], [2, 2])
		stock_snapshot.assert_called_once_with("RM-SHARED", "_Test Warehouse - _TC")

	@patch("process_simplification.api.shortage.get_bom_items_as_dict")
	def test_material_coverage_blocks_missing_source_warehouse(self, get_bom_items):
		from process_simplification.api.shortage import calculate_material_coverage

		get_bom_items.return_value = {
			"RM-001": frappe._dict({"item_code": "RM-001", "qty": 1})
		}

		result = calculate_material_coverage(
			[{"bom_no": "BOM-FG-001", "qty": 1}],
			"_Test Company",
			defaults=frappe._dict({"source_warehouse": None}),
		)

		self.assertEqual(result.materials[0]["status"], "cannot_calculate")
		self.assertTrue(result.materials[0]["blocked"])
		self.assertEqual(result.shortages, [])

	@patch(
		"process_simplification.api.shortage.resolve_production_source_warehouse",
		return_value=frappe._dict(
			{"warehouse": "Invalid Stores - TC", "can_use": False, "reason": "warehouse_disabled"}
		),
	)
	@patch("process_simplification.api.shortage.get_material_stock_snapshot")
	@patch("process_simplification.api.shortage.get_bom_items_as_dict")
	def test_material_coverage_blocks_an_unusable_resolved_work_order_warehouse(
		self, get_bom_items, stock_snapshot, resolve_source_warehouse
	):
		from process_simplification.api.shortage import calculate_material_coverage

		get_bom_items.return_value = {
			"RM-001": frappe._dict({"item_code": "RM-001", "qty": 1})
		}
		result = calculate_material_coverage(
			[
				{
					"bom_no": "BOM-FG-001",
					"qty": 1,
					"source": {"sales_order_item_warehouse": "Finished Goods - TC"},
				}
			],
			"_Test Company",
			defaults=frappe._dict({"source_warehouse": "Invalid Stores - TC"}),
		)

		self.assertEqual(result.materials[0]["warehouse"], "Invalid Stores - TC")
		self.assertEqual(result.materials[0]["status"], "cannot_calculate")
		self.assertTrue(result.materials[0]["blocked"])
		self.assertEqual(result.shortages, [])
		stock_snapshot.assert_not_called()

	@patch("process_simplification.api.actions.create_work_orders_via_production_plan")
	@patch("process_simplification.api.actions.get_allocated_production_row")
	@patch("process_simplification.api.actions.resolve_production_source_warehouse")
	@patch("process_simplification.api.actions.get_default_bom")
	@patch("process_simplification.api.actions.get_company_defaults")
	@patch("process_simplification.api.actions.frappe.get_doc")
	@patch("process_simplification.api.actions.get_sales_order_item")
	@patch("process_simplification.api.actions._row_from_workbench")
	@patch("process_simplification.api.actions.frappe.has_permission")
	def test_create_work_order_checks_all_create_and_submit_permissions_before_writing(
		self,
		has_permission,
		row_from_workbench,
		get_sales_order_item,
		get_doc,
		get_company_defaults,
		get_default_bom,
		resolve_source_warehouse,
		get_allocated_production_row,
		create_via_pp,
	):
		from process_simplification.api.actions import create_work_order

		def permission(doctype, permission_type, throw=False):
			if (doctype, permission_type) == ("Production Plan", "submit"):
				raise frappe.PermissionError
			return True

		has_permission.side_effect = permission
		row_from_workbench.return_value = frappe._dict({"uncovered_qty": 0})

		with self.assertRaises(frappe.PermissionError):
			create_work_order("SO-001", "SOI-001", 1)

		row_from_workbench.assert_not_called()
		create_via_pp.assert_not_called()

	@patch("process_simplification.api.actions.create_work_orders_via_production_plan")
	@patch("process_simplification.api.actions.get_allocated_production_row")
	@patch("process_simplification.api.actions.resolve_production_source_warehouse")
	@patch("process_simplification.api.actions.get_default_bom")
	@patch("process_simplification.api.actions.get_company_defaults")
	@patch("process_simplification.api.actions.frappe.get_doc")
	@patch("process_simplification.api.actions.get_sales_order_item")
	@patch("process_simplification.api.actions._row_from_workbench")
	@patch("process_simplification.api.actions.frappe.has_permission")
	def test_create_work_order_prefers_bom_snapshotted_on_sales_order_item(
		self,
		has_permission,
		row_from_workbench,
		get_sales_order_item,
		get_doc,
		get_company_defaults,
		get_default_bom,
		resolve_source_warehouse,
		get_allocated_production_row,
		create_via_pp,
	):
		from process_simplification.api.actions import create_work_order

		has_permission.return_value = True
		row_from_workbench.return_value = frappe._dict({"uncovered_qty": 4})
		get_allocated_production_row.return_value = frappe._dict({"unplanned_production_qty": 4})
		get_sales_order_item.return_value = frappe._dict(
			{
				"item_code": "FG-001",
				"bom_no": "BOM-FG-001-OLD",
				"warehouse": "Finished Goods - TC",
				"delivery_date": "2099-01-01",
			}
		)
		get_doc.return_value = frappe._dict({"company": "Test Company"})
		get_company_defaults.return_value = frappe._dict(
			{
				"source_warehouse": "Stores - TC",
				"wip_warehouse": "Work In Progress - TC",
				"fg_warehouse": "Finished Goods - TC",
			}
		)
		resolve_source_warehouse.return_value = frappe._dict(
			{"warehouse": "Stores - TC", "can_use": True, "reason": None}
		)
		create_via_pp.return_value = {
			"production_plan": "PP-0001",
			"work_orders": ["WO-0001"],
			"sub_assembly_count": 0,
		}

		result = create_work_order("SO-001", "SOI-001", 4)

		# The BOM snapshotted on the Sales Order Item wins over get_default_bom,
		# and the resolved source warehouse is used to check sub-assembly stock.
		kwargs = create_via_pp.call_args.kwargs
		self.assertEqual(kwargs["bom_no"], "BOM-FG-001-OLD")
		self.assertEqual(kwargs["sub_assembly_warehouse"], "Stores - TC")
		self.assertEqual(kwargs["source_warehouse"], "Stores - TC")
		self.assertEqual(kwargs["fg_warehouse"], "Finished Goods - TC")
		self.assertEqual(kwargs["planned_qty"], 4)
		self.assertEqual(kwargs["sales_order"], "SO-001")
		self.assertEqual(kwargs["sales_order_item"], "SOI-001")
		self.assertEqual(result["work_order"], "WO-0001")
		get_default_bom.assert_not_called()
