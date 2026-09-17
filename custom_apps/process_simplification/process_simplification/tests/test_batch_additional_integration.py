"""Native stock invoices and independent-session batch reservation contracts."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextvars import Context
from threading import Event

import frappe
from frappe.utils import flt, nowdate, nowtime

from process_simplification.batch_compat import get_stock_snapshot
from process_simplification.tests.test_batch_sales import BatchSalesFixture


class TestBatchAdditionalIntegration(BatchSalesFixture):
	def setUp(self):
		# The concurrency case must commit its unique fixture for another connection.
		# Preserve settings before the shared fixture changes them, then restore them
		# with its native cancellation cleanup. Ordinary invoice work rolls back.
		self._previous_settings = {
			field: frappe.db.get_single_value("Stock Settings", field)
			for field in (
				"enable_serial_and_batch_no_for_item", "enable_stock_reservation",
				"auto_reserve_serial_and_batch", "auto_create_serial_and_batch_bundle_for_outward",
				"allow_partial_reservation",
			)
		}
		super().setUp()
		self.addCleanup(frappe.db.rollback)

	def _invoice(self, doctype, item, batch, qty, supplier=None):
		from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import add_serial_batch_ledgers

		company = frappe.get_doc("Company", self.company)
		values = dict(
			doctype=doctype, company=self.company, currency="INR", conversion_rate=1,
			posting_date=nowdate(), posting_time=nowtime(), set_posting_time=1,
			update_stock=1, cost_center=company.cost_center,
			items=[dict(
				item_code=item.name, qty=qty, rate=20, warehouse=self.stores,
				uom="Nos", stock_uom="Nos", conversion_factor=1,
				cost_center=company.cost_center, expense_account=company.default_expense_account,
				use_serial_batch_fields=0,
			)],
		)
		if doctype == "Purchase Invoice":
			values.update(supplier=supplier, credit_to=company.default_payable_account)
		else:
			values.update(customer=self.customer, debit_to=company.default_receivable_account,
				selling_price_list=self.price_list)
			values["items"][0]["income_account"] = company.default_income_account
		doc = frappe.get_doc(values)
		row = doc.items[0]
		# This is the native selector's save path before the invoice is inserted.
		bundle = add_serial_batch_ledgers(
			entries=[dict(batch_no=batch.name, qty=qty)],
			child_row=row.as_dict(), doc=doc.as_dict(), warehouse=self.stores,
		)
		row.serial_and_batch_bundle = bundle.name
		return doc.insert().submit()

	def _assert_invoice_bundle(self, doc, batch, qty):
		doc.reload()
		self.assertEqual(doc.docstatus, 1)
		self.assertTrue(doc.update_stock)
		self.assertFalse(doc.items[0].use_serial_batch_fields)
		bundle = frappe.get_doc("Serial and Batch Bundle", doc.items[0].serial_and_batch_bundle)
		self.assertEqual(bundle.docstatus, 1)
		self.assertEqual((bundle.company, bundle.warehouse), (self.company, self.stores))
		self.assertEqual((bundle.voucher_type, bundle.voucher_no, bundle.voucher_detail_no),
			(doc.doctype, doc.name, doc.items[0].name))
		self.assertEqual([(row.batch_no, flt(row.qty)) for row in bundle.entries], [(batch.name, qty)])

	def _assert_batch_stock(self, item, batch, qty):
		facts = get_stock_snapshot(item.name, self.stores)
		self.assertEqual(facts.actual_qty, qty)
		self.assertEqual(facts.batch_available_qty.get(batch.name, 0), qty)

	def test_native_inventory_invoices_returns_and_cancellation_need_no_production_source(self):
		from erpnext.accounts.doctype.purchase_invoice.purchase_invoice import make_debit_note
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

		item = self._item("INVOICES")
		batch = frappe.get_doc(dict(doctype="Batch", item=item.name, batch_id=self.prefix + "-INVOICE")).insert()
		supplier = frappe.get_doc(dict(
			doctype="Supplier", supplier_name=self.prefix, supplier_type="Company",
			supplier_group=frappe.db.get_value("Supplier Group", {"is_group": 0}, "name"),
		)).insert()
		purchase = self._invoice("Purchase Invoice", item, batch, 10, supplier.name)
		self._assert_invoice_bundle(purchase, batch, 10)
		self._assert_batch_stock(item, batch, 10)

		sale = self._invoice("Sales Invoice", item, batch, 4)
		self._assert_invoice_bundle(sale, batch, -4)
		self._assert_batch_stock(item, batch, 6)
		sales_return = make_sales_return(sale.name).insert().submit()
		self._assert_invoice_bundle(sales_return, batch, 4)
		self._assert_batch_stock(item, batch, 10)
		sales_return.cancel()
		self._assert_batch_stock(item, batch, 6)
		sale.reload().cancel()
		self._assert_batch_stock(item, batch, 10)

		purchase_return = make_debit_note(purchase.name).insert().submit()
		self._assert_invoice_bundle(purchase_return, batch, -10)
		self._assert_batch_stock(item, batch, 0)
		purchase_return.cancel()
		self._assert_batch_stock(item, batch, 10)
		purchase.reload().cancel()
		self._assert_batch_stock(item, batch, 0)
		self.assertFalse(frappe.db.exists("Stock Entry Detail", {"item_code": item.name}))
		self.assertFalse(frappe.db.exists("Stock Reservation Entry", {"item_code": item.name}))

	def test_two_database_sessions_cannot_claim_same_finished_source_using_other_batch(self):
		from process_simplification.batch_compat import source_batch_allocation
		from process_simplification.production_workflow.stock_reservation import locked_available_qty

		item = self._item("SOURCE-RACE")
		receipt, batch = self._receive(item, "SOURCE-RACE", 1)
		other_receipt, _ = self._receive(item, "OTHER-SOURCE", 9)
		source = receipt.items[0]
		orders = [self._order(item, 1), self._order(item, 1)]
		site = frappe.local.site
		started = Event()
		primary_connection = frappe.db.sql("select connection_id()")[0][0]
		frappe.db.commit()

		def competitor():
			frappe.init(site=site); frappe.connect(); frappe.set_user("Administrator")
			frappe.flags.in_test = True
			connection = frappe.db.sql("select connection_id()")[0][0]
			try:
				frappe.db.sql("SET SESSION innodb_lock_wait_timeout = 5")
				before = sum(row.qty for row in source_batch_allocation(source, 1))
				started.set()
				try:
					locked_available_qty(item.name, self.stores)
					entries = source_batch_allocation(source, 1)
					if not entries:
						return dict(status="exhausted", before=before, connection=connection)
					self._reserve(orders[1], 1, entries=entries, source=source)
					return dict(status="accepted", before=before, connection=connection)
				except frappe.QueryDeadlockError:
					return dict(status="conflict", before=before, connection=connection)
			finally:
				frappe.db.rollback(); frappe.destroy()

		try:
			locked_available_qty(item.name, self.stores)
			winner = self._reserve(orders[0], 1, entries=source_batch_allocation(source, 1), source=source)
			with ThreadPoolExecutor(max_workers=1) as executor:
				future = executor.submit(Context().run, competitor)
				try:
					self.assertTrue(started.wait(timeout=10))
					with self.assertRaises(FutureTimeoutError):
						future.result(timeout=0.25)
					frappe.db.commit()
					result = future.result(timeout=15)
				finally:
					frappe.db.rollback()
			self.assertNotEqual(primary_connection, result["connection"])
			self.assertEqual(result["before"], 1)
			self.assertIn(result["status"], {"exhausted", "conflict"})
			self.assertEqual(frappe.db.count("Stock Reservation Entry", {"item_code": item.name, "docstatus": 1}), 1)
			self.assertEqual([(row.batch_no, row.qty) for row in winner.sb_entries], [(batch.name, 1)])
			self.assertEqual(source_batch_allocation(source, 1), [])
			facts = get_stock_snapshot(item.name, self.stores)
			self.assertEqual((facts.actual_qty, facts.available_qty), (10, 9))
		finally:
			frappe.db.rollback()
			for name in frappe.get_all("Stock Reservation Entry", filters={"item_code": item.name, "docstatus": 1}, pluck="name"):
				frappe.get_doc("Stock Reservation Entry", name).cancel()
			for doc in (*reversed(orders), other_receipt, receipt):
				if frappe.db.get_value(doc.doctype, doc.name, "docstatus") == 1:
					frappe.get_doc(doc.doctype, doc.name).cancel()
			for field, value in self._previous_settings.items():
				frappe.db.set_single_value("Stock Settings", field, value)
			frappe.db.commit()
			frappe.clear_document_cache("Stock Settings", "Stock Settings")

	def test_two_database_sessions_cannot_both_reserve_last_eligible_batch_stock(self):
		item = self._item("CONCURRENT")
		receipt, available_batch = self._receive(item, "LAST-AVAILABLE", 1)
		blocked_receipt, blocked_batch = self._receive(item, "DISABLED", 9)
		frappe.db.set_value("Batch", blocked_batch.name, "disabled", 1)
		orders = [self._order(item, 1), self._order(item, 1)]
		# Allocate names before the race: otherwise native tabSeries locking can
		# serialize two inserts before either reaches the stock-pool lock.
		drafts = [frappe.get_doc(dict(
			doctype="Stock Reservation Entry", company=self.company,
			voucher_type="Sales Order", voucher_no=order.name, voucher_detail_no=order.items[0].name,
			item_code=item.name, warehouse=self.stores, stock_uom="Nos", has_batch_no=1,
			available_qty=1, voucher_qty=1, reserved_qty=1, reservation_based_on="Qty",
		)).insert() for order in orders]
		site = frappe.local.site
		started = Event()
		primary_connection = frappe.db.sql("select connection_id()")[0][0]
		# Committed, unique fixture only: both sessions must see the same last unit.
		# finally cancels all its stock/reservations and restores singleton settings.
		frappe.db.commit()

		def competitor():
			frappe.init(site=site)
			frappe.connect()
			frappe.set_user("Administrator")
			frappe.flags.in_test = True
			connection = frappe.db.sql("select connection_id()")[0][0]
			try:
				frappe.db.sql("SET SESSION innodb_lock_wait_timeout = 5")
				before = get_stock_snapshot(item.name, self.stores)
				reservation = frappe.get_doc("Stock Reservation Entry", drafts[1].name)
				started.set()
				try:
					reservation.submit()
				except frappe.ValidationError as exc:
					return dict(status="rejected", connection=connection,
						before=before.available_qty, error=str(exc))
				except frappe.QueryDeadlockError as exc:
					# MariaDB snapshot isolation may reject the stale snapshot at
					# FOR UPDATE. This is a safe transaction conflict, not success.
					return dict(status="conflict", connection=connection,
						before=before.available_qty, error=str(exc))
				return dict(status="accepted", connection=connection, before=before.available_qty)
			finally:
				# A failed competing submit must leave its original draft unchanged.
				frappe.db.rollback()
				frappe.destroy()

		try:
			facts = get_stock_snapshot(item.name, self.stores)
			self.assertEqual((facts.actual_qty, facts.eligible_qty, facts.available_qty), (10, 1, 1))
			self.assertEqual(facts.batch_available_qty, {available_batch.name: 1})
			winner = drafts[0].submit()
			with ThreadPoolExecutor(max_workers=1) as executor:
				future = executor.submit(Context().run, competitor)
				try:
					self.assertTrue(started.wait(timeout=10), "Competing database session did not start")
					# The native SRE validation reaches the same Bin lock while the
					# first submitted reservation is still uncommitted.
					with self.assertRaises(FutureTimeoutError):
						future.result(timeout=0.25)
					frappe.db.commit()
					result = future.result(timeout=15)
				finally:
					frappe.db.rollback()
			self.assertNotEqual(primary_connection, result["connection"])
			self.assertEqual(result["before"], 1)
			self.assertIn(result["status"], {"rejected", "conflict"}, result)
			self.assertTrue(result["error"])
			reservations = frappe.get_all("Stock Reservation Entry",
				filters={"item_code": item.name}, fields=["name", "docstatus", "reserved_qty", "has_batch_no"])
			self.assertEqual({r.name: (r.docstatus, flt(r.reserved_qty), r.has_batch_no) for r in reservations},
				{winner.name: (1, 1, 1), drafts[1].name: (0, 1, 1)})
			facts = get_stock_snapshot(item.name, self.stores)
			self.assertEqual((facts.actual_qty, facts.eligible_qty, facts.available_qty), (10, 1, 0))
		finally:
			frappe.db.rollback()
			# Native cancellation preserves an auditable test history but leaves no
			# stock or reservation from this committed cross-session fixture.
			for name in frappe.get_all("Stock Reservation Entry",
				filters={"item_code": item.name, "docstatus": 1}, pluck="name"):
				frappe.get_doc("Stock Reservation Entry", name).cancel()
			for name in frappe.get_all("Stock Reservation Entry",
				filters={"item_code": item.name, "docstatus": 0}, pluck="name"):
				frappe.delete_doc("Stock Reservation Entry", name)
			frappe.db.set_value("Batch", blocked_batch.name, "disabled", 0)
			for doc in (*reversed(orders), blocked_receipt, receipt):
				if frappe.db.get_value(doc.doctype, doc.name, "docstatus") == 1:
					frappe.get_doc(doc.doctype, doc.name).cancel()
			for field, value in self._previous_settings.items():
				frappe.db.set_single_value("Stock Settings", field, value)
			frappe.db.commit()
			frappe.clear_document_cache("Stock Settings", "Stock Settings")
			self.assertEqual(get_stock_snapshot(item.name, self.stores).actual_qty, 0)
