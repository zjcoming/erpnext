from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from process_simplification.production_reporting.stock_entry import (
	SubassemblyReservationStockEntryMixin,
	_refresh_subassembly_bin,
)


class TestStockEntryHooks(UnitTestCase):
	def test_submit_refresh_runs_after_native_stock_entry_controller(self):
		events = []

		class NativeStockEntry:
			def on_submit(self):
				events.append("native")
				return "submitted"

		class ExtendedStockEntry(SubassemblyReservationStockEntryMixin, NativeStockEntry):
			pass

		with patch(
			"process_simplification.production_reporting.stock_entry._refresh_subassembly_bin",
			side_effect=lambda doc: events.append("refresh"),
		):
			result = ExtendedStockEntry().on_submit()

		self.assertEqual(result, "submitted")
		self.assertEqual(events, ["native", "refresh"])

	def test_cancel_refresh_runs_after_native_stock_entry_controller(self):
		events = []

		class NativeStockEntry:
			def on_cancel(self):
				events.append("native")
				return "cancelled"

		class ExtendedStockEntry(SubassemblyReservationStockEntryMixin, NativeStockEntry):
			pass

		with patch(
			"process_simplification.production_reporting.stock_entry._refresh_subassembly_bin",
			side_effect=lambda doc: events.append("refresh"),
		):
			result = ExtendedStockEntry().on_cancel()

		self.assertEqual(result, "cancelled")
		self.assertEqual(events, ["native", "refresh"])

	def test_non_manufacture_entry_does_not_refresh_bin(self):
		with patch(
			"process_simplification.production_reporting.stock_entry.frappe.db.get_value"
		) as get_value:
			_refresh_subassembly_bin(
				frappe._dict(purpose="Material Transfer", work_order="WO-1")
			)

		get_value.assert_not_called()

	def test_finished_good_work_order_does_not_refresh_subassembly_bin(self):
		with patch(
			"process_simplification.production_reporting.stock_entry.frappe.db.get_value",
			return_value=frappe._dict(
				production_plan="PP-1",
				production_plan_sub_assembly_item=None,
				production_item="FG-1",
				fg_warehouse="Finished Goods - TC",
			),
		) as get_value:
			_refresh_subassembly_bin(
				frappe._dict(purpose="Manufacture", work_order="WO-FG")
			)

		get_value.assert_called_once()

	def test_subassembly_manufacture_refreshes_exact_output_bin(self):
		bin_doc = MagicMock()
		with (
			patch(
				"process_simplification.production_reporting.stock_entry.frappe.db.get_value",
				side_effect=[
					frappe._dict(
						production_plan="PP-1",
						production_plan_sub_assembly_item="PPSA-1",
						production_item="SA-1",
						fg_warehouse="Stores - TC",
					),
					"BIN-SA-1",
				],
			) as get_value,
			patch(
				"process_simplification.production_reporting.stock_entry.frappe.get_doc",
				return_value=bin_doc,
			) as get_doc,
		):
			_refresh_subassembly_bin(
				frappe._dict(purpose="Manufacture", work_order="WO-SA")
			)

		self.assertEqual(get_value.call_count, 2)
		get_doc.assert_called_once_with("Bin", "BIN-SA-1")
		bin_doc.update_reserved_qty_for_for_sub_assembly.assert_called_once_with()
