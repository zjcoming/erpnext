"""Native stock paths remain usable with the simplified application installed."""

import frappe

from process_simplification.batch_compat import get_stock_snapshot
from process_simplification.batch_display import document_batch_summary
from process_simplification.tests.test_batch_sales import BatchSalesFixture


class TestBatchNativeIntegration(BatchSalesFixture):
	def test_purchase_receipt_rejected_stock_and_supplier_return_keep_batches(self):
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import (
			make_purchase_return,
			make_purchase_return_against_rejected_warehouse,
		)

		item = self._item("PURCHASE")
		supplier = frappe.get_doc(dict(
			doctype="Supplier", supplier_name=self.prefix, supplier_type="Company",
			supplier_group=frappe.db.get_value("Supplier Group", {"is_group": 0}, "name"),
		)).insert()
		rejected = frappe.get_doc(dict(
			doctype="Warehouse", warehouse_name=self.prefix + "-Rejected", company=self.company,
		)).insert()
		batch = frappe.get_doc(dict(doctype="Batch", batch_id=self.prefix + "-PR", item=item.name)).insert()
		receipt = frappe.get_doc(dict(
			doctype="Purchase Receipt", company=self.company, supplier=supplier.name,
			currency="INR", conversion_rate=1,
			items=[dict(item_code=item.name, qty=4, received_qty=5, rejected_qty=1, rate=20,
				warehouse=self.stores, rejected_warehouse=rejected.name,
				use_serial_batch_fields=1, batch_no=batch.name)],
		)).insert().submit()
		summary = document_batch_summary(receipt)[receipt.items[0].name]
		self.assertEqual([(x["batch_no"], x["qty"]) for x in summary["batches"]], [(batch.name, 4)])
		self.assertEqual([(x["batch_no"], x["qty"]) for x in summary["rejected_batches"]], [(batch.name, 1)])
		self.assertEqual(get_stock_snapshot(item.name, self.stores).actual_qty, 4)
		self.assertEqual(get_stock_snapshot(item.name, rejected.name).actual_qty, 1)

		returned_rejected = make_purchase_return_against_rejected_warehouse(receipt.name)
		returned_rejected.insert().submit()
		self.assertEqual(get_stock_snapshot(item.name, rejected.name).actual_qty, 0)
		returned_rejected.reload().cancel()
		returned = make_purchase_return(receipt.name)
		returned.insert().submit()
		self.assertEqual(get_stock_snapshot(item.name, self.stores).actual_qty, 0)
		returned.reload().cancel()
		receipt.reload().cancel()
		self.assertEqual(get_stock_snapshot(item.name, self.stores).actual_qty, 0)
		self.assertEqual(get_stock_snapshot(item.name, rejected.name).actual_qty, 0)

	def test_native_transfer_and_repack_need_no_production_plan(self):
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		raw = self._item("REPACK-RAW")
		output = self._item("REPACK-OUTPUT")
		_, batch = self._receive(raw, "REPACK-A", 10)
		target = frappe.get_doc(dict(
			doctype="Warehouse", warehouse_name=self.prefix + "-Other", company=self.company,
		)).insert()
		transfer = make_stock_entry(item_code=raw.name, company=self.company,
			from_warehouse=self.stores, to_warehouse=target.name, qty=4, rate=10,
			batch_no=batch.name)
		self.assertEqual(get_stock_snapshot(raw.name, self.stores).actual_qty, 6)
		self.assertEqual(get_stock_snapshot(raw.name, target.name).batch_available_qty, {batch.name: 4})
		out_batch = frappe.get_doc(dict(doctype="Batch", batch_id=self.prefix + "-REPACK-B", item=output.name)).insert()
		repack = frappe.get_doc(dict(
			doctype="Stock Entry", stock_entry_type="Repack", company=self.company,
			items=[
				dict(item_code=raw.name, qty=4, s_warehouse=target.name,
					use_serial_batch_fields=1, batch_no=batch.name),
				dict(item_code=output.name, qty=2, t_warehouse=target.name, is_finished_item=1,
					use_serial_batch_fields=1, batch_no=out_batch.name, basic_rate=20),
			],
		)).insert().submit()
		self.assertFalse(repack.work_order)
		self.assertEqual(get_stock_snapshot(raw.name, target.name).actual_qty, 0)
		self.assertEqual(get_stock_snapshot(output.name, target.name).batch_available_qty, {out_batch.name: 2})
		repack.reload().cancel()
		transfer.reload().cancel()
		self.assertEqual(get_stock_snapshot(raw.name, self.stores).actual_qty, 10)
		self.assertEqual(get_stock_snapshot(output.name, target.name).actual_qty, 0)

	def test_native_batch_reconciliation_and_cancel_preserve_other_batch(self):
		from erpnext.stock.doctype.stock_reconciliation.test_stock_reconciliation import create_stock_reconciliation

		item = self._item("COUNT")
		_, first = self._receive(item, "COUNT-A", 10)
		_, second = self._receive(item, "COUNT-B", 5)
		reconciliation = create_stock_reconciliation(
			item_code=item.name, warehouse=self.stores, company=self.company,
			qty=8, rate=10, batch_no=first.name, reconcile_all_serial_batch=0,
		)
		self.assertEqual(reconciliation.docstatus, 1)
		self.assertEqual(get_stock_snapshot(item.name, self.stores).batch_available_qty,
			{first.name: 8, second.name: 5})
		reconciliation.reload().cancel()
		self.assertEqual(get_stock_snapshot(item.name, self.stores).batch_available_qty,
			{first.name: 10, second.name: 5})
