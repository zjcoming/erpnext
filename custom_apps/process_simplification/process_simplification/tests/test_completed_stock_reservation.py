"""Receipt lineage regression using native reservations, deliveries and cancellations."""

from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate

from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from process_simplification.api import actions
from process_simplification.tests.test_batch_sales import BatchSalesFixture


class TestCompletedStockReservation(BatchSalesFixture):
	def _receive_plain(self, item, qty):
		return make_stock_entry(
			item_code=item.name,
			company=self.company,
			to_warehouse=self.stores,
			qty=qty,
			rate=10,
			posting_date=add_days(nowdate(), -1),
			posting_time="12:00:00",
		)

	def _reserve_completed(self, order, receipts, qty):
		# Supply the manufacturing selection only; source claims, stock balances,
		# SO-row locking and all submitted documents use the real database.
		with (
			patch.object(
				actions, "_row_from_workbench", return_value=frappe._dict(completed_unreserved_qty=qty)
			),
			patch.object(
				actions, "_manufactured_finished_rows", return_value=[r.items[0] for r in receipts]
			),
		):
			result = actions.reserve_completed_stock(order.name, order.items[0].name, qty=qty)
		return [frappe.get_doc("Stock Reservation Entry", name) for name in result["stock_reservation_entries"]]

	def _deliver_reserved(self, order, qty=None):
		delivery = actions.make_delivery_note(
			order.name,
			kwargs={"for_reserved_stock": True, "filtered_children": [order.items[0].name]},
		)
		if qty is not None:
			self.assertEqual(len(delivery.items), 1)
			delivery.items[0].qty = qty
		return delivery.insert().submit()

	def test_second_receipt_does_not_reuse_already_delivered_source(self):
		item = self._item("SPLIT-DELIVERY", batch=False)
		order = self._order(item, 6)
		first = self._receive_plain(item, 2)
		first_reservation = self._reserve_completed(order, [first], 2)[0]
		self._deliver_reserved(order)
		self.assertEqual(first_reservation.reload().delivered_qty, 2)

		second = self._receive_plain(item, 4)
		reservations = self._reserve_completed(order, [first, second], 4)
		self.assertEqual(
			[(r.from_voucher_detail_no, r.reserved_qty) for r in reservations],
			[(second.items[0].name, 4)],
		)
		delivery = self._deliver_reserved(order)
		self.assertEqual([row.qty for row in delivery.items], [4])
		self.assertEqual(order.reload().items[0].delivered_qty, 6)

	def test_active_and_partially_delivered_reservations_still_claim_their_source(self):
		item = self._item("PARTIAL-DELIVERY", batch=False)
		order = self._order(item, 6)
		first = self._receive_plain(item, 4)
		reservation = self._reserve_completed(order, [first], 4)[0]
		second = self._receive_plain(item, 2)
		reservations = self._reserve_completed(order, [first, second], 2)
		self.assertEqual(
			[(r.from_voucher_detail_no, r.reserved_qty) for r in reservations],
			[(second.items[0].name, 2)],
		)
		reservations[0].cancel()

		self._deliver_reserved(order, qty=2)
		self.assertEqual(reservation.reload().delivered_qty, 2)
		reservations = self._reserve_completed(order, [first, second], 2)
		self.assertEqual(
			[(r.from_voucher_detail_no, r.reserved_qty) for r in reservations],
			[(second.items[0].name, 2)],
		)

	def test_cancelled_reservation_releases_only_its_claimed_source_quantity(self):
		item = self._item("CANCELLED-CLAIM", batch=False)
		order = self._order(item, 6)
		first = self._receive_plain(item, 2)
		reservation = self._reserve_completed(order, [first], 2)[0]
		second = self._receive_plain(item, 4)
		reservation.cancel()

		reservations = self._reserve_completed(order, [first, second], 6)
		self.assertEqual(
			[(r.from_voucher_detail_no, r.reserved_qty) for r in reservations],
			[(first.items[0].name, 2), (second.items[0].name, 4)],
		)
