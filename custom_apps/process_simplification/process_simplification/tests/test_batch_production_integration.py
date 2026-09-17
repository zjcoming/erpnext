"""Native batch transactions behind the simplified manufacturing hand-offs."""

from collections import defaultdict

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, getdate, now_datetime, nowdate

from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.serial_batch_bundle import SerialBatchCreation
from process_simplification.batch_compat import get_stock_snapshot
from process_simplification.production_workflow import service


class TestBatchProductionIntegration(IntegrationTestCase):
	COMPANY = "Batch Production Test Company"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("Company", cls.COMPANY):
			frappe.get_doc(dict(doctype="Company", company_name=cls.COMPANY, abbr="BPC",
				default_currency="INR", country="India",
				create_chart_of_accounts_based_on="Standard Template")).insert()
		if not frappe.db.exists("Fiscal Year", {
			"year_start_date": ["<=", nowdate()], "year_end_date": [">=", nowdate()], "disabled": 0,
		}):
			year = getdate().year
			frappe.get_doc(dict(doctype="Fiscal Year", year=f"Batch Production {year}",
				year_start_date=f"{year}-01-01", year_end_date=f"{year}-12-31")).insert()
		# Only class master fixtures persist in the isolated test site. Each test's
		# unique items, warehouses and business transactions are rolled back below.
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.addCleanup(self._cleanup)
		self.prefix = "BATCH-PROD-" + frappe.generate_hash(length=8)
		self.stores, self.wip, self.other = [self._warehouse(label) for label in ("Stores", "WIP", "Other")]
		for field, value in {
			"enable_serial_and_batch_no_for_item": 1,
			"enable_stock_reservation": 1,
			"auto_reserve_serial_and_batch": 0,
			"auto_create_serial_and_batch_bundle_for_outward": 0,
		}.items():
			frappe.db.set_single_value("Stock Settings", field, value)
		frappe.db.set_single_value("Manufacturing Settings", "make_serial_no_batch_from_work_order", 0)
		frappe.db.set_single_value("Manufacturing Settings", "backflush_raw_materials_based_on", "Material Transferred for Manufacture")
		frappe.db.set_value("Company", self.COMPANY, "default_wip_warehouse", self.wip)
		for doctype in ("Stock Settings", "Manufacturing Settings"):
			frappe.clear_document_cache(doctype, doctype)
		frappe.clear_document_cache("Company", self.COMPANY)
		self.raw, self.semi, self.fg = [self._item(label) for label in ("RAW", "SEMI", "FG")]
		self.semi_bom = self._bom(self.semi, self.raw)
		self.fg_bom = self._bom(self.fg, self.semi)

	def _cleanup(self):
		frappe.db.rollback()
		frappe.clear_document_cache("Company", self.COMPANY)
		for doctype in ("Stock Settings", "Manufacturing Settings"):
			frappe.clear_document_cache(doctype, doctype)

	def _warehouse(self, label):
		return frappe.get_doc(dict(doctype="Warehouse", warehouse_name=self.prefix + "-" + label,
			company=self.COMPANY)).insert().name

	def _item(self, label):
		# Tracking is configured before the first stock transaction for this item.
		return frappe.get_doc(dict(doctype="Item", item_code=self.prefix + "-" + label,
			item_name=self.prefix + "-" + label, item_group="All Item Groups", stock_uom="Nos",
			is_stock_item=1, is_purchase_item=1, is_sales_item=1, has_batch_no=1,
			create_new_batch=0, valuation_rate=5)).insert().name

	def _bom(self, item, component):
		return frappe.get_doc(dict(doctype="BOM", item=item, company=self.COMPANY, quantity=1,
			is_active=1, is_default=1, currency="INR", default_source_warehouse=self.stores,
			items=[dict(item_code=component, qty=1, rate=5)])).insert().submit().name

	def _batch(self, item, label):
		return frappe.get_doc(dict(doctype="Batch", item=item,
			batch_id=self.prefix + "-" + label)).insert().name

	def _stock(self, item, qty, batch):
		return make_stock_entry(item_code=item, qty=qty, to_warehouse=self.stores,
			company=self.COMPANY, basic_rate=5, batch_no=batch, use_serial_batch_fields=1)

	def _work_order(self, item, bom, qty, *, target=None):
		values = dict(doctype="Work Order", company=self.COMPANY, production_item=item,
			bom_no=bom, qty=qty, use_multi_level_bom=0, source_warehouse=self.stores,
			wip_warehouse=self.wip, fg_warehouse=self.stores, planned_start_date=now_datetime(),
			transfer_material_against="Work Order", reserve_stock=0)
		if target:
			values.update(custom_replenishes_work_order=target.name,
				custom_replenishes_work_order_item=target.required_items[0].name)
		order = frappe.get_doc(values)
		order.get_items_and_operations_from_bom()
		return order.insert().submit()

	def _set_batches(self, entry, row, batches):
		outward = bool(row.s_warehouse)
		bundle = SerialBatchCreation(dict(
			item_code=row.item_code, warehouse=row.s_warehouse or row.t_warehouse,
			voucher_type="Stock Entry", voucher_no=entry.name, voucher_detail_no=row.name,
			total_qty=sum(batches.values()) * (-1 if outward else 1), batches=frappe._dict(batches),
			type_of_transaction="Outward" if outward else "Inward", company=self.COMPANY,
			do_not_submit=True,
		)).make_serial_and_batch_bundle()
		row.serial_and_batch_bundle = bundle.name
		row.batch_no = None
		row.serial_no = None
		row.use_serial_batch_fields = 0

	def _lots(self, entry, *, outward=False):
		result = defaultdict(float)
		entry.reload()
		for row in entry.items:
			if bool(row.s_warehouse) != outward:
				continue
			if row.serial_and_batch_bundle:
				bundle = frappe.get_doc("Serial and Batch Bundle", row.serial_and_batch_bundle)
				for child in bundle.entries:
					result[child.batch_no] += abs(flt(child.qty))
			elif row.batch_no:
				result[row.batch_no] += flt(row.transfer_qty)
		return dict(result)

	def _issue(self, order, batches):
		request = service.request_material_issue(order.name)
		entry = frappe.get_doc("Stock Entry", request["stock_entry"])
		for row in entry.items:
			if row.s_warehouse:
				self._set_batches(entry, row, batches)
		entry.save().submit()
		order.reload()
		return entry

	def _manufacture(self, order, batch, *, inputs=None, qty=None):
		if qty is None:
			request = service.request_manufacture(order.name)
			entry = frappe.get_doc("Stock Entry", request["stock_entry"])
		else:
			entry = service._insert_guided_stock_entry(order, "Manufacture", qty, service.RECEIPT_ACTION)
		remaining_inputs = dict(inputs or {})
		for row in entry.items:
			if row.is_finished_item:
				self._set_batches(entry, row, {batch: flt(row.transfer_qty)})
			elif inputs is not None and row.s_warehouse:
				# Native transferred-material backflush can split the same item into
				# one row per batch. Keep those identities and never put the whole
				# five-unit input bundle onto each two-/three-unit row.
				if row.serial_and_batch_bundle:
					selected = {child.batch_no: abs(flt(child.qty)) for child in
						frappe.get_doc("Serial and Batch Bundle", row.serial_and_batch_bundle).entries}
				elif row.batch_no:
					selected = {row.batch_no: flt(row.transfer_qty)}
				else:
					selected, remaining = {}, flt(row.transfer_qty)
					for batch_no, available in remaining_inputs.items():
						take = min(max(available, 0), remaining)
						if take > 0:
							selected[batch_no] = take
							remaining -= take
					self.assertAlmostEqual(remaining, 0)
					self._set_batches(entry, row, selected)
				for batch_no, consumed in selected.items():
					remaining_inputs[batch_no] = remaining_inputs.get(batch_no, 0) - consumed
		if inputs is not None:
			self.assertTrue(all(abs(qty) < 1e-8 for qty in remaining_inputs.values()), remaining_inputs)
		entry.save().submit()
		order.reload()
		return entry

	def _source_reservations(self, entry):
		return [frappe.get_doc("Stock Reservation Entry", name) for name in frappe.get_all(
			"Stock Reservation Entry", filters={"docstatus": 1, "from_voucher_type": "Stock Entry",
				"from_voucher_no": entry.name}, pluck="name", limit=0)]

	def _actual(self, item, warehouse):
		return flt(frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty"))

	def test_quantity_request_then_native_two_batch_issue_manufacture_and_cancel(self):
		a, b = self._batch(self.raw, "RAW-A"), self._batch(self.raw, "RAW-B")
		self._stock(self.raw, 2, a)
		self._stock(self.raw, 3, b)
		order = self._work_order(self.semi, self.semi_bom, 5)
		request = service.request_material_issue(order.name)
		entry = frappe.get_doc("Stock Entry", request["stock_entry"])
		reservations = self._source_reservations(entry)
		self.assertEqual(sum(row.reserved_qty for row in reservations), 5)
		self.assertTrue(all(not row.sb_entries for row in reservations))
		self._set_batches(entry, entry.items[0], {a: 2, b: 3})
		entry.save().submit()
		self.assertEqual(self._lots(entry, outward=True), {a: 2, b: 3})
		finished = self._batch(self.semi, "SEMI-A")
		receipt = self._manufacture(order, finished, inputs={a: 2, b: 3})
		self.assertEqual(self._lots(receipt, outward=True), {a: 2, b: 3})
		self.assertEqual(self._lots(receipt), {finished: 5})
		self.assertEqual(self._actual(self.semi, self.stores), 5)
		receipt.cancel()
		self.assertEqual(self._actual(self.semi, self.stores), 0)
		self.assertEqual(self._actual(self.raw, self.wip), 5)
		entry.cancel()
		self.assertEqual(self._actual(self.raw, self.wip), 0)
		self.assertEqual(self._actual(self.raw, self.stores), 5)
		self.assertEqual(get_stock_snapshot(self.raw, self.stores).available_qty, 5)

	def test_directed_semi_receipt_reserves_needed_lot_and_leaves_surplus_shared(self):
		raw_batch = self._batch(self.raw, "RAW")
		self._stock(self.raw, 6, raw_batch)
		target = self._work_order(self.fg, self.fg_bom, 4)
		supply = self._work_order(self.semi, self.semi_bom, 6, target=target)
		self._issue(supply, {raw_batch: 6})
		semi_batch = self._batch(self.semi, "SEMI")
		receipt = self._manufacture(supply, semi_batch, inputs={raw_batch: 6})
		reservations = self._source_reservations(receipt)
		self.assertEqual(sum(row.reserved_qty for row in reservations), 4)
		self.assertEqual({row.voucher_no for row in reservations}, {target.name})
		self.assertEqual([(child.batch_no, child.qty) for row in reservations for child in row.sb_entries], [(semi_batch, 4)])
		self.assertEqual(get_stock_snapshot(self.semi, self.stores).available_qty, 2)
		self.assertEqual(service.reserve_replenishment_output(receipt).reserved_qty, 0)
		# A separate order can use the ordinary surplus; the original target owns 4.
		other = self._work_order(self.fg, self.fg_bom, 2)
		other_issue = self._issue(other, {semi_batch: 2})
		self.assertEqual(self._actual(self.semi, self.stores), 4)
		self.assertEqual(sum(row.reserved_qty for row in self._source_reservations(receipt)), 4)
		other_issue.cancel()
		target_issue = self._issue(target, {semi_batch: 4})
		self.assertEqual(sum(row.transferred_qty for row in self._source_reservations(receipt)), 4)
		fg_batch = self._batch(self.fg, "FG")
		fg_receipt = self._manufacture(target, fg_batch, inputs={semi_batch: 4})
		self.assertEqual(self._lots(fg_receipt, outward=True), {semi_batch: 4})
		self.assertEqual(self._lots(fg_receipt), {fg_batch: 4})
		self.assertEqual(self._actual(self.fg, self.stores), 4)
		fg_receipt.cancel()
		target_issue.cancel()
		self.assertEqual(sum(row.transferred_qty for row in self._source_reservations(receipt)), 0)
		self.assertEqual(get_stock_snapshot(self.semi, self.stores).available_qty, 2)
		receipt.cancel()
		self.assertEqual(self._source_reservations(receipt), [])
		self.assertEqual(self._actual(self.semi, self.stores), 0)

	def test_moved_source_batch_cannot_claim_other_batch_stock(self):
		raw_batch = self._batch(self.raw, "RAW")
		self._stock(self.raw, 2, raw_batch)
		supply = self._work_order(self.semi, self.semi_bom, 2)
		self._issue(supply, {raw_batch: 2})
		a, b = self._batch(self.semi, "SEMI-A"), self._batch(self.semi, "SEMI-B")
		receipt = self._manufacture(supply, a, inputs={raw_batch: 2})
		move = make_stock_entry(item_code=self.semi, qty=2, company=self.COMPANY,
			from_warehouse=self.stores, to_warehouse=self.other, batch_no=a, use_serial_batch_fields=1)
		self._stock(self.semi, 2, b)
		target = self._work_order(self.fg, self.fg_bom, 2)
		source = next(row for row in receipt.items if row.is_finished_item)
		kwargs = dict(work_order=target, work_order_item=target.required_items[0], qty=2,
			from_stock_entry=receipt.name, from_detail=source.name, source_detail_doc=source)
		self.assertIsNone(service._new_work_order_reservation(**kwargs))
		self.assertEqual(get_stock_snapshot(self.semi, self.stores).available_qty, 2)
		move.cancel()
		reservation = service._new_work_order_reservation(**kwargs)
		self.assertEqual([(row.batch_no, row.qty) for row in reservation.sb_entries], [(a, 2)])
		self.assertEqual(get_stock_snapshot(self.semi, self.stores).available_qty, 2)
		reservation.reload().cancel()
		receipt.cancel()
		self.assertEqual(self._actual(self.semi, self.stores), 2)

	def test_partial_completion_return_and_reissue_keep_lot_quantities(self):
		# Exercise native bundle generation too; correction must discard its
		# obsolete draft bundles, not leave quantities linked to the old rows.
		frappe.db.set_single_value("Stock Settings", "use_serial_batch_fields", 0)
		frappe.clear_document_cache("Stock Settings", "Stock Settings")
		a, b = self._batch(self.raw, "RAW-A"), self._batch(self.raw, "RAW-B")
		self._stock(self.raw, 5, a)
		order = self._work_order(self.semi, self.semi_bom, 5)
		issue = self._issue(order, {a: 5})
		first_batch, second_batch = self._batch(self.semi, "SEMI-1"), self._batch(self.semi, "SEMI-2")
		first = self._manufacture(order, first_batch, inputs={a: 2}, qty=2)
		returned = frappe.get_doc(dict(doctype="Stock Entry", company=self.COMPANY,
			stock_entry_type="Material Transfer for Manufacture", work_order=order.name,
			is_return=1, custom_return_source_warehouse=self.stores,
			items=[dict(item_code=self.raw, qty=1, s_warehouse=self.wip,
				t_warehouse=self.stores, batch_no=a, use_serial_batch_fields=1)]
		)).insert().submit()
		self.assertEqual(self._actual(self.raw, self.wip), 2)
		self._stock(self.raw, 1, b)
		reissue = self._issue(order, {b: 1})
		self.assertEqual(reissue.fg_completed_qty, 0)
		self.assertEqual(self._lots(reissue, outward=True), {b: 1})
		second = self._manufacture(order, second_batch, inputs={a: 2, b: 1})
		self.assertEqual(self._lots(second, outward=True), {a: 2, b: 1})
		self.assertEqual(self._actual(self.semi, self.stores), 5)
		self.assertEqual(self._actual(self.raw, self.stores), 1)
		second.cancel()
		self.assertEqual(self._actual(self.semi, self.stores), 2)
		self.assertEqual(self._lots(first), {first_batch: 2})
		reissue.cancel()
		returned.cancel()
		first.cancel()
		issue.cancel()
		self.assertEqual(self._actual(self.raw, self.stores), 6)
		self.assertEqual(self._actual(self.raw, self.wip), 0)
		self.assertEqual(self._actual(self.semi, self.stores), 0)
