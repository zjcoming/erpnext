"""Real batch ledger contracts for the guided sales entry and native delivery."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import add_days, flt, nowdate

from process_simplification.api import actions, quick_order, workbench
from process_simplification.batch_compat import get_available_qty, get_stock_snapshot, source_batch_allocation


class TestBatchSalesUnit(UnitTestCase):
	def test_sales_reservation_keeps_native_quantity_mode_without_source_batches(self):
		reservation = MagicMock()
		with (
			patch.object(actions, "get_item_uom_details", return_value=frappe._dict(stock_uom="Nos", has_batch_no=1, has_serial_no=0)),
			patch.object(actions, "get_available_qty_to_reserve", return_value=10),
			patch.object(frappe, "new_doc", return_value=reservation),
		):
			actions._new_sre(sales_order="SO", sales_order_item="ROW", item_code="BATCH", warehouse="WH", qty=5, company="CO", voucher_qty=5)
		self.assertEqual(reservation.reservation_based_on, "Qty")
		reservation.append.assert_not_called()
		reservation.submit.assert_called_once()

	def test_invalid_batch_promise_requires_adjustment_before_another_action(self):
		with self.assertRaises(frappe.ValidationError):
			actions._ensure_usable_reservations(frappe._dict(invalid_reserved_qty=2))

	def test_native_delivery_maps_multiple_reservations_with_one_total_limit(self):
		doc = MagicMock()
		doc.items = [frappe._dict(so_detail="ROW", qty=4), frappe._dict(so_detail="ROW", qty=4)]
		with (
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(actions, "_row_from_workbench", return_value=frappe._dict(reserved_qty=6)),
			patch.object(actions, "_locked_row_from_workbench", return_value=frappe._dict(reserved_qty=6)),
			patch.object(actions, "_get_existing_draft_delivery_note", return_value=None),
			patch.object(actions, "get_sales_order_item", return_value=frappe._dict(conversion_factor=1)),
			patch.object(actions, "make_delivery_note", return_value=doc),
		):
			actions.create_delivery_note("SO", "ROW")
		self.assertEqual([item.qty for item in doc.items], [4, 2])

	def test_native_bundle_is_not_silently_larger_than_limited_delivery_row(self):
		doc = MagicMock()
		doc.items = [frappe._dict(so_detail="ROW", qty=8, serial_and_batch_bundle="BUNDLE")]
		with (
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(actions, "_row_from_workbench", return_value=frappe._dict(reserved_qty=6)),
			patch.object(actions, "_locked_row_from_workbench", return_value=frappe._dict(reserved_qty=6)),
			patch.object(actions, "_get_existing_draft_delivery_note", return_value=None),
			patch.object(actions, "get_sales_order_item", return_value=frappe._dict(conversion_factor=1)),
			patch.object(actions, "make_delivery_note", return_value=doc),
			self.assertRaises(frappe.ValidationError),
		):
			actions.create_delivery_note("SO", "ROW")
		doc.insert.assert_not_called()


class BatchSalesFixture(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.company = "Batch Compatibility Test Company"
		if not frappe.db.exists("Company", cls.company):
			frappe.get_doc(dict(doctype="Company", company_name=cls.company, abbr="BCT", default_currency="INR", country="India", create_chart_of_accounts_based_on="Standard Template")).insert()
		cls.stores = "Stores - BCT"
		if not frappe.db.exists("Fiscal Year", {"year_start_date": ["<=", nowdate()], "year_end_date": [">=", nowdate()]}):
			year = nowdate()[:4]
			frappe.get_doc(dict(doctype="Fiscal Year", year=year, year_start_date=f"{year}-01-01", year_end_date=f"{year}-12-31")).insert()
		cls.price_list = "Batch Compatibility Selling"
		if not frappe.db.exists("Price List", cls.price_list):
			frappe.get_doc(dict(doctype="Price List", price_list_name=cls.price_list, selling=1, enabled=1, currency="INR")).insert()
		cls.customer = "Batch Compatibility Customer"
		if not frappe.db.exists("Customer", cls.customer):
			group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
			frappe.get_doc(dict(doctype="Customer", customer_name=cls.customer, customer_type="Company", customer_group=group, territory="All Territories")).insert()
		# These reusable masters outlive each transactional test's rollback.
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.prefix = "BATCH-COMPAT-" + frappe.generate_hash(length=8)
		for field, value in {"enable_serial_and_batch_no_for_item": 1, "enable_stock_reservation": 1, "auto_reserve_serial_and_batch": 0, "auto_create_serial_and_batch_bundle_for_outward": 1, "allow_partial_reservation": 0}.items():
			frappe.db.set_single_value("Stock Settings", field, value)

	def _item(self, label="FG", *, batch=True):
		return frappe.get_doc(dict(doctype="Item", item_code=self.prefix + "-" + label, item_name=label, item_group="Products", stock_uom="Nos", is_stock_item=1, is_sales_item=1, has_batch_no=int(batch), create_new_batch=0, valuation_rate=10, item_defaults=[dict(company=self.company, default_warehouse=self.stores)])).insert()

	def _receive(self, item, label, qty):
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
		batch = frappe.get_doc(dict(doctype="Batch", item=item.name, batch_id=self.prefix + "-" + label)).insert()
		entry = make_stock_entry(item_code=item.name, company=self.company, to_warehouse=self.stores, qty=qty, rate=10, batch_no=batch.name, use_serial_batch_fields=1)
		return entry, batch

	def _order(self, item, qty):
		return frappe.get_doc(dict(doctype="Sales Order", company=self.company, customer=self.customer, delivery_date=add_days(nowdate(), 1), currency="INR", selling_price_list=self.price_list, conversion_rate=1, items=[dict(item_code=item.name, qty=qty, rate=20, warehouse=self.stores)])).insert().submit()

	def _reserve(self, order, qty, *, entries=None, source=None):
		return actions._new_sre(sales_order=order.name, sales_order_item=order.items[0].name, item_code=order.items[0].item_code, warehouse=self.stores, qty=qty, company=self.company, voucher_qty=order.items[0].stock_qty, batch_entries=entries, from_voucher_type="Stock Entry" if source else None, from_voucher_no=source.parent if source else None, from_voucher_detail_no=source.name if source else None)


class TestBatchSalesIntegration(BatchSalesFixture):
	def test_existing_native_legacy_draft_recovers_reserved_batch_once(self):
		frappe.db.set_single_value("Stock Settings", "use_serial_batch_fields", 1)
		item = self._item("LEGACY-DRAFT")
		_, batch = self._receive(item, "LEGACY-DRAFT", 2)
		order = self._order(item, 2)
		self._reserve(order, 2, entries=[dict(batch_no=batch.name, qty=2)])
		draft = actions.make_delivery_note(order.name, kwargs={"for_reserved_stock": True, "filtered_children": [order.items[0].name]}).insert()
		self.assertFalse(draft.items[0].serial_and_batch_bundle)
		result = actions.create_delivery_note(order.name, order.items[0].name)
		self.assertTrue(result["reused"])
		self.assertEqual(result["delivery_note"], draft.name)
		draft.reload()
		bundle = draft.items[0].serial_and_batch_bundle
		self.assertTrue(bundle)
		actions.create_delivery_note(order.name, order.items[0].name)
		self.assertEqual(draft.reload().items[0].serial_and_batch_bundle, bundle)
		draft.submit()
		self.assertEqual(get_stock_snapshot(item.name, self.stores).actual_qty, 0)

	def test_bound_batch_delivery_works_with_both_native_field_display_settings(self):
		for use_fields in (0, 1):
			with self.subTest(use_serial_batch_fields=use_fields):
				frappe.db.set_single_value("Stock Settings", "use_serial_batch_fields", use_fields)
				item = self._item("FIELD-MODE-" + str(use_fields))
				_, first = self._receive(item, "FIRST-" + str(use_fields), 2)
				_, second = self._receive(item, "SECOND-" + str(use_fields), 3)
				order = self._order(item, 5)
				reservations = [
					self._reserve(order, 2, entries=[dict(batch_no=first.name, qty=2)]),
					self._reserve(order, 3, entries=[dict(batch_no=second.name, qty=3)]),
				]
				delivery = frappe.get_doc("Delivery Note", actions.create_delivery_note(order.name, order.items[0].name)["delivery_note"])
				self.assertEqual(len(delivery.items), 2)
				for line, batch, qty in zip(delivery.items, (first, second), (2, 3), strict=True):
					self.assertTrue(line.serial_and_batch_bundle)
					bundle = frappe.get_doc("Serial and Batch Bundle", line.serial_and_batch_bundle)
					self.assertEqual([(row.batch_no, abs(row.qty)) for row in bundle.entries], [(batch.name, qty)])
				delivery.submit()
				self.assertEqual(get_stock_snapshot(item.name, self.stores).actual_qty, 0)
				for reservation in reservations:
					reservation.reload()
					self.assertEqual(reservation.delivered_qty, reservation.reserved_qty)
					self.assertEqual(reservation.sb_entries[0].delivered_qty, reservation.reserved_qty)
				delivery.cancel()
				self.assertEqual(get_stock_snapshot(item.name, self.stores).actual_qty, 5)
				for reservation in reservations:
					reservation.reload()
					self.assertEqual(reservation.delivered_qty, 0)
					reservation.cancel()

	def test_actual_quick_submit_accepts_mixed_batch_and_plain_rows_and_replays_once(self):
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		tracked = self._item("QUICK-BATCH")
		plain = self._item("QUICK-PLAIN", batch=False)
		self._receive(tracked, "QUICK-A", 10)
		make_stock_entry(item_code=plain.name, company=self.company, to_warehouse=self.stores, qty=10, rate=10)
		customer = frappe.get_doc("Customer", self.customer)
		customer.default_price_list = self.price_list
		customer.save()
		for item in (tracked, plain):
			item.item_defaults[0].default_price_list = self.price_list
			item.save()
		payload = {
			"customer": self.customer,
			"delivery_date": add_days(nowdate(), 1),
			"po_no": self.prefix,
			"items": [
				{"item_code": tracked.name, "qty": 6, "rate": 20},
				{"item_code": plain.name, "qty": 3, "rate": 20},
			],
		}
		native_defaults = quick_order.get_company_defaults
		# Only select the isolated fixture company and retain the test transaction.
		# Native validation, document submission, stock reads and idempotency rows
		# are real. Notification delivery is irrelevant to this stock contract.
		with (
			patch.object(quick_order, "get_company_defaults", side_effect=lambda company=None: native_defaults(company or self.company)),
			patch.object(quick_order, "get_default_company", return_value=self.company),
			patch.object(frappe.db, "commit"),
			patch("process_simplification.notifications.notify_quick_order_submitted") as notify,
		):
			preflight = quick_order.preflight_quick_sales_order(payload)
			self.assertTrue(preflight["can_submit"], preflight.get("blockers"))
			self.assertEqual(preflight["production_required"], 0)
			self.assertEqual(preflight["available_to_reserve"], 9)
			token = preflight["review_token"]
			self.addCleanup(frappe.cache.delete_value, "process_simplification:quick_order_review:" + token, shared=True)
			first = quick_order.submit_quick_sales_order(payload, review_token=token, idempotency_key=self.prefix)
			self.assertEqual(first["status"], "submitted", first)
			order = frappe.get_doc("Sales Order", first["sales_order"])
			self.assertEqual(order.docstatus, 1)
			self.assertEqual(order.company, self.company)
			self.assertEqual({row.item_code: row.stock_qty for row in order.items}, {tracked.name: 6, plain.name: 3})
			self.assertTrue(all(row.warehouse == self.stores for row in order.items))
			self.assertTrue(all(not row.get("batch_no") for row in order.items))
			second = quick_order.submit_quick_sales_order(payload, review_token=token, idempotency_key=self.prefix)
			self.assertEqual(second["sales_order"], order.name)
			self.assertTrue(second["idempotent_replay"])
			self.assertEqual(frappe.db.count("Sales Order", {"company": self.company, "po_no": self.prefix}), 1)
			notify.assert_called_once()
		self.assertEqual(get_stock_snapshot(tracked.name, self.stores).actual_qty, 10)

	def test_batch_product_search_defaults_and_native_quantity_reservation_delivery(self):
		item = self._item()
		_, batch = self._receive(item, "A", 10)
		rows = quick_order.search_quick_order_products("Item", item.name, "name", 0, 20, {})
		self.assertIn(item.name, [row[0] for row in rows])
		defaults = quick_order.get_quick_order_item_defaults(item.name, company=self.company)
		self.assertTrue(defaults["has_batch_no"])
		self.assertEqual(defaults["available_to_reserve"], 10)
		order = self._order(item, 6)
		reservation = self._reserve(order, 6)
		self.assertEqual(reservation.reservation_based_on, "Qty")
		self.assertFalse(reservation.sb_entries)
		result = actions.create_delivery_note(order.name, order.items[0].name)
		delivery = frappe.get_doc("Delivery Note", result["delivery_note"])
		self.assertEqual(delivery.docstatus, 0)
		for row in delivery.items:
			row.use_serial_batch_fields = 1
			row.batch_no = batch.name
		delivery.save().submit()
		self.assertEqual(get_stock_snapshot(item.name, self.stores).actual_qty, 4)
		delivery.cancel()
		self.assertEqual(get_stock_snapshot(item.name, self.stores).actual_qty, 10)
		reservation.reload().cancel()
		self.assertEqual(get_available_qty(item.name, self.stores), 10)

	def test_mixed_batch_and_quantity_reservations_exclude_disabled_inventory_once(self):
		item = self._item()
		_, healthy = self._receive(item, "GOOD", 60)
		_, disabled = self._receive(item, "DISABLED", 40)
		frappe.db.set_value("Batch", disabled.name, "disabled", 1)
		first = self._order(item, 20)
		second = self._order(item, 20)
		self._reserve(first, 20, entries=[dict(batch_no=healthy.name, qty=20)])
		self._reserve(second, 20)
		facts = get_stock_snapshot(item.name, self.stores)
		self.assertEqual(facts.actual_qty, 100)
		self.assertEqual(facts.eligible_qty, 60)
		self.assertEqual(facts.available_qty, 20)
		self.assertEqual(sorted(facts.effective_reservation_qty.values()), [20, 20])

	def test_source_allocation_is_clipped_and_used_receipt_is_not_claimed_again(self):
		item = self._item()
		receipt, batch = self._receive(item, "SOURCE", 10)
		source = receipt.items[0]
		order = self._order(item, 10)
		entries = source_batch_allocation(source, 6)
		self.assertEqual([(row.batch_no, row.qty) for row in entries], [(batch.name, 6)])
		reservation = self._reserve(order, 6, entries=entries, source=source)
		self.assertEqual(sum(row.qty for row in source_batch_allocation(source, 10)), 4)
		delivery = frappe.get_doc("Delivery Note", actions.create_delivery_note(order.name, order.items[0].name)["delivery_note"])
		delivery.submit()
		self.assertEqual(sum(row.qty for row in source_batch_allocation(source, 10)), 4)
		delivery.cancel()
		reservation.reload().cancel()
		self.assertEqual(sum(row.qty for row in source_batch_allocation(source, 10)), 10)

	def test_source_batch_transferred_away_does_not_use_other_batch_in_same_warehouse(self):
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
		item = self._item()
		receipt, source_batch = self._receive(item, "SOURCE", 5)
		self._receive(item, "OTHER", 5)
		make_stock_entry(item_code=item.name, company=self.company, from_warehouse=self.stores, qty=5, batch_no=source_batch.name, use_serial_batch_fields=1)
		self.assertEqual(get_available_qty(item.name, self.stores), 5)
		self.assertEqual(source_batch_allocation(receipt.items[0], 5), [])

	def test_native_auto_reservation_setting_is_respected(self):
		item = self._item()
		self._receive(item, "A", 4)
		self._receive(item, "B", 6)
		frappe.db.set_single_value("Stock Settings", "auto_reserve_serial_and_batch", 1)
		order = self._order(item, 7)
		reservation = self._reserve(order, 7)
		self.assertEqual(reservation.reservation_based_on, "Serial and Batch")
		self.assertEqual(sum(row.qty for row in reservation.sb_entries), 7)
		self.assertEqual(get_available_qty(item.name, self.stores), 3)

	def test_invalid_bound_batch_remains_booked_but_is_not_executable(self):
		item = self._item()
		_, batch = self._receive(item, "A", 5)
		order = self._order(item, 5)
		self._reserve(order, 5, entries=[dict(batch_no=batch.name, qty=5)])
		frappe.db.set_value("Batch", batch.name, "disabled", 1)
		row = workbench.get_order_workbench(order.name)["rows"][0]
		self.assertEqual(row["booked_reserved_qty"], 5)
		self.assertEqual(row["reserved_qty"], 0)
		self.assertEqual(row["invalid_reserved_qty"], 5)
		self.assertEqual(row["status"], "批次预留需核对")
		with self.assertRaises(frappe.ValidationError):
			actions.create_delivery_note(order.name, order.items[0].name)

	def test_partial_delivery_of_a_bound_batch_can_be_cancelled_without_losing_reservation(self):
		from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import add_serial_batch_ledgers

		frappe.db.set_single_value("Stock Settings", "use_serial_batch_fields", 0)
		item = self._item("PARTIAL")
		_, first_batch = self._receive(item, "PARTIAL-A", 6)
		_, second_batch = self._receive(item, "PARTIAL-B", 4)
		order = self._order(item, 10)
		reservation = self._reserve(order, 10, entries=[
			{"batch_no": first_batch.name, "qty": 6},
			{"batch_no": second_batch.name, "qty": 4},
		])
		delivery = frappe.get_doc("Delivery Note", actions.create_delivery_note(order.name, order.items[0].name)["delivery_note"])
		self.assertEqual(len(delivery.items), 1)
		line = delivery.items[0]
		self.assertTrue(line.serial_and_batch_bundle)
		line.qty = 4
		# This is the native selector's update API, not a direct ledger rewrite.
		updated_bundle = add_serial_batch_ledgers(
			entries=[{"batch_no": first_batch.name, "qty": 4, "warehouse": self.stores}],
			child_row=frappe._dict(line.as_dict()), doc=delivery.as_dict(), warehouse=self.stores,
		)
		line.serial_and_batch_bundle = updated_bundle.name
		delivery.save().submit()
		reservation.reload()
		self.assertEqual(reservation.delivered_qty, 4)
		self.assertEqual({row.batch_no: row.delivered_qty for row in reservation.sb_entries}, {first_batch.name: 4, second_batch.name: 0})
		facts = get_stock_snapshot(item.name, self.stores)
		self.assertEqual(facts.actual_qty, 6)
		self.assertEqual(facts.available_qty, 0)
		self.assertEqual(facts.effective_reservation_qty[reservation.name], 6)
		delivery.cancel()
		reservation.reload()
		self.assertEqual(reservation.delivered_qty, 0)
		self.assertTrue(all(not row.delivered_qty for row in reservation.sb_entries))
		facts = get_stock_snapshot(item.name, self.stores)
		self.assertEqual(facts.actual_qty, 10)
		self.assertEqual(facts.effective_reservation_qty[reservation.name], 10)
		reservation.cancel()
		self.assertEqual(get_available_qty(item.name, self.stores), 10)

	def test_native_partial_customer_return_keeps_original_batch_and_cancel_restores_stock(self):
		from erpnext.stock.doctype.batch.batch import get_batch_qty
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return

		frappe.db.set_single_value("Stock Settings", "use_serial_batch_fields", 0)
		item = self._item("CUSTOMER-RETURN")
		_, shipped_batch = self._receive(item, "RETURN-A", 10)
		_, untouched_batch = self._receive(item, "RETURN-B", 5)
		order = self._order(item, 6)
		reservation = self._reserve(order, 6, entries=[{"batch_no": shipped_batch.name, "qty": 6}])
		delivery = frappe.get_doc("Delivery Note", actions.create_delivery_note(order.name, order.items[0].name)["delivery_note"])
		delivery.submit()
		returned = make_sales_return(delivery.name)
		returned.items[0].qty = -2
		returned.save().submit()
		self.assertEqual(returned.return_against, delivery.name)
		self.assertEqual(returned.items[0].stock_qty, -2)
		bundle = frappe.get_doc("Serial and Batch Bundle", returned.items[0].serial_and_batch_bundle)
		self.assertEqual({row.batch_no for row in bundle.entries}, {shipped_batch.name})
		self.assertEqual(sum(abs(flt(row.qty)) for row in bundle.entries), 2)
		self.assertEqual(get_batch_qty(batch_no=shipped_batch.name, warehouse=self.stores, ignore_reserved_stock=True), 6)
		self.assertEqual(get_batch_qty(batch_no=untouched_batch.name, warehouse=self.stores, ignore_reserved_stock=True), 5)
		self.assertEqual(get_stock_snapshot(item.name, self.stores).actual_qty, 11)
		returned.cancel()
		self.assertEqual(get_stock_snapshot(item.name, self.stores).actual_qty, 9)
		self.assertEqual(get_batch_qty(batch_no=shipped_batch.name, warehouse=self.stores, ignore_reserved_stock=True), 4)
		delivery.reload().cancel()
		reservation.reload().cancel()
		self.assertEqual(get_available_qty(item.name, self.stores), 15)
