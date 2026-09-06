from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from process_simplification.production_reporting.stock_entry import (
	SubassemblyReservationStockEntryMixin,
	_build_row_wise_serial_batch_usage,
	_get_work_order_serial_batch_usage,
	_merge_alternative_fulfillment_usage,
	_rebuild_serial_batch_child_usage,
	_refresh_subassembly_bin,
	_refresh_serial_batch_reservation_usage,
	build_work_order_sre_reconciliation,
)


class TestStockEntryHooks(UnitTestCase):
	def test_guided_receipt_leaves_finished_goods_for_explicit_sales_reservation(self):
		from process_simplification.production_reporting.stock_entry import (
			SubassemblyReservationStockEntryMixin,
		)

		class NativeStockEntry:
			def make_stock_reserve_for_wip_and_fg(self):
				self.native_reservation_calls += 1
				return "native"

		class GuidedStockEntry(SubassemblyReservationStockEntryMixin, NativeStockEntry):
			def __init__(self, **values):
				self.values = values
				self.native_reservation_calls = 0

			def get(self, fieldname):
				return self.values.get(fieldname)

		guided_receipt = GuidedStockEntry(
			purpose="Manufacture",
			work_order="WO-GUIDED",
			custom_process_workflow_action="Receipt Request",
		)
		self.assertIsNone(guided_receipt.make_stock_reserve_for_wip_and_fg())
		self.assertEqual(guided_receipt.native_reservation_calls, 0)

		for values in (
			{
				"purpose": "Material Transfer for Manufacture",
				"work_order": "WO-GUIDED",
				"custom_process_workflow_action": "Material Issue Request",
			},
			{
				"purpose": "Manufacture",
				"work_order": "WO-NATIVE",
				"custom_process_workflow_action": None,
			},
		):
			entry = GuidedStockEntry(**values)
			self.assertEqual(entry.make_stock_reserve_for_wip_and_fg(), "native")
			self.assertEqual(entry.native_reservation_calls, 1)

	def test_serial_batch_usage_is_attributed_to_the_required_original_item(self):
		usage = _merge_alternative_fulfillment_usage(
			_build_row_wise_serial_batch_usage(
				[
					frappe._dict(
						item_code="ALT-RM",
						original_item="RM",
						warehouse="Stores - TC",
						serial_no="ALT-SERIAL-1",
						qty=-1,
					)
				]
			),
			[
				frappe._dict(
					item_code="ALT-RM", original_item="RM",
					s_warehouse="Stores - TC", transfer_qty=1,
				)
			],
		)

		self.assertEqual(
			usage[("RM", "Stores - TC")].serial_nos,
			[],
		)
		self.assertEqual(
			usage[("RM", "Stores - TC")].alternative_fulfillment_qty,
			1,
		)

	def test_non_serial_alternative_still_fulfils_original_serial_reservation(self):
		usage = _merge_alternative_fulfillment_usage(
			{},
			[
				frappe._dict(
					item_code="ALT-NON-SERIAL", original_item="RM-SERIAL",
					s_warehouse="Stores - TC", transfer_qty=1,
				)
			],
		)

		self.assertEqual(
			usage[("RM-SERIAL", "Stores - TC")].alternative_fulfillment_qty,
			1,
		)
		self.assertEqual(usage[("RM-SERIAL", "Stores - TC")].serial_nos, [])

	def test_non_batch_alternative_uses_stock_uom_qty_for_original_batch(self):
		usage = _merge_alternative_fulfillment_usage(
			{},
			[
				frappe._dict(
					item_code="ALT-NON-BATCH", original_item="RM-BATCH",
					s_warehouse="Stores - TC", transfer_qty=3,
				)
			],
		)

		self.assertEqual(
			usage[("RM-BATCH", "Stores - TC")].alternative_fulfillment_qty,
			3,
		)
		self.assertEqual(usage[("RM-BATCH", "Stores - TC")].batch_nos, {})

	def test_usage_reads_non_serial_alternative_without_a_bundle(self):
		def get_all(doctype, **kwargs):
			if doctype == "Stock Entry":
				return ["STE-ALT"]
			if doctype == "Serial and Batch Bundle":
				return []
			if doctype == "Stock Entry Detail":
				return [
					frappe._dict(
						name="STE-ALT-ROW", item_code="ALT-NON-SERIAL",
						original_item="RM-SERIAL", s_warehouse="Stores - TC",
						transfer_qty=2,
					)
				]
			raise AssertionError(f"Unexpected doctype {doctype}")

		with patch(
			"process_simplification.production_reporting.stock_entry.frappe.get_all",
			side_effect=get_all,
		):
			usage = _get_work_order_serial_batch_usage("WO-1", "transfer")

		self.assertEqual(
			usage[("RM-SERIAL", "Stores - TC")].alternative_fulfillment_qty,
			2,
		)
		self.assertEqual(usage[("RM-SERIAL", "Stores - TC")].serial_nos, [])

	def test_alternative_serial_issue_releases_original_serial_reservation(self):
		child = frappe._dict(
			name="SBE-ORIGINAL", idx=1, serial_no="ORIGINAL-SERIAL", qty=1,
			delivered_qty=0,
		)
		child.db_update = MagicMock()
		used = _rebuild_serial_batch_child_usage(
			[
				(
					frappe._dict(
						name="SRE-ORIGINAL", item_code="RM",
						warehouse="Stores - TC",
					),
					frappe._dict(sb_entries=[child]),
				)
			],
			{
				("RM", "Stores - TC"): frappe._dict(
					serial_nos=[], batch_nos={}, alternative_fulfillment_qty=1,
				)
			},
		)

		self.assertEqual(used, {"SRE-ORIGINAL": 1})
		self.assertEqual(child.delivered_qty, 1)
		child.db_update.assert_called_once_with()

	def test_alternative_batch_issue_partially_releases_original_batch_reservation(self):
		child = frappe._dict(
			name="SBE-ORIGINAL", idx=1, batch_no="ORIGINAL-BATCH", qty=5,
			delivered_qty=0,
		)
		child.db_update = MagicMock()
		used = _rebuild_serial_batch_child_usage(
			[
				(
					frappe._dict(
						name="SRE-ORIGINAL", item_code="RM",
						warehouse="Stores - TC",
					),
					frappe._dict(sb_entries=[child]),
				)
			],
			{
				("RM", "Stores - TC"): frappe._dict(
					serial_nos=[], batch_nos={}, alternative_fulfillment_qty=3,
				)
			},
		)

		self.assertEqual(used, {"SRE-ORIGINAL": 3})
		self.assertEqual(child.delivered_qty, 3)
		child.db_update.assert_called_once_with()

	def test_cancelled_alternative_issue_restores_original_serial_reservation(self):
		child = frappe._dict(
			name="SBE-ORIGINAL", idx=1, serial_no="ORIGINAL-SERIAL", qty=1,
			delivered_qty=0,
		)
		child.db_update = MagicMock()
		entry_documents = [
			(
				frappe._dict(
					name="SRE-ORIGINAL", item_code="RM", warehouse="Stores - TC",
				),
				frappe._dict(sb_entries=[child]),
			)
		]

		_rebuild_serial_batch_child_usage(
			entry_documents,
			{
				("RM", "Stores - TC"): frappe._dict(
					serial_nos=[], batch_nos={}, alternative_fulfillment_qty=1,
				)
			},
		)
		self.assertEqual(child.delivered_qty, 1)

		used_after_cancel = _rebuild_serial_batch_child_usage(entry_documents, {})

		self.assertEqual(used_after_cancel, {"SRE-ORIGINAL": 0})
		self.assertEqual(child.delivered_qty, 0)
		self.assertEqual(child.db_update.call_count, 2)

	def test_submit_refresh_runs_after_native_stock_entry_controller(self):
		events = []

		class NativeStockEntry:
			def on_submit(self):
				events.append("native")
				return "submitted"

		class ExtendedStockEntry(SubassemblyReservationStockEntryMixin, NativeStockEntry):
			pass

		with (
			patch(
				"process_simplification.production_reporting.stock_entry._reconcile_work_order_reservations",
				side_effect=lambda doc: events.append("reservation"),
			),
			patch(
				"process_simplification.production_reporting.stock_entry._refresh_subassembly_bin",
				side_effect=lambda doc: events.append("refresh"),
			),
			patch(
				"process_simplification.production_workflow.service.reserve_replenishment_output",
				return_value=None,
			),
			patch(
				"process_simplification.production_exceptions.service.complete_linked_stock_entry",
				side_effect=lambda doc: events.append("exception"),
			),
			patch(
				"process_simplification.notifications.notify_production_stock_completed",
			),
		):
			result = ExtendedStockEntry().on_submit()

		self.assertEqual(result, "submitted")
		self.assertEqual(events, ["native", "reservation", "refresh", "exception"])

	def test_cancel_refresh_runs_after_native_stock_entry_controller(self):
		events = []

		class NativeStockEntry:
			def on_cancel(self):
				events.append("native")
				return "cancelled"

		class ExtendedStockEntry(SubassemblyReservationStockEntryMixin, NativeStockEntry):
			pass

		with (
			patch(
				"process_simplification.production_workflow.service.cancel_replenishment_output_reservations",
			),
			patch(
				"process_simplification.production_reporting.stock_entry._reconcile_work_order_reservations",
				side_effect=lambda doc: events.append("reservation"),
			),
			patch(
				"process_simplification.production_reporting.stock_entry._refresh_subassembly_bin",
				side_effect=lambda doc: events.append("refresh"),
			),
			patch(
				"process_simplification.production_exceptions.service.reopen_cancelled_stock_entry",
				side_effect=lambda doc: events.append("exception"),
			),
			patch(
				"process_simplification.production_workflow.service.release_issue_request_reservations",
			),
			patch(
				"process_simplification.notifications.notify_production_stock_cancelled",
			),
		):
			result = ExtendedStockEntry().on_cancel()

		self.assertEqual(result, "cancelled")
		self.assertEqual(events, ["native", "reservation", "refresh", "exception"])

	def test_operation_split_transfer_is_allocated_once_across_sres(self):
		updates = build_work_order_sre_reconciliation(
			frappe._dict(skip_transfer=0, wip_warehouse="WIP - TC"),
			[
				frappe._dict(
					name="WOI-1", item_code="RM-1", source_warehouse="Stores - TC",
					transferred_qty=6, consumed_qty=0,
				),
				frappe._dict(
					name="WOI-2", item_code="RM-1", source_warehouse="Stores - TC",
					transferred_qty=6, consumed_qty=0,
				),
			],
			[
				frappe._dict(
					name="SRE-1", voucher_detail_no="WOI-1", item_code="RM-1",
					warehouse="Stores - TC", reserved_qty=5, voucher_qty=5,
					transferred_qty=5, consumed_qty=0, delivered_qty=0,
					status="Closed", creation="2026-09-01 08:00:00",
				),
				frappe._dict(
					name="SRE-2", voucher_detail_no="WOI-2", item_code="RM-1",
					warehouse="Stores - TC", reserved_qty=5, voucher_qty=5,
					transferred_qty=5, consumed_qty=0, delivered_qty=0,
					status="Closed", from_voucher_type="Stock Entry",
					creation="2026-09-01 08:00:01",
				),
			],
		)
		by_name = {row.name: row for row in updates}

		self.assertEqual(sum(row.transferred_qty for row in updates), 6)
		self.assertEqual(by_name["SRE-1"].transferred_qty, 5)
		self.assertEqual(by_name["SRE-1"].status, "Closed")
		self.assertEqual(by_name["SRE-2"].transferred_qty, 1)
		self.assertEqual(by_name["SRE-2"].status, "Partially Used")

	def test_submitted_transfer_fact_does_not_close_same_item_in_another_source(self):
		updates = build_work_order_sre_reconciliation(
			frappe._dict(name="WO-1", skip_transfer=0, wip_warehouse="WIP - TC"),
			[
				frappe._dict(
					name="WOI-A", item_code="RM-1", source_warehouse="Stores-A - TC",
					transferred_qty=5, consumed_qty=0,
				),
				frappe._dict(
					name="WOI-B", item_code="RM-1", source_warehouse="Stores-B - TC",
					transferred_qty=5, consumed_qty=0,
				),
			],
			[
				frappe._dict(
					name="SRE-A", voucher_detail_no="WOI-A", item_code="RM-1",
					warehouse="Stores-A - TC", reserved_qty=5, voucher_qty=5,
					transferred_qty=5, consumed_qty=0, delivered_qty=0,
					status="Closed",
				),
				frappe._dict(
					name="SRE-B", voucher_detail_no="WOI-B", item_code="RM-1",
					warehouse="Stores-B - TC", reserved_qty=5, voucher_qty=5,
					transferred_qty=5, consumed_qty=0, delivered_qty=0,
					status="Closed",
				),
			],
			{
				("WO-1", "RM-1", "Stores-A - TC"): frappe._dict(
					gross_issued_qty=5,
				),
			},
		)
		by_name = {row.name: row for row in updates}

		self.assertEqual(by_name["SRE-A"].transferred_qty, 5)
		self.assertEqual(by_name["SRE-A"].status, "Closed")
		self.assertEqual(by_name["SRE-B"].transferred_qty, 0)
		self.assertEqual(by_name["SRE-B"].status, "Reserved")

	def test_cancelled_full_transfer_reopens_single_closed_sre(self):
		updates = build_work_order_sre_reconciliation(
			frappe._dict(skip_transfer=0, wip_warehouse="WIP - TC"),
			[
				frappe._dict(
					name="WOI-1", item_code="RM-1", source_warehouse="Stores - TC",
					transferred_qty=0, consumed_qty=0,
				),
			],
			[
				frappe._dict(
					name="SRE-1", voucher_detail_no="WOI-1", item_code="RM-1",
					warehouse="Stores - TC", reserved_qty=5, voucher_qty=5,
					transferred_qty=5, consumed_qty=0, delivered_qty=0, status="Closed",
				),
			],
		)

		self.assertEqual(sum(row.transferred_qty for row in updates), 0)
		self.assertEqual({row.status for row in updates}, {"Reserved"})

	def test_skip_transfer_consumption_is_allocated_once_across_sres(self):
		updates = build_work_order_sre_reconciliation(
			frappe._dict(
				skip_transfer=1, from_wip_warehouse=1, wip_warehouse="WIP - TC",
			),
			[
				frappe._dict(
					name="WOI-1", item_code="RM-1", source_warehouse="Stores - TC",
					transferred_qty=0, consumed_qty=6,
				),
				frappe._dict(
					name="WOI-2", item_code="RM-1", source_warehouse="Stores - TC",
					transferred_qty=0, consumed_qty=6,
				),
			],
			[
				frappe._dict(
					name="SRE-1", voucher_detail_no="WOI-1", item_code="RM-1",
					warehouse="WIP - TC", reserved_qty=5, voucher_qty=5,
					transferred_qty=0, consumed_qty=5, delivered_qty=0,
					status="Delivered", creation="2026-09-01 08:00:00",
				),
				frappe._dict(
					name="SRE-2", voucher_detail_no="WOI-2", item_code="RM-1",
					warehouse="WIP - TC", reserved_qty=5, voucher_qty=5,
					transferred_qty=0, consumed_qty=5, delivered_qty=0,
					status="Delivered", creation="2026-09-01 08:00:01",
				),
			],
		)
		by_name = {row.name: row for row in updates}

		self.assertEqual(sum(row.consumed_qty for row in updates), 6)
		self.assertEqual(by_name["SRE-1"].consumed_qty, 5)
		self.assertEqual(by_name["SRE-1"].status, "Delivered")
		self.assertEqual(by_name["SRE-2"].consumed_qty, 1)
		self.assertEqual(by_name["SRE-2"].status, "Reserved")

	def test_serial_batch_header_uses_rebuilt_child_usage_once(self):
		updates = build_work_order_sre_reconciliation(
			frappe._dict(skip_transfer=0, wip_warehouse="WIP - TC"),
			[
				frappe._dict(
					name="WOI-1", item_code="RM-SERIAL", source_warehouse="Stores - TC",
					transferred_qty=6, consumed_qty=0,
				),
				frappe._dict(
					name="WOI-2", item_code="RM-SERIAL", source_warehouse="Stores - TC",
					transferred_qty=6, consumed_qty=0,
				),
			],
			[
				frappe._dict(
					name="SRE-1", voucher_detail_no="WOI-1", item_code="RM-SERIAL",
					warehouse="Stores - TC", reserved_qty=5, voucher_qty=5,
					transferred_qty=5, consumed_qty=0, delivered_qty=0,
					reservation_based_on="Serial and Batch", serial_batch_used_qty=5,
				),
				frappe._dict(
					name="SRE-2", voucher_detail_no="WOI-2", item_code="RM-SERIAL",
					warehouse="Stores - TC", reserved_qty=5, voucher_qty=5,
					transferred_qty=5, consumed_qty=0, delivered_qty=0,
					reservation_based_on="Serial and Batch", serial_batch_used_qty=1,
				),
			],
		)

		self.assertEqual(
			{row.name: row.transferred_qty for row in updates},
			{"SRE-1": 5, "SRE-2": 1},
		)

	def test_serial_batch_children_are_rebuilt_from_current_submitted_entries(self):
		class Reservation:
			def __init__(self):
				child = frappe._dict(delivered_qty=5, idx=1, name="SBE-1")
				child.db_update = MagicMock()
				self.sb_entries = [child]

			def get(self, fieldname):
				return getattr(self, fieldname, None)

		doc = Reservation()
		entries = [
			frappe._dict(
				name="SRE-SERIAL", voucher_detail_no="WOI-1", item_code="RM-SERIAL",
				warehouse="Stores - TC", reservation_based_on="Serial and Batch",
			)
		]
		with (
			patch(
				"process_simplification.production_reporting.stock_entry._get_work_order_serial_batch_usage",
				return_value={},
			) as get_usage,
			patch(
				"process_simplification.production_reporting.stock_entry.frappe.get_doc",
				return_value=doc,
			),
		):
			documents = _refresh_serial_batch_reservation_usage(
				frappe._dict(name="WO-1", skip_transfer=0, wip_warehouse="WIP - TC"),
				[
					frappe._dict(
						name="WOI-1", item_code="RM-SERIAL",
						source_warehouse="Stores - TC",
					)
				],
				entries,
			)

		get_usage.assert_called_once_with("WO-1", "transfer")
		self.assertEqual(entries[0].serial_batch_used_qty, 0)
		self.assertEqual(doc.sb_entries[0].delivered_qty, 0)
		doc.sb_entries[0].db_update.assert_called_once_with()
		self.assertIs(documents["SRE-SERIAL"], doc)

	def test_consumption_serial_batch_usage_includes_returns_and_all_outward_purposes(self):
		stock_entry_filters = []
		bundle_query = {}
		detail_query = {}

		def get_all(doctype, **kwargs):
			if doctype == "Stock Entry":
				filters = kwargs["filters"]
				stock_entry_filters.append(filters)
				if isinstance(filters["purpose"], list):
					return ["STE-MANUFACTURE", "STE-CONSUMPTION"]
				return ["STE-RETURN"]
			if doctype == "Stock Entry Detail":
				detail_query.update(kwargs)
				return []
			bundle_query.update(kwargs)
			return [
				frappe._dict(
					item_code="RM-SERIAL", warehouse="WIP - TC",
					serial_no="SERIAL-1", batch_no=None, qty=-1,
				),
				frappe._dict(
					item_code="RM-BATCH", warehouse="WIP - TC",
					serial_no=None, batch_no="BATCH-1", qty=-2,
				),
			]

		with patch(
			"process_simplification.production_reporting.stock_entry.frappe.get_all",
			side_effect=get_all,
		):
			usage = _get_work_order_serial_batch_usage("WO-1", "consumption")

		self.assertEqual(len(stock_entry_filters), 2)
		self.assertEqual(
			set(stock_entry_filters[0]["purpose"][1]),
			{"Manufacture", "Material Consumption for Manufacture"},
		)
		self.assertEqual(stock_entry_filters[0]["docstatus"], 1)
		self.assertEqual(
			stock_entry_filters[1],
			{
				"work_order": "WO-1",
				"purpose": "Material Transfer for Manufacture",
				"is_return": 1,
				"docstatus": 1,
			},
		)
		self.assertIn(
			[
				"Serial and Batch Bundle", "voucher_no", "in",
				["STE-CONSUMPTION", "STE-MANUFACTURE", "STE-RETURN"],
			],
			bundle_query["filters"],
		)
		self.assertIn(
			["Serial and Batch Bundle", "is_cancelled", "=", 0],
			bundle_query["filters"],
		)
		self.assertIn(
			["Serial and Batch Entry", "qty", "<", 0],
			bundle_query["filters"],
		)
		self.assertEqual(
			detail_query["filters"],
			{
				"parent": [
					"in",
					["STE-CONSUMPTION", "STE-MANUFACTURE", "STE-RETURN"],
				]
			},
		)
		self.assertEqual(
			usage[("RM-SERIAL", "WIP - TC")].serial_nos,
			["SERIAL-1"],
		)
		self.assertEqual(
			usage[("RM-BATCH", "WIP - TC")].batch_nos["BATCH-1"],
			2,
		)

	def test_wip_serial_batch_children_use_combined_consumption_usage(self):
		class Reservation:
			def __init__(self):
				child = frappe._dict(
					serial_no="SERIAL-RETURN", delivered_qty=0, idx=1, name="SBE-1",
				)
				child.db_update = MagicMock()
				self.sb_entries = [child]

			def get(self, fieldname):
				return getattr(self, fieldname, None)

		doc = Reservation()
		entry = frappe._dict(
			name="SRE-WIP", voucher_detail_no="WOI-1", item_code="RM-SERIAL",
			warehouse="WIP - TC", from_voucher_type="Stock Entry",
			reservation_based_on="Serial and Batch",
		)
		with (
			patch(
				"process_simplification.production_reporting.stock_entry.frappe.get_doc",
				return_value=doc,
			),
			patch(
				"process_simplification.production_reporting.stock_entry._get_work_order_serial_batch_usage",
				return_value={
					("RM-SERIAL", "WIP - TC"): frappe._dict(
						serial_nos=["SERIAL-RETURN"], batch_nos={},
					)
				},
			) as get_usage,
		):
			_refresh_serial_batch_reservation_usage(
				frappe._dict(name="WO-1", skip_transfer=0, wip_warehouse="WIP - TC"),
				[
					frappe._dict(
						name="WOI-1", item_code="RM-SERIAL",
						source_warehouse="Stores - TC",
					)
				],
				[entry],
			)

		get_usage.assert_called_once_with("WO-1", "consumption")
		self.assertEqual(entry.serial_batch_used_qty, 1)
		self.assertEqual(doc.sb_entries[0].delivered_qty, 1)
		doc.sb_entries[0].db_update.assert_called_once_with()

	def test_serial_batch_reservation_without_children_falls_back_to_qty(self):
		class Reservation:
			def __init__(self):
				self.sb_entries = []

			def get(self, fieldname):
				return getattr(self, fieldname, None)

		doc = Reservation()
		entry = frappe._dict(
			name="SRE-SERIAL", voucher_detail_no="WOI-1", item_code="RM-SERIAL",
			warehouse="Stores - TC", reservation_based_on="Serial and Batch",
			reserved_qty=5, voucher_qty=5, delivered_qty=0,
			transferred_qty=0, consumed_qty=0,
		)
		with (
			patch(
				"process_simplification.production_reporting.stock_entry.frappe.get_doc",
				return_value=doc,
			),
			patch(
				"process_simplification.production_reporting.stock_entry._get_work_order_serial_batch_usage",
			) as get_usage,
		):
			documents = _refresh_serial_batch_reservation_usage(
				frappe._dict(name="WO-1", skip_transfer=0, wip_warehouse="WIP - TC"),
				[
					frappe._dict(
						name="WOI-1", item_code="RM-SERIAL",
						source_warehouse="Stores - TC",
					)
				],
				[entry],
			)

		get_usage.assert_not_called()
		self.assertNotIn("serial_batch_used_qty", entry)
		self.assertIs(documents["SRE-SERIAL"], doc)
		updates = build_work_order_sre_reconciliation(
			frappe._dict(skip_transfer=0, wip_warehouse="WIP - TC"),
			[
				frappe._dict(
					name="WOI-1", item_code="RM-SERIAL",
					source_warehouse="Stores - TC", transferred_qty=3,
					consumed_qty=0,
				)
			],
			[entry],
		)

		self.assertEqual(updates[0].transferred_qty, 3)

	def test_batch_usage_is_distributed_once_across_sre_children(self):
		def reservation(child_name):
			child = frappe._dict(
				name=child_name, idx=1, batch_no="BATCH-1", qty=5, delivered_qty=5,
			)
			child.db_update = MagicMock()
			return frappe._dict(sb_entries=[child])

		first = reservation("SBE-1")
		second = reservation("SBE-2")
		used = _rebuild_serial_batch_child_usage(
			[
				(
					frappe._dict(
						name="SRE-1", item_code="RM-BATCH", warehouse="Stores - TC",
						creation="2026-09-01 08:00:00",
					),
					first,
				),
				(
					frappe._dict(
						name="SRE-2", item_code="RM-BATCH", warehouse="Stores - TC",
						creation="2026-09-01 08:00:01",
					),
					second,
				),
			],
			{
				("RM-BATCH", "Stores - TC"): frappe._dict(
					serial_nos=[], batch_nos={"BATCH-1": 6},
				)
			},
		)

		self.assertEqual(used, {"SRE-1": 5, "SRE-2": 1})
		self.assertEqual(first.sb_entries[0].delivered_qty, 5)
		self.assertEqual(second.sb_entries[0].delivered_qty, 1)

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


class TestStockEntrySREIntegration(IntegrationTestCase):
	def test_operation_split_same_item_transfer_reconciles_submit_and_cancel(self):
		from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import (
			make_stock_entry as make_stock_entry_test_record,
		)
		from frappe.utils import now

		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", 1)
		frappe.db.set_single_value("Stock Settings", "allow_partial_reservation", 0)
		frappe.db.set_single_value("Stock Settings", "auto_reserve_serial_and_batch", 0)

		suffix = frappe.generate_hash(length=8)
		production_item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"PS-SRE-FG-{suffix}",
				"item_name": f"PS SRE FG {suffix}",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"is_manufactured_item": 1,
				"is_purchase_item": 0,
			}
		)
		production_item.insert()
		raw_material = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"PS-SRE-RM-{suffix}",
				"item_name": f"PS SRE RM {suffix}",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"is_purchase_item": 1,
				"valuation_rate": 10,
			}
		)
		raw_material.insert()
		workstation = frappe.get_doc(
			{
				"doctype": "Workstation",
				"workstation_name": f"PS SRE WS {suffix}",
				"production_capacity": 1,
			}
		)
		workstation.insert()
		operations = []
		for idx in (1, 2):
			operation = frappe.get_doc(
				{
					"doctype": "Operation",
					"name": f"PS SRE Operation {idx} {suffix}",
					"workstation": workstation.name,
				}
			)
			operation.insert()
			operations.append(operation)

		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": production_item.name,
				"company": "_Test Company",
				"currency": "USD",
				"quantity": 1,
				"is_active": 1,
				"is_default": 1,
				"with_operations": 1,
			}
		)
		for operation in operations:
			bom.append(
				"operations",
				{
					"operation": operation.name,
					"workstation": workstation.name,
					"time_in_mins": 10,
				},
			)
			bom.append(
				"items",
				{
					"item_code": raw_material.name,
					"qty": 1,
					"uom": "Nos",
					"stock_uom": "Nos",
					"rate": 10,
					"source_warehouse": "Stores - _TC",
					"operation": operation.name,
				},
			)
		bom.insert()
		bom.submit()

		make_stock_entry_test_record(
			item_code=raw_material.name,
			to_warehouse="Stores - _TC",
			qty=10,
			rate=10,
		)
		work_order = frappe.get_doc(
			{
				"doctype": "Work Order",
				"production_item": production_item.name,
				"bom_no": bom.name,
				"qty": 5,
				"company": "_Test Company",
				"stock_uom": "Nos",
				"reserve_stock": 1,
				"source_warehouse": "Stores - _TC",
				"wip_warehouse": "_Test Warehouse - _TC",
				"fg_warehouse": "_Test Warehouse 1 - _TC",
				"planned_start_date": now(),
			}
		)
		work_order.get_items_and_operations_from_bom()
		for row in work_order.required_items:
			row.source_warehouse = "Stores - _TC"
		work_order.insert()
		work_order.submit()

		self.assertEqual(len(work_order.required_items), 2)
		self.assertEqual(
			{row.operation for row in work_order.required_items},
			{operation.name for operation in operations},
		)
		reservations = frappe.get_all(
			"Stock Reservation Entry",
			filters={
				"voucher_type": "Work Order",
				"voucher_no": work_order.name,
				"item_code": raw_material.name,
				"warehouse": "Stores - _TC",
				"docstatus": 1,
			},
			fields=["name", "reserved_qty", "transferred_qty", "status"],
			order_by="creation, name",
		)
		self.assertEqual(len(reservations), 2)
		self.assertEqual([row.reserved_qty for row in reservations], [5, 5])

		transfer = frappe.get_doc(
			make_stock_entry(
				work_order.name,
				"Material Transfer for Manufacture",
				5,
			)
		)
		self.assertEqual(len(transfer.items), 1)
		self.assertEqual(transfer.items[0].qty, 5)
		transfer.submit()

		reservations = frappe.get_all(
			"Stock Reservation Entry",
			filters={"name": ["in", [row.name for row in reservations]]},
			fields=["name", "transferred_qty", "status"],
			order_by="creation, name",
		)
		self.assertEqual(sum(row.transferred_qty for row in reservations), 5)
		self.assertEqual(
			sorted(row.transferred_qty for row in reservations),
			[0, 5],
		)
		self.assertEqual(sorted(row.status for row in reservations), ["Closed", "Reserved"])

		transfer.cancel()
		reservations = frappe.get_all(
			"Stock Reservation Entry",
			filters={"name": ["in", [row.name for row in reservations]]},
			fields=["transferred_qty", "status"],
		)
		self.assertEqual([row.transferred_qty for row in reservations], [0, 0])
		self.assertEqual([row.status for row in reservations], ["Reserved", "Reserved"])
