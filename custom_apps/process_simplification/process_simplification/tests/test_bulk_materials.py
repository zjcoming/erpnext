from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from process_simplification.production_workflow import bulk_materials as bulk


class TestBulkMaterials(TestCase):
	def setUp(self):
		self.stack = ExitStack()
		self.addCleanup(self.stack.close)
		def throw(message, exc=frappe.ValidationError, *args, **kwargs):
			raise exc(message)
		self.stack.enter_context(patch.object(frappe, "throw", side_effect=throw))

	def test_disabled_module_never_reads_or_writes(self):
		with patch.object(frappe, "conf", frappe._dict()), patch.object(bulk, "_work_order") as lookup:
			with self.assertRaises(frappe.PermissionError):
				bulk.request_material("WO", {}, 5)
			lookup.assert_not_called()

	def test_warehouse_role_cannot_request_manager_cannot_submit(self):
		with patch.object(frappe, "conf", frappe._dict(enable_operation_dispatch_pool=1)), patch.object(bulk, "user_company_scope", return_value={"C"}):
			for allowed, denied in ((bulk.CAPABILITY_WAREHOUSE_WORKBENCH, "request"), (bulk.CAPABILITY_PRODUCTION_REVIEW, "confirm")):
				with self.subTest(allowed=allowed), patch.object(bulk, "user_has_capability", side_effect=lambda capability: capability == allowed):
					with self.assertRaises(frappe.PermissionError):
						bulk._access(denied)
					self.assertEqual(bulk._access()[0], "confirm" if denied == "request" else "request")

	def test_empty_or_foreign_company_scope_is_rejected(self):
		with patch.object(frappe, "conf", frappe._dict(enable_operation_dispatch_pool=1)), patch.object(bulk, "user_has_capability", return_value=True):
			for scope in (set(), {"Other"}):
				with patch.object(bulk, "user_company_scope", return_value=scope):
					with self.assertRaises(frappe.PermissionError):
						bulk._access("request", "C")

	def test_names_are_bounded_unique_and_structured(self):
		for value in ([], "{}", ["WO", "WO"], ["WO"] * 21, [None]):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				bulk._names(value)
		self.assertEqual(bulk._names('["WO"]'), ["WO"])

	def request_fixture(self):
		self.stack.enter_context(patch.object(bulk, "_access", return_value=("request", {}, {"C"})))
		work_order = self.stack.enter_context(patch.object(bulk, "_work_order", return_value=frappe._dict(name="WO", company="C")))
		self.stack.enter_context(patch.object(bulk.service, "load_work_order_stock_facts", return_value={}))
		self.stack.enter_context(patch.object(bulk, "_current_stock_facts", return_value={}))
		self.stack.enter_context(patch.object(bulk, "_request_state", return_value="current"))
		existing = self.stack.enter_context(patch.object(bulk, "_existing_draft", return_value=None))
		write = self.stack.enter_context(patch.object(bulk.service, "request_material_issue", return_value={"stock_entry": "STE"}))
		return work_order, existing, write

	def test_request_preserves_original_priority_and_partial_gate(self):
		work_order, _, write = self.request_fixture()
		bulk.request_material("WO", {"work_order": "current"}, 4, allow_partial=1)
		work_order.assert_called_once_with("WO", {"C"}, lock=True)
		write.assert_called_once_with("WO", qty=4, allow_partial=1, override_priority=0)

	def test_changed_work_order_or_issued_quantity_cannot_reach_write(self):
		_, _, write = self.request_fixture()
		with self.assertRaisesRegex(frappe.ValidationError, "已变化"):
			bulk.request_material("WO", {"work_order": "old"}, 4)
		write.assert_not_called()

	def test_nan_and_infinite_quantities_rejected(self):
		_, _, write = self.request_fixture()
		for value in (None, "nan", "inf", 0, -1):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				bulk.request_material("WO", {"work_order": "current"}, value)
		write.assert_not_called()

	def test_retry_reuses_equal_original_draft_without_creating_another(self):
		_, existing, write = self.request_fixture()
		existing.return_value = frappe._dict(name="STE", custom_process_coverage_qty=4)
		with patch.object(bulk.service, "validate_guided_stock_entry_before_submit") as guard:
			self.assertTrue(bulk.request_material("WO", {"work_order": "current", "draft": None}, 4)["reused"])
			guard.assert_called_once_with(existing.return_value)
		write.assert_not_called()

	def test_modified_draft_and_different_existing_quantity_require_new_preview(self):
		_, existing, write = self.request_fixture()
		existing.return_value = frappe._dict(custom_process_coverage_qty=4)
		with patch.object(bulk, "_draft_state", return_value="new"):
			with self.assertRaisesRegex(frappe.ValidationError, "修改"):
				bulk.request_material("WO", {"work_order": "current", "draft": "old"}, 4)
			with self.assertRaisesRegex(frappe.ValidationError, "数量"):
				bulk.request_material("WO", {"work_order": "current", "draft": None}, 5)
		write.assert_not_called()

	def submit_fixture(self, *, docstatus=0):
		self.stack.enter_context(patch.object(bulk, "_access", return_value=("confirm", {}, {"C"})))
		doc = MagicMock(name="stock_entry")
		doc.name, doc.docstatus, doc.work_order = "STE", docstatus, "WO"
		self.stack.enter_context(patch.object(bulk, "_stock_entry", return_value=doc))
		permissions = self.stack.enter_context(patch.object(frappe, "has_permission", return_value=True))
		self.stack.enter_context(patch.object(bulk, "_draft_state", return_value="current"))
		batch = self.stack.enter_context(patch.object(bulk, "_requires_native_confirmation", return_value=False))
		self.stack.enter_context(patch.object(bulk, "_work_order", return_value=frappe._dict(name="WO")))
		self.stack.enter_context(patch.object(bulk, "_current_stock_facts", return_value={}))
		self.stack.enter_context(patch.object(bulk.service, "load_work_order_stock_facts", return_value={}))
		self.stack.enter_context(patch.object(bulk, "_request_state", return_value="current"))
		guard = self.stack.enter_context(patch.object(bulk.service, "validate_guided_stock_entry_before_submit"))
		stock = self.stack.enter_context(patch.object(bulk, "_assert_physical_stock"))
		return doc, permissions, batch, guard, stock

	def test_submit_checks_both_native_permissions_before_posting(self):
		doc, permissions, _, guard, stock = self.submit_fixture()
		bulk.submit_material("STE", "current")
		self.assertEqual([call.args[1] for call in permissions.call_args_list], ["write", "submit"])
		guard.assert_called_once_with(doc)
		stock.assert_called_once_with(doc, frappe._dict(name="WO"))
		doc.submit.assert_called_once_with()

	def test_modified_or_cancelled_document_cannot_post(self):
		doc, _, _, guard, _ = self.submit_fixture()
		with self.assertRaisesRegex(frappe.ValidationError, "修改"):
			bulk.submit_material("STE", "old")
		doc.docstatus = 2
		with self.assertRaises(frappe.ValidationError):
			bulk.submit_material("STE", "current")
		doc.submit.assert_not_called()
		guard.assert_not_called()

	def test_submitted_retry_has_no_second_stock_posting(self):
		doc, _, _, _, _ = self.submit_fixture(docstatus=1)
		self.assertTrue(bulk.submit_material("STE", "old")["reused"])
		doc.submit.assert_not_called()

	def test_batch_serial_confirmation_goes_through_native_form(self):
		doc, _, batch, _, _ = self.submit_fixture()
		batch.return_value = True
		with self.assertRaisesRegex(frappe.ValidationError, "批次"):
			bulk.submit_material("STE", "current")
		doc.submit.assert_not_called()

	def test_permissions_failure_does_not_get_native_ignore_flag(self):
		doc, permissions, _, _, _ = self.submit_fixture()
		permissions.side_effect = frappe.PermissionError
		with self.assertRaises(frappe.PermissionError):
			bulk.submit_material("STE", "current")
		doc.submit.assert_not_called()

	def test_physical_shortage_cannot_be_masked_by_owned_reservations(self):
		from process_simplification.production_workflow import stock_reservation
		entry = MagicMock()
		entry.items = [frappe._dict(item_code="RM", s_warehouse="Stores", transfer_qty=10)]
		order = frappe._dict(name="WO", required_items=[frappe._dict(item_code="RM", source_warehouse="Stores")])
		with patch.object(stock_reservation, "locked_available_qty", return_value=0), patch.object(frappe.db, "get_value", return_value=5), patch.object(bulk, "_current_owned_reservation_qty", return_value=10):
			with self.assertRaisesRegex(frappe.ValidationError, "库存"):
				bulk._assert_physical_stock(entry, order)

	def test_preview_cannot_combine_companies(self):
		with self.assertRaises(frappe.ValidationError):
			bulk._same_company([{"company": "A"}, {"company": "B"}])

	def test_draft_hash_includes_warehouses_posting_date_and_quantity(self):
		doc = MagicMock()
		data = {"posting_date": "2026-09-30", "items": [{"s_warehouse": "Stores", "qty": 10}]}
		doc.as_dict.return_value = data
		old = bulk._draft_state(doc)
		data["items"][0]["s_warehouse"] = "Other"
		self.assertNotEqual(old, bulk._draft_state(doc))
