from __future__ import annotations

import logging
import unittest
from unittest.mock import MagicMock, patch

import frappe

from process_simplification import batch_compat as batch


def reservation(name, qty, *, batches=None, mode="Serial and Batch", **kwargs):
	return frappe._dict(name=name, reserved_qty=qty, reservation_based_on=mode,
		sb_entries=[frappe._dict(batch_no=key, qty=value) for key, value in (batches or {}).items()], **kwargs)


class TestBatchStockFacts(unittest.TestCase):
	def facts(self, *, actual=100, eligible=None, available=None, reservations=None):
		return batch.calculate_stock_facts(actual_qty=actual, eligible_batches=eligible or {},
			available_batches=available or {}, reservations=reservations or [])


	def test_mixed_quantity_and_batch_promises_are_deducted_once(self):
		facts = self.facts(eligible={"GOOD": 60}, available={"GOOD": 40}, reservations=[
			reservation("BOUND", 20, batches={"GOOD": 20}), reservation("QTY", 20, mode="Qty"),
		])
		self.assertEqual(facts.available_qty, 20)
		self.assertEqual(facts.unbound_reserved_qty, 20)
		self.assertEqual(facts.effective_reservation_qty, {"BOUND": 20, "QTY": 20})

	def test_empty_work_order_batch_children_still_consume_quantity(self):
		facts = self.facts(eligible={"A": 40}, available={"A": 40}, reservations=[reservation("WO", 15)])
		self.assertEqual(facts.available_qty, 25)
		self.assertEqual(facts.effective_reservation_qty["WO"], 15)

	def test_partially_bound_reservation_only_deducts_unbound_remainder(self):
		facts = self.facts(eligible={"A": 60}, available={"A": 40},
			reservations=[reservation("PART", 35, batches={"A": 20})])
		self.assertEqual(facts.available_qty, 25)
		self.assertEqual(facts.effective_reservation_qty["PART"], 35)

	def test_expired_bound_reservation_does_not_become_an_unbound_promise(self):
		facts = self.facts(eligible={"GOOD": 60}, available={"GOOD": 60},
			reservations=[reservation("EXPIRED", 10, batches={"BAD": 10})])
		self.assertEqual(facts.unbound_reserved_qty, 0)
		self.assertEqual(facts.available_qty, 60)
		self.assertEqual(facts.effective_reservation_qty["EXPIRED"], 0)

	def test_expired_part_of_own_reservation_cannot_be_added_back(self):
		facts = self.facts(eligible={"GOOD": 10}, available={}, reservations=[
			reservation("OWN", 20, batches={"GOOD": 10, "BAD": 10}),
		])
		self.assertEqual(facts.effective_reservation_qty["OWN"], 10)

	def test_batch_identity_precedes_older_unbound_promise(self):
		facts = self.facts(eligible={"A": 20}, available={"A": 5}, reservations=[
			reservation("FIRST", 10, mode="Qty", creation="2026-01-01"),
			reservation("LATER", 15, batches={"A": 15}, creation="2026-01-02"),
		])
		self.assertEqual(facts.effective_reservation_qty, {"FIRST": 5, "LATER": 15})
		self.assertEqual(facts.available_qty, 0)

	def test_child_used_qty_is_not_subtracted_again_from_head(self):
		row = reservation("WO", 30, batches={"A": 30}, transferred_qty=12)
		row.sb_entries[0].delivered_qty = 12
		facts = self.facts(eligible={"A": 40}, available={"A": 22}, reservations=[row])
		self.assertEqual(facts.unbound_reserved_qty, 0)
		self.assertEqual(facts.effective_reservation_qty["WO"], 18)
		self.assertEqual(facts.available_qty, 22)

	def test_pos_deductions_from_native_pool_are_preserved(self):
		facts = self.facts(eligible={"A": 45}, available={"A": 45},
			reservations=[reservation("QTY", 10, mode="Qty")])
		self.assertEqual(facts.available_qty, 35)

	def test_aggregate_physical_limit_is_also_respected(self):
		facts = self.facts(actual=12, eligible={"A": 40}, available={"A": 40},
			reservations=[reservation("QTY", 10, mode="Qty")])
		self.assertEqual(facts.available_qty, 2)

	def test_quantity_mode_ignores_stale_batch_children_like_native_query(self):
		facts = self.facts(eligible={"A": 30}, available={"A": 30},
			reservations=[reservation("QTY", 10, mode="Qty", batches={"A": 10})])
		self.assertEqual(facts.available_qty, 20)

	def test_non_batch_availability_keeps_native_path(self):
		with patch.object(batch, "is_batch_item", return_value=False), \
			patch.object(batch, "_native_available_qty", return_value=7) as native, \
			patch.object(batch, "_native_batch_pool") as pool:
			self.assertEqual(batch.get_available_qty("PLAIN", "WH", ignore_sre="SELF"), 7)
			native.assert_called_once_with("PLAIN", "WH", "SELF")
			pool.assert_not_called()

	def test_native_scalar_ignore_sre_contract_is_preserved(self):
		with patch.object(frappe, "logger", return_value=logging.getLogger("batch-contract-test")), \
			patch("erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry.get_available_qty_to_reserve", return_value=19) as native:
			self.assertEqual(batch._native_available_qty("PLAIN", "WH", "SELF"), 19)
			native.assert_called_once_with("PLAIN", "WH", ignore_sre="SELF")

	def test_native_batch_query_receives_ignored_sre_as_voucher_list(self):
		with patch("erpnext.stock.doctype.batch.batch.get_batch_qty", return_value=[
			frappe._dict(batch_no="A", warehouse="WH", qty=5),
			frappe._dict(batch_no="A", warehouse="OTHER", qty=99),
		]) as native:
			self.assertEqual(batch._native_batch_pool("ITEM", "WH", ignore_sre="SELF"), {"A": 5})
			self.assertEqual(native.call_args.kwargs["ignore_voucher_nos"], ["SELF"])
			self.assertFalse(native.call_args.kwargs["ignore_reserved_stock"])

	def test_ignored_sre_is_excluded_from_unbound_reservation_query(self):
		with patch.object(batch.frappe, "get_all", return_value=[]) as query:
			self.assertEqual(batch._load_reservations("ITEM", "WH", "SELF"), [])
			self.assertEqual(query.call_args.kwargs["filters"]["name"], ["not in", ["SELF"]])

	def test_ignoring_own_quantity_reservation_does_not_add_back_bound_stock_twice(self):
		bound = reservation("BOUND", 20, batches={"A": 20})
		own = reservation("SELF", 20, mode="Qty")
		with patch.object(batch, "is_batch_item", return_value=True), \
			patch.object(batch.frappe, "db", MagicMock(get_value=MagicMock(return_value=100))), \
			patch.object(batch, "_native_batch_pool", side_effect=lambda *args, **kwargs: {"A": 60 if kwargs.get("ignore_reserved_stock") else 40}), \
			patch.object(batch, "_load_reservations", side_effect=lambda item, warehouse, ignored: [bound] if ignored else [bound, own]):
			self.assertEqual(batch.get_available_qty("ITEM", "WH"), 20)
			self.assertEqual(batch.get_available_qty("ITEM", "WH", ignore_sre="SELF"), 40)

	def test_ignoring_own_bound_reservation_only_restores_its_batch_balance(self):
		bound = reservation("SELF", 20, batches={"A": 20})
		loose = reservation("QTY", 20, mode="Qty")
		with patch.object(batch, "is_batch_item", return_value=True), \
			patch.object(batch.frappe, "db", MagicMock(get_value=MagicMock(return_value=100))), \
			patch.object(batch, "_native_batch_pool", side_effect=lambda *args, **kwargs: {"A": 60 if kwargs.get("ignore_reserved_stock") or kwargs.get("ignore_sre") else 40}), \
			patch.object(batch, "_load_reservations", side_effect=lambda item, warehouse, ignored: [loose] if ignored else [bound, loose]):
			self.assertEqual(batch.get_available_qty("ITEM", "WH"), 20)
			self.assertEqual(batch.get_available_qty("ITEM", "WH", ignore_sre="SELF"), 40)

	def test_posting_date_and_ignored_reservation_reach_both_native_queries(self):
		with patch.object(batch, "is_batch_item", return_value=True), \
			patch.object(batch.frappe, "db", MagicMock(get_value=MagicMock(return_value=20))), \
			patch.object(batch, "_native_batch_pool", return_value={"A": 20}) as pool, \
			patch.object(batch, "_load_reservations", return_value=[]):
			batch.get_stock_snapshot("ITEM", "WH", ignore_sre="SELF", posting_date="2026-09-14", posting_time="10:00:00")
			self.assertEqual(pool.call_count, 2)
			for call in pool.call_args_list:
				self.assertEqual(call.kwargs["posting_date"], "2026-09-14")
				self.assertEqual(call.kwargs["posting_time"], "10:00:00")
				self.assertEqual(call.kwargs["ignore_sre"], "SELF")


class TestSourceBatchAllocation(unittest.TestCase):
	def rows(self, **quantities):
		return [frappe._dict(batch_no=name, qty=qty, warehouse="WH") for name, qty in quantities.items()]

	def allocate(self, rows, qty, available, **kwargs):
		return batch.allocate_source_batches(rows, qty, available_by_batch=available,
			available_qty=kwargs.pop("available_qty", sum(available.values())), **kwargs)

	def test_requested_quantity_clips_a_multi_batch_receipt(self):
		rows = self.allocate(self.rows(A=6, B=4), 8, {"A": 6, "B": 4})
		self.assertEqual([(row.batch_no, row.qty) for row in rows], [("A", 6), ("B", 2)])

	def test_current_stock_of_another_batch_cannot_replace_the_receipt(self):
		self.assertEqual(self.allocate(self.rows(A=10), 10, {"B": 100}), [])

	def test_historical_claim_including_used_qty_cannot_be_claimed_again(self):
		rows = self.allocate(self.rows(A=10), 10, {"A": 100}, claimed_by_batch={"A": 7})
		self.assertEqual(rows[0].qty, 3)

	def test_old_claim_of_unknown_batch_still_caps_source_total(self):
		rows = self.allocate(self.rows(A=10), 10, {"A": 100}, claimed_by_batch={"OLD": 7})
		self.assertEqual(rows[0].qty, 3)

	def test_old_unbound_source_claim_is_capped_before_new_allocations(self):
		rows = self.allocate(self.rows(A=6, B=4), 10, {"A": 6, "B": 4}, unbound_claimed_qty=8)
		self.assertEqual([(row.batch_no, row.qty) for row in rows], [("B", 2)])

	def test_quantity_reservations_cap_source_allocation_too(self):
		rows = self.allocate(self.rows(A=30), 30, {"A": 30}, available_qty=5)
		self.assertEqual(rows[0].qty, 5)

	def test_fractional_quantities_are_preserved(self):
		rows = self.allocate(self.rows(A=0.7, B=0.8), 0.9, {"A": 0.7, "B": 0.8}, claimed_by_batch={"A": 0.2})
		self.assertAlmostEqual(sum(row.qty for row in rows), 0.9)
		self.assertAlmostEqual(rows[0].qty, 0.5)

	def test_shared_batch_budget_prevents_two_source_previews_counting_same_stock(self):
		row = frappe._dict(name="SED-1", parent="SE", item_code="ITEM", t_warehouse="WH")
		second_row = frappe._dict(row, name="SED-2")
		facts = frappe._dict(has_batch_no=True, batch_available_qty={"A": 10}, available_qty=10)
		with patch.object(batch, "_source_row", side_effect=[row, second_row]), \
			patch.object(batch, "_source_batches", return_value=self.rows(A=10)), \
			patch.object(batch, "get_stock_snapshot", return_value=facts), \
			patch.object(batch, "_load_source_claims", return_value={}):
			budget = {}
			first = batch.source_batch_allocation("FIRST", 7, batch_budget=budget)
			second = batch.source_batch_allocation("SECOND", 7, batch_budget=budget)
			self.assertEqual(first[0].qty, 7)
			self.assertEqual(second[0].qty, 3)

	def test_repeated_source_preview_cannot_claim_receipt_twice(self):
		row = frappe._dict(name="SED", parent="SE", item_code="ITEM", t_warehouse="WH")
		facts = frappe._dict(has_batch_no=True, batch_available_qty={"A": 100}, available_qty=100)
		with patch.object(batch, "_source_row", return_value=row), \
			patch.object(batch, "_source_batches", return_value=self.rows(A=10)), \
			patch.object(batch, "get_stock_snapshot", return_value=facts), \
			patch.object(batch, "_load_source_claims", return_value={}):
			budget = {}
			first = batch.source_batch_allocation("SAME", 7, batch_budget=budget)
			second = batch.source_batch_allocation("SAME", 7, batch_budget=budget)
			self.assertEqual(first[0].qty, 7)
			self.assertEqual(second[0].qty, 3)

	def test_shared_aggregate_budget_also_covers_distinct_batches(self):
		row = frappe._dict(name="SED", parent="SE", item_code="ITEM", t_warehouse="WH")
		facts = frappe._dict(has_batch_no=True, batch_available_qty={"A": 10, "B": 10}, available_qty=12)
		with patch.object(batch, "_source_row", return_value=row), \
			patch.object(batch, "_source_batches", side_effect=[self.rows(A=10), self.rows(B=10)]), \
			patch.object(batch, "get_stock_snapshot", return_value=facts), \
			patch.object(batch, "_load_source_claims", return_value={}):
			budget = {}
			first = batch.source_batch_allocation("FIRST", 10, batch_budget=budget)
			second = batch.source_batch_allocation("SECOND", 10, batch_budget=budget)
			self.assertEqual(first[0].qty + second[0].qty, 12)

	def test_legacy_batch_field_uses_stock_quantity(self):
		row = frappe._dict(batch_no="LEGACY", transfer_qty=2.5, t_warehouse="WH")
		self.assertEqual(batch._source_batches(row), self.rows(LEGACY=2.5))

	def test_cancelled_source_is_rejected(self):
		with patch.object(batch.frappe, "db", MagicMock(get_value=MagicMock(return_value=frappe._dict(docstatus=2)))), \
			patch.object(batch.frappe, "throw", side_effect=ValueError) as reject:
			with self.assertRaises(ValueError):
				batch._source_row("CANCELLED")
			reject.assert_called_once()

	def test_bundle_source_mismatch_is_rejected(self):
		row = frappe._dict(name="SED", parent="SE", item_code="ITEM", t_warehouse="WH", transfer_qty=1, serial_and_batch_bundle="BUNDLE")
		bundle = frappe._dict(item_code="OTHER", type_of_transaction="Inward")
		with patch.object(batch.frappe, "get_doc", return_value=bundle), \
			patch.object(batch.frappe, "throw", side_effect=ValueError):
			with self.assertRaises(ValueError):
				batch._source_batches(row)

	def test_source_claims_use_gross_quantity_instead_of_unused_remainder(self):
		row = frappe._dict(parent="SE", name="SED", item_code="ITEM")
		with patch.object(batch.frappe, "get_all", side_effect=[
			[frappe._dict(name="CLAIM", reserved_qty=8, reservation_based_on="Serial and Batch")],
			[frappe._dict(parent="CLAIM", batch_no="A", qty=8, delivered_qty=7)],
		]):
			claims = batch._load_source_claims(row)
		self.assertEqual(claims.by_batch, {"A": 8})
		self.assertEqual(claims.unbound_qty, 0)
