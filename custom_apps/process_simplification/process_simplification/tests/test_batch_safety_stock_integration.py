"""Daily batch receipt and native safety-stock replenishment on real stock documents."""

from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, nowdate

from erpnext.stock import reorder_item as native_reorder
from process_simplification.batch_compat import get_stock_snapshot
from process_simplification.purchasing import allocation
from process_simplification.tests.test_batch_sales import BatchSalesFixture


class TestBatchSafetyStockIntegration(BatchSalesFixture):
	def setUp(self):
		super().setUp()
		frappe.db.set_single_value("Stock Settings", "auto_indent", 1)
		frappe.db.set_single_value("Stock Settings", "reorder_email_notify", 0)
		previous_email_notify = getattr(frappe.local, "reorder_email_notify", None)
		self.addCleanup(setattr, frappe.local, "reorder_email_notify", previous_email_notify)
		frappe.local.reorder_email_notify = 0

	def _automatic_item(self, label="AUTO"):
		item = self._item(label)
		item.create_new_batch = 1
		item.batch_number_series = self.prefix + "-.#####"
		return item.save()

	def _reorder_rule(self, item, *, level=5, qty=4, warehouse_group=None):
		item.safety_stock = level
		item.append("reorder_levels", dict(
			warehouse=self.stores, warehouse_group=warehouse_group,
			warehouse_reorder_level=level, warehouse_reorder_qty=qty,
			material_request_type="Purchase",
		))
		return item.save()

	def _run_reorder(self):
		# Limit job discovery to this transaction's unique test items. Quantity
		# calculation, native MR submission and stock accounting are unmodified.
		native_discovery = native_reorder.get_items_for_reorder
		with (
			patch.object(native_reorder, "get_items_for_reorder", side_effect=lambda: {
				code: rows for code, rows in native_discovery().items() if code.startswith(self.prefix)
			}),
			patch.object(native_reorder, "send_email_notification"),
			patch.object(native_reorder, "notify_errors") as errors,
		):
			requests = native_reorder.reorder_item() or []
			errors.assert_not_called()
			return requests

	def _bin(self, item):
		return frappe._dict(frappe.db.get_value("Bin", dict(item_code=item.name, warehouse=self.stores),
			["actual_qty", "projected_qty", "indented_qty", "ordered_qty"], as_dict=True) or {})

	def test_safety_stock_alone_and_disabled_automatic_reorder_do_not_create_requests(self):
		item = self._item("OFF")
		item.safety_stock = 5
		item.save()
		self.assertEqual(self._run_reorder(), [])
		self._reorder_rule(item)
		frappe.db.set_single_value("Stock Settings", "auto_indent", 0)
		self.assertEqual(self._run_reorder(), [])
		self.assertFalse(frappe.db.exists("Material Request Item", {"item_code": item.name}))

	def test_automatic_inward_batch_and_automatic_outward_preserve_actual_identity(self):
		item = self._automatic_item()
		doc = frappe.get_doc(dict(doctype="Stock Entry", stock_entry_type="Material Receipt",
			company=self.company, items=[dict(item_code=item.name, qty=10, t_warehouse=self.stores,
			basic_rate=10, use_serial_batch_fields=0)])).insert().submit()
		doc.reload()
		bundle = frappe.get_doc("Serial and Batch Bundle", doc.items[0].serial_and_batch_bundle)
		self.assertEqual(bundle.docstatus, 1)
		self.assertEqual(len(bundle.entries), 1)
		batch = bundle.entries[0].batch_no
		self.assertTrue(batch.startswith(self.prefix))
		self.assertEqual(frappe.db.get_value("Batch", batch, "item"), item.name)
		self.assertEqual(flt(bundle.entries[0].qty), 10)
		issue = frappe.get_doc(dict(doctype="Stock Entry", stock_entry_type="Material Issue",
			company=self.company, items=[dict(item_code=item.name, qty=4, s_warehouse=self.stores,
			use_serial_batch_fields=0)])).insert().submit()
		issue.reload()
		outward = frappe.get_doc("Serial and Batch Bundle", issue.items[0].serial_and_batch_bundle)
		self.assertEqual([(e.batch_no, flt(e.qty)) for e in outward.entries], [(batch, -4)])
		self.assertEqual(get_stock_snapshot(item.name, self.stores).batch_available_qty, {batch: 6})
		issue.cancel()
		doc.reload().cancel()
		self.assertEqual(self._bin(item).actual_qty, 0)

	def test_native_reorder_uses_pending_supply_once_and_enters_factory_purchase_queue(self):
		item = self._automatic_item("REORDER")
		self._reorder_rule(item)
		self._receive(item, "INITIAL", 2)
		before = self._bin(item)
		requests = self._run_reorder()
		self.assertEqual(len(requests), 1)
		request = requests[0]
		self.assertEqual((request.docstatus, request.auto_created_via_reorder), (1, 1))
		self.assertEqual(flt(request.items[0].stock_qty), 4)
		self.assertEqual(self._bin(item).actual_qty, before.actual_qty)
		self.assertEqual(self._bin(item).projected_qty, 6)
		self.assertEqual(self._run_reorder(), [])
		page = allocation.get_request_page(search=item.name)
		self.assertIn(request.name, [row["name"] for row in page["rows"]])
		context = allocation.get_allocation_context(request.name)
		self.assertEqual(context["items"][0]["available_qty"], 4)
		request.cancel()
		self.assertEqual(self._bin(item).projected_qty, 2)
		self.assertEqual(len(self._run_reorder()), 1)

	def test_disabled_and_expired_batches_cannot_suppress_required_replenishment(self):
		item = self._item("INVALID-STOCK")
		self._reorder_rule(item, qty=6)
		self._receive(item, "USABLE", 1)
		_, disabled = self._receive(item, "DISABLED", 5)
		_, expired = self._receive(item, "EXPIRED", 4)
		frappe.db.set_value("Batch", disabled.name, "disabled", 1)
		frappe.db.set_value("Batch", expired.name, "expiry_date", add_days(nowdate(), -1))
		facts = get_stock_snapshot(item.name, self.stores)
		self.assertEqual((facts.actual_qty, facts.eligible_qty), (10, 1))
		requests = self._run_reorder()
		self.assertEqual(len(requests), 1,
			"Only 1 usable unit remains below the reorder level 5; unusable lots must not suppress replenishment")
		self.assertEqual(flt(requests[0].items[0].stock_qty), 6)
		self.assertEqual(self._run_reorder(), [])

	def test_batch_reorder_preserves_sales_demand_without_double_counting_hard_reservation(self):
		item = self._item("RESERVED")
		self._reorder_rule(item, level=10, qty=1)
		_, good = self._receive(item, "GOOD", 10)
		_, bad = self._receive(item, "BAD", 10)
		frappe.db.set_value("Batch", bad.name, "disabled", 1)
		order = self._order(item, 2)
		self._reserve(order, 2, entries=[dict(batch_no=good.name, qty=2)])
		self.assertEqual(self._bin(item).projected_qty, 18)
		projected = native_reorder.get_item_warehouse_projected_qty({item.name: []})
		self.assertEqual(projected[item.name][self.stores], 8)
		request = self._run_reorder()[0]
		self.assertEqual(flt(request.items[0].stock_qty), 2)
		self.assertEqual(self._bin(item).actual_qty, 20)

	def test_group_reorder_sums_usable_lots_across_warehouses(self):
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		item = self._item("GROUP")
		group = "All Warehouses - BCT"
		self._reorder_rule(item, level=9, qty=3, warehouse_group=group)
		other = frappe.get_doc(dict(doctype="Warehouse", warehouse_name=self.prefix + "-Other",
			company=self.company, parent_warehouse=group)).insert()
		self._receive(item, "LOCAL", 2)
		_, bad = self._receive(item, "UNUSABLE", 20)
		frappe.db.set_value("Batch", bad.name, "disabled", 1)
		batch = frappe.get_doc(dict(doctype="Batch", item=item.name, batch_id=self.prefix + "-OTHER")).insert()
		make_stock_entry(item_code=item.name, company=self.company, to_warehouse=other.name,
			qty=6, rate=10, batch_no=batch.name, use_serial_batch_fields=1)
		projected = native_reorder.get_item_warehouse_projected_qty({item.name: []})[item.name]
		self.assertEqual((projected[self.stores], projected[other.name], projected[group]), (2, 6, 8))
		request = self._run_reorder()[0]
		self.assertEqual((request.items[0].warehouse, flt(request.items[0].stock_qty)), (self.stores, 3))
		self.assertEqual(self._run_reorder(), [])

	def test_plain_stock_keeps_native_reorder_trigger_at_equal_threshold(self):
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		item = self._item("PLAIN", batch=False)
		self._reorder_rule(item)
		make_stock_entry(item_code=item.name, company=self.company, to_warehouse=self.stores, qty=5, rate=10)
		with patch.object(native_reorder, "_usable_batch_stock_for_reorder") as batch_query:
			request = self._run_reorder()[0]
			batch_query.assert_not_called()
		self.assertEqual(flt(request.items[0].stock_qty), 4)
		self.assertEqual(self._bin(item).projected_qty, 9)
		self.assertEqual(self._run_reorder(), [])

	def test_automatic_reorder_purchase_partial_receipts_and_return_keep_supply_once(self):
		from erpnext.stock.doctype.material_request.material_request import make_purchase_order
		from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_return

		item = self._automatic_item("PURCHASE-CHAIN")
		self._reorder_rule(item)
		self._receive(item, "OPENING", 2)
		request = self._run_reorder()[0]
		supplier = frappe.get_doc(dict(doctype="Supplier", supplier_name=self.prefix,
			supplier_type="Company", supplier_group=frappe.db.get_value("Supplier Group", {"is_group": 0}, "name"))).insert()
		order = make_purchase_order(request.name)
		order.supplier = supplier.name
		order.currency, order.conversion_rate = "INR", 1
		order.items[0].rate = 10
		order.insert().submit()
		self.assertEqual((self._bin(item).indented_qty, self._bin(item).ordered_qty), (0, 4))
		self.assertEqual(self._run_reorder(), [])

		first = make_purchase_receipt(order.name)
		first.items[0].qty = 2
		first.insert().submit()
		first.reload()
		first_bundle = frappe.get_doc("Serial and Batch Bundle", first.items[0].serial_and_batch_bundle)
		first_batch = first_bundle.entries[0].batch_no
		self.assertEqual((self._bin(item).actual_qty, self._bin(item).ordered_qty, self._bin(item).projected_qty), (4, 2, 6))
		self.assertEqual(self._run_reorder(), [])

		returned = make_purchase_return(first.name)
		returned.items[0].qty = -1
		# Use the original batch identity for a partial supplier return.
		returned.items[0].serial_and_batch_bundle = None
		returned.items[0].use_serial_batch_fields = 1
		returned.items[0].batch_no = first_batch
		returned.insert().submit()
		self.assertEqual((self._bin(item).actual_qty, self._bin(item).ordered_qty, self._bin(item).projected_qty), (3, 3, 6))
		self.assertEqual(self._run_reorder(), [])
		self.assertEqual(get_stock_snapshot(item.name, self.stores).batch_available_qty[first_batch], 1)
		returned.cancel()
		first.reload().cancel()
		order.reload().cancel()
		request.reload().cancel()
		self.assertEqual((self._bin(item).actual_qty, self._bin(item).projected_qty), (2, 2))

	def test_native_production_plan_adds_safety_stock_only_when_requested(self):
		from erpnext.manufacturing.doctype.production_plan.production_plan import get_items_for_material_requests

		raw = self._item("SAFETY-RAW")
		raw.safety_stock = 5
		raw.save()
		finished = self._item("SAFETY-FINISHED", batch=False)
		bom = frappe.get_doc(dict(doctype="BOM", company=self.company, item=finished.name,
			quantity=1, is_active=1, is_default=1, items=[dict(item_code=raw.name, qty=1, rate=10)])).insert().submit()
		plan = frappe.new_doc("Production Plan")
		plan.company, plan.for_warehouse = self.company, self.stores
		plan.append("po_items", dict(item_code=finished.name, bom_no=bom.name, planned_qty=3,
			warehouse=self.stores, include_exploded_items=1))
		plan.include_safety_stock = 0
		without = get_items_for_material_requests(plan.as_dict())
		plan.include_safety_stock = 1
		with_safety = get_items_for_material_requests(plan.as_dict())
		self.assertEqual(sum(flt(row["quantity"]) for row in without if row["item_code"] == raw.name), 3)
		self.assertEqual(sum(flt(row["quantity"]) for row in with_safety if row["item_code"] == raw.name), 8)
		self.assertFalse(frappe.db.exists("Material Request Item", {"item_code": raw.name}))
