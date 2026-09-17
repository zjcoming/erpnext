from types import SimpleNamespace
from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from process_simplification import batch_display, management_access
from process_simplification.management_access import (
	MANAGED_REPORT_ROLE_ADDITIONS, WAREHOUSE_OPERATOR_PERMISSIONS,
)


class TestBatchDisplay(TestCase):
	def setUp(self):
		self.permission = self.enterContext(patch.object(frappe, "has_permission", return_value=True))
		self.query = self.enterContext(patch.object(frappe, "get_all", return_value=[]))
		self.bundles = self.enterContext(patch.object(frappe, "get_list", return_value=[]))
		self.enterContext(patch.object(frappe, "throw", side_effect=frappe.PermissionError))

	def document(self, **item_fields):
		return frappe._dict(doctype="Purchase Receipt", name="PR-1", company="Factory A", items=[
			frappe._dict(name="PRI-1", item_code="RM", qty=3, stock_qty=30, stock_uom="Kg", uom="Box",
				warehouse="Accepted", **item_fields),
		])

	def bundle(self, name, warehouse="Accepted", **fields):
		return frappe._dict(name=name, item_code="RM", company="Factory A", warehouse=warehouse,
			voucher_type="Purchase Receipt", voucher_no="PR-1", voucher_detail_no="PRI-1", **fields)

	def test_denied_source_cannot_lookup_any_batch_data(self):
		self.permission.return_value = False
		with self.assertRaises(frappe.PermissionError):
			batch_display.document_batch_summary(self.document(serial_and_batch_bundle="PRIVATE"))
		self.bundles.assert_not_called()
		self.query.assert_not_called()

	def test_multiple_batches_keep_stock_units_and_rejected_lots_separate(self):
		doc = self.document(serial_and_batch_bundle="IN", rejected_serial_and_batch_bundle="REJECT", rejected_warehouse="Quarantine")
		self.bundles.return_value = [self.bundle("IN"), self.bundle("REJECT", "Quarantine")]
		self.query.return_value = [
			frappe._dict(parent="IN", batch_no="A1", qty=12.5),
			frappe._dict(parent="IN", batch_no="A2", qty=17.5),
			frappe._dict(parent="REJECT", batch_no="R1", qty=2.5),
		]
		result = batch_display.get_print_batch_summary(doc)["PRI-1"]
		self.assertEqual([(r["batch_no"], r["qty"], r["stock_uom"]) for r in result["batches"]],
			[("A1", 12.5, "Kg"), ("A2", 17.5, "Kg")])
		self.assertEqual(result["rejected_batches"][0]["batch_no"], "R1")

	def test_legacy_return_shows_absolute_batch_quantities_in_stock_units(self):
		doc = self.document(batch_no="A1", rejected_qty=-0.25, conversion_factor=10)
		doc.is_return = 1
		doc.get("items")[0].stock_qty = -30
		result = batch_display.get_print_batch_summary(doc)["PRI-1"]
		self.assertEqual(result["batches"], [{"batch_no": "A1", "qty": 30, "stock_uom": "Kg"}])
		self.assertEqual(result["rejected_batches"][0]["qty"], 2.5)
		self.query.assert_not_called()

	def test_invalid_or_unreadable_bundle_never_falls_back_to_stale_batch_field(self):
		doc = self.document(serial_and_batch_bundle="OTHER", batch_no="OLD")
		for field, value in [("company", "Foreign"), ("warehouse", "Foreign"), ("item_code", "OTHER"),
			("voucher_no", "PR-FOREIGN"), ("voucher_detail_no", "PRI-OTHER")]:
			bundle = self.bundle("OTHER")
			bundle[field] = value
			self.bundles.return_value = [bundle]
			self.query.reset_mock()
			result = batch_display.get_print_batch_summary(doc)["PRI-1"]
			self.assertEqual(result["batches"], [], field)
			self.query.assert_not_called()
		self.bundles.return_value = []
		self.assertEqual(batch_display.get_print_batch_summary(doc)["PRI-1"]["batches"], [])

	def test_plain_print_does_not_query_batch_data_or_item_settings(self):
		result = batch_display.get_print_batch_summary(self.document())["PRI-1"]
		self.assertFalse(result["has_batch_no"])
		self.query.assert_not_called()
		self.bundles.assert_not_called()

	def test_pending_batch_item_is_marked_without_requiring_automatic_numbering(self):
		self.query.return_value = ["RM"]
		result = batch_display.document_batch_summary(self.document())["PRI-1"]
		self.assertTrue(result["has_batch_no"])
		self.assertEqual(result["batches"], [])
		self.bundles.assert_not_called()

	def test_scoped_operator_has_no_new_report_or_master_data_write_grant(self):
		self.assertEqual(WAREHOUSE_OPERATOR_PERMISSIONS["Batch"], {"select", "create"})
		self.assertEqual(WAREHOUSE_OPERATOR_PERMISSIONS["Serial and Batch Bundle"], {"read", "select", "create", "write", "submit"})
		self.assertFalse({name for _, name in batch_display.BATCH_REPORTS}.intersection(MANAGED_REPORT_ROLE_ADDITIONS))
		for doctype in ("Stock Entry", "Purchase Receipt", "Delivery Note"):
			self.assertIn("print", WAREHOUSE_OPERATOR_PERMISSIONS[doctype])
			self.assertIn("print", management_access.OWNER_PERMISSIONS[doctype])
			self.assertFalse({"cancel", "delete", "report"}.intersection(WAREHOUSE_OPERATOR_PERMISSIONS[doctype]))

	def test_upgrade_adds_needed_batch_permissions_without_removing_customer_grants(self):
		with patch.object(management_access, "MANAGED_DOCUMENT_PERMISSIONS", {
			management_access.WAREHOUSE_OPERATOR_ROLE: {"Batch": {"select", "create"}},
		}), patch.object(frappe, "db", Mock(), create=True) as db, patch.object(frappe, "clear_cache"):
			db.exists.return_value = True
			db.get_value.return_value = "CUSTOM-BATCH-PERMISSION"
			management_access.ensure_management_document_permissions()
			self.assertEqual(db.set_value.call_args.args[2], {"select": 1, "create": 1})

	def test_new_native_batch_permissions_override_add_permission_defaults(self):
		for doctype in ("Batch", "Serial and Batch Bundle"):
			allowed = WAREHOUSE_OPERATOR_PERMISSIONS[doctype]
			with self.subTest(doctype=doctype), patch.object(management_access, "MANAGED_DOCUMENT_PERMISSIONS", {
				management_access.WAREHOUSE_OPERATOR_ROLE: {doctype: allowed},
			}), patch.object(frappe, "db", Mock(), create=True) as db, \
				patch.object(frappe, "clear_cache"), patch("frappe.permissions.add_permission") as add_permission:
				db.exists.return_value = True
				db.get_value.side_effect = [None, "NEW-NATIVE-PERMISSION"]
				management_access.ensure_management_document_permissions()
				add_permission.assert_called_once_with(doctype, management_access.WAREHOUSE_OPERATOR_ROLE,
					permlevel=0, ptype="read")
				updates = db.set_value.call_args.args[2]
				self.assertEqual(updates, {ptype: int(ptype in allowed)
					for ptype in management_access.DOCUMENT_PERMISSION_FIELDS})
				self.assertEqual(updates["read"], int(doctype != "Batch"))

	def test_repeated_management_permission_sync_installs_configuration_references_once(self):
		fields = {}
		def get_field(doctype, fieldname):
			return fields.setdefault((doctype, fieldname), frappe._dict(options="Warehouse", ignore_user_permissions=0))
		def set_property(doctype, fieldname, property_name, value, property_type):
			get_field(doctype, fieldname)[property_name] = value
		with ExitStack() as stack:
			for name in ("ensure_management_roles", "ensure_management_role_profiles",
				"ensure_management_document_permissions", "ensure_management_page_roles", "ensure_management_report_roles"):
				stack.enter_context(patch.object(management_access, name))
			stack.enter_context(patch.object(frappe, "get_meta", side_effect=lambda doctype:
				SimpleNamespace(get_field=lambda fieldname: get_field(doctype, fieldname))))
			setter = stack.enter_context(patch("frappe.custom.doctype.property_setter.property_setter.make_property_setter",
				side_effect=set_property))
			management_access.ensure_management_access()
			first_calls = setter.call_args_list[:]
			management_access.ensure_management_access()
			self.assertEqual(setter.call_args_list, first_calls)
			changed = {(call.args[0], call.args[1]) for call in first_calls}
			self.assertIn(("Stock Settings", "default_warehouse"), changed)
			self.assertIn(("Stock Settings", "sample_retention_warehouse"), changed)
			self.assertIn(("Company", "default_wip_warehouse"), changed)
			self.assertEqual({doctype for doctype, _ in changed}, {"Company", "Stock Settings"})
			self.assertTrue(all(call.args[2:] == ("ignore_user_permissions", 1, "Check") for call in first_calls))

	def test_scoped_manager_does_not_receive_full_batch_report_links(self):
		with patch.object(frappe, "session", SimpleNamespace(user="warehouse"), create=True), \
			patch.object(frappe, "get_roles", return_value=["Stock Manager"]), \
			patch.object(batch_display, "get_user_permissions", return_value={"Warehouse": ["Allowed"]}), \
			patch.object(frappe, "get_doc") as report:
			self.assertEqual(batch_display.batch_navigation()["reports"], [])
			report.assert_not_called()

	def test_historical_batch_records_keep_allowed_native_report_links_with_setting_off(self):
		with patch.object(frappe, "session", SimpleNamespace(user="Administrator"), create=True), \
			patch.object(batch_display, "get_user_permissions", return_value={}), \
			patch.object(frappe, "db", Mock(), create=True) as db, \
			patch.object(frappe, "get_doc", return_value=SimpleNamespace(disabled=0, ref_doctype="Stock Ledger Entry", is_permitted=lambda: True)):
			db.get_single_value.return_value = 0
			db.exists.return_value = "HISTORICAL"
			self.assertEqual(len(batch_display.batch_navigation()["reports"]), 3)
			db.set_single_value.assert_not_called()
