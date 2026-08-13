"""Tests for per-Sales-Order-Item attribution on generated purchase requests.

To support per-order material readiness, a purchase Material Request created
from shortage rows must stamp each line with its originating Sales Order Item
(a native field, so nothing in ERPNext core changes). A shortage line that
serves several orders is split proportionally across those Sales Order Items.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase


class TestPurchaseAttribution(UnitTestCase):
	def _capture_mr(self):
		"""Return a fake Material Request doc that records appended item rows."""
		captured = {"items": [], "inserted": False, "submitted": False}

		class FakeMR:
			material_request_type = None
			company = None
			transaction_date = None
			schedule_date = None
			name = "MAT-MR-TEST"
			docstatus = 1

			def append(self, table, row):
				captured["items"].append(frappe._dict(row))

			def insert(self):
				captured["inserted"] = True

			def submit(self):
				captured["submitted"] = True

		return FakeMR(), captured

	def test_material_request_line_split_by_source_sales_order_item(self):
		from process_simplification.api import shortage

		fake_mr, captured = self._capture_mr()

		shortage_rows = [
			{
				"item_code": "RM-SHARED",
				"warehouse": "仓库 - 恒",
				"shortage_qty": 100,
				"purchase_qty": 100,
				"sources": [
					{"sales_order": "SO-A", "sales_order_item": "SOI-A", "required_qty": 30},
					{"sales_order": "SO-B", "sales_order_item": "SOI-B", "required_qty": 70},
				],
			}
		]

		with (
			patch("process_simplification.api.shortage.frappe.has_permission", return_value=True),
			patch("process_simplification.api.shortage.frappe.new_doc", return_value=fake_mr),
			patch("process_simplification.api.shortage.get_company_defaults",
				return_value=frappe._dict({"company": "恒算科技", "source_warehouse": "仓库 - 恒"})),
		):
			result = shortage.create_material_request(shortage_rows, company="恒算科技")

		# One line per source Sales Order Item, split proportionally (30 / 70).
		items = captured["items"]
		self.assertEqual(len(items), 2)
		by_soi = {i.sales_order_item: i for i in items}
		self.assertEqual(set(by_soi), {"SOI-A", "SOI-B"})
		self.assertEqual(by_soi["SOI-A"].sales_order, "SO-A")
		self.assertEqual(by_soi["SOI-A"].qty, 30)
		self.assertEqual(by_soi["SOI-B"].qty, 70)
		# Every line still carries item and warehouse.
		for row in items:
			self.assertEqual(row.item_code, "RM-SHARED")
			self.assertEqual(row.warehouse, "仓库 - 恒")
		self.assertEqual(result["material_request"], "MAT-MR-TEST")

	def test_material_request_line_without_sources_stays_single_unattributed(self):
		from process_simplification.api import shortage

		fake_mr, captured = self._capture_mr()
		shortage_rows = [
			{"item_code": "RM-X", "warehouse": "仓库 - 恒", "shortage_qty": 40, "purchase_qty": 40, "sources": []}
		]

		with (
			patch("process_simplification.api.shortage.frappe.has_permission", return_value=True),
			patch("process_simplification.api.shortage.frappe.new_doc", return_value=fake_mr),
			patch("process_simplification.api.shortage.get_company_defaults",
				return_value=frappe._dict({"company": "恒算科技", "source_warehouse": "仓库 - 恒"})),
		):
			shortage.create_material_request(shortage_rows, company="恒算科技")

		items = captured["items"]
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0].qty, 40)
		self.assertIsNone(items[0].get("sales_order_item"))

	def test_rounding_remainder_goes_to_last_source(self):
		from process_simplification.api import shortage

		fake_mr, captured = self._capture_mr()
		# 10 split across thirds -> 3.33 / 3.33 / 3.34 (remainder to last).
		shortage_rows = [
			{
				"item_code": "RM-3",
				"warehouse": "仓库 - 恒",
				"shortage_qty": 10,
				"purchase_qty": 10,
				"sources": [
					{"sales_order": "SO-1", "sales_order_item": "SOI-1", "required_qty": 1},
					{"sales_order": "SO-2", "sales_order_item": "SOI-2", "required_qty": 1},
					{"sales_order": "SO-3", "sales_order_item": "SOI-3", "required_qty": 1},
				],
			}
		]

		with (
			patch("process_simplification.api.shortage.frappe.has_permission", return_value=True),
			patch("process_simplification.api.shortage.frappe.new_doc", return_value=fake_mr),
			patch("process_simplification.api.shortage.get_company_defaults",
				return_value=frappe._dict({"company": "恒算科技", "source_warehouse": "仓库 - 恒"})),
		):
			shortage.create_material_request(shortage_rows, company="恒算科技")

		qtys = [i.qty for i in captured["items"]]
		# Total is preserved exactly.
		self.assertEqual(sum(qtys), 10)
		self.assertEqual(len(qtys), 3)
