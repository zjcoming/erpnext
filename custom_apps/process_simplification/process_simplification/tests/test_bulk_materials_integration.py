"""Native stock posting exercised only on a disposable ERPNext test site."""

import frappe
from frappe.utils import add_days, flt, nowdate

from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from process_simplification.production_workflow import bulk_materials as bulk
from process_simplification.tests import test_operation_dispatch_pool_integration as fixtures


class TestBulkMaterialsIntegration(fixtures.TestOperationDispatchPoolIntegration):
	TEST_COMPANY = "Bulk Materials Test Company"
	TEST_COMPANY_ABBR = "BMT"
	OTHER_COMPANY = "Bulk Materials Other Company"
	OTHER_COMPANY_ABBR = "BMO"
	TEST_OPERATION = "Bulk Materials Test Operation"
	TEST_WORKSTATION = "Bulk Materials Test Workstation"
	TEST_FINISHED_GOOD = "BMT-FG-001"
	TEST_RAW_MATERIAL = "BMT-RM-001"

	def _fund(self, qty):
		return make_stock_entry(item_code=self.TEST_RAW_MATERIAL, to_warehouse=self.source_warehouse,
			company=self.TEST_COMPANY, qty=qty, basic_rate=1,
			posting_date=add_days(nowdate(), -1), posting_time="12:00:00")

	def _preview(self, *cards):
		return bulk.preview_material_requests([card.work_order for card in cards])["rows"]

	def _request(self, row):
		return bulk.request_material(row["work_order"], row["expected_state"], row["request_qty"], int(row["partial"]))

	def _warehouse_user(self):
		user = self._make_user("Process Simplification Warehouse Operator")
		self._grant_company(user, self.TEST_COMPANY)
		return user

	def _actual(self, warehouse):
		return flt(frappe.db.get_value("Bin", {"item_code": self.TEST_RAW_MATERIAL, "warehouse": warehouse}, "actual_qty"))

	def test_materials_preview_is_readonly_and_repeated_request_reuses_draft(self):
		card = self._planned_card()
		self._fund(10)
		before = self._business_snapshot(card)
		with self.set_user(self.supervisor):
			row = self._preview(card)[0]
			self.assertEqual(self._business_snapshot(card), before)
			self.assertEqual(row["request_qty"], 10)
			first = self._request(row)
			again = self._request(row)
		self.assertEqual(first["stock_entry"], again["stock_entry"])
		self.assertTrue(again["reused"])
		self.assertEqual(frappe.db.count("Stock Entry", {"work_order": card.work_order}), 1)
		self.assertEqual(frappe.db.count("Stock Ledger Entry", {"voucher_no": first["stock_entry"]}), 0)
		self.assertEqual(self._actual(self.source_warehouse), 10)

	def test_materials_two_work_orders_share_stock_without_over_reserving(self):
		first, second = self._planned_card(), self._planned_card()
		self._fund(15)
		with self.set_user(self.supervisor):
			rows = self._preview(first, second)
			self.assertEqual(sum(row["request_qty"] for row in rows), 15)
			by_name = {row["work_order"]: row for row in rows}
			self.assertEqual(by_name[first.work_order]["request_qty"], 10)
			self.assertEqual(by_name[second.work_order]["request_qty"], 5)
			self.assertTrue(by_name[second.work_order]["partial"])
			with self.assertRaises(frappe.ValidationError):
				bulk.request_material(second.work_order, by_name[second.work_order]["expected_state"], 5)
			entries = [self._request(row)["stock_entry"] for row in rows]
		reserved = sum(flt(row.reserved_qty) for row in frappe.get_all("Stock Reservation Entry",
			filters={"docstatus": 1, "from_voucher_no": ["in", entries]}, fields=["reserved_qty"]))
		self.assertEqual(reserved, 15)
		self.assertEqual(self._actual(self.source_warehouse), 15)

	def test_materials_warehouse_posts_native_ledger_once(self):
		card = self._planned_card()
		self._fund(10)
		warehouse = self._warehouse_user()
		with self.set_user(self.supervisor):
			entry = self._request(self._preview(card)[0])["stock_entry"]
			with self.assertRaises(frappe.PermissionError):
				bulk.submit_material(entry, "unused")
		with self.set_user(warehouse):
			with self.assertRaises(frappe.PermissionError):
				self._preview(card)
			row = bulk.preview_material_submissions([entry])["rows"][0]
			self.assertTrue(row["can_select"])
			bulk.submit_material(entry, row["expected_state"])
			self.assertTrue(bulk.submit_material(entry, row["expected_state"])["reused"])
		self.assertEqual(self._actual(self.source_warehouse), 0)
		self.assertEqual(self._actual(self.wip_warehouse), 10)
		movements = frappe.get_all("Stock Ledger Entry", filters={"voucher_no": entry, "is_cancelled": 0},
			fields=["warehouse", "actual_qty"])
		self.assertEqual({row.warehouse: flt(row.actual_qty) for row in movements},
			{self.source_warehouse: -10, self.wip_warehouse: 10})
		self.assertEqual(len(movements), 2)
		self.assertEqual(flt(frappe.db.get_value("Work Order", card.work_order, "material_transferred_for_manufacturing")), 10)

	def test_materials_changed_draft_requires_new_preview_without_posting(self):
		card = self._planned_card()
		self._fund(10)
		with self.set_user(self.supervisor):
			entry = self._request(self._preview(card)[0])["stock_entry"]
		warehouse = self._warehouse_user()
		with self.set_user(warehouse):
			old = bulk.preview_material_submissions([entry])["rows"][0]
			doc = frappe.get_doc("Stock Entry", entry)
			doc.remarks = "现场核对后补充说明"
			doc.save()
			with self.assertRaisesRegex(frappe.ValidationError, "修改"):
				bulk.submit_material(entry, old["expected_state"])
		self.assertEqual(frappe.db.get_value("Stock Entry", entry, "docstatus"), 0)
		self.assertEqual(frappe.db.count("Stock Ledger Entry", {"voucher_no": entry}), 0)
		self.assertEqual(self._actual(self.source_warehouse), 10)

	def test_materials_foreign_company_hidden_from_reads_and_writes(self):
		card = self._planned_card()
		self._fund(10)
		with self.set_user(self.supervisor):
			entry = self._request(self._preview(card)[0])["stock_entry"]
		foreign = self._make_user("Process Simplification Warehouse Operator")
		self._grant_company(foreign, self.OTHER_COMPANY)
		with self.set_user(foreign):
			self.assertEqual(bulk.get_material_workbench("confirm")["rows"], [])
			with self.assertRaises(frappe.PermissionError):
				bulk.preview_material_submissions([entry])
			with self.assertRaises(frappe.PermissionError):
				bulk.submit_material(entry, "unused")

	def test_materials_native_batch_flags_require_original_form(self):
		self.enterContext(self.change_settings("Stock Settings", {"enable_serial_and_batch_no_for_item": 1}))
		item = frappe.get_doc("Item", self.TEST_RAW_MATERIAL)
		item.has_batch_no = 1
		item.create_new_batch = 0
		item.save()
		card = self._planned_card()
		batch = frappe.get_doc({"doctype": "Batch", "item": item.name, "batch_id": "BMT-" + frappe.generate_hash(length=8)}).insert()
		make_stock_entry(item_code=item.name, to_warehouse=self.source_warehouse,
			company=self.TEST_COMPANY, qty=10, basic_rate=1, batch_no=batch.name)
		with self.set_user(self.supervisor):
			entry = self._request(self._preview(card)[0])["stock_entry"]
		warehouse = self._warehouse_user()
		with self.set_user(warehouse):
			row = bulk.preview_material_submissions([entry])["rows"][0]
			self.assertFalse(row["can_select"])
			self.assertTrue(row["requires_native"])
			self.assertIn("批次", row["block_message"])
			with self.assertRaisesRegex(frappe.ValidationError, "批次"):
				bulk.submit_material(entry, row["expected_state"])
		self.assertEqual(frappe.db.get_value("Stock Entry", entry, "docstatus"), 0)


# Reuse fixture lifecycle/builders without rerunning the parent's unrelated
# operation-dispatch acceptance tests as part of this inventory suite.
for _name in dir(fixtures.TestOperationDispatchPoolIntegration):
	if _name.startswith("test_"):
		setattr(TestBulkMaterialsIntegration, _name, None)
