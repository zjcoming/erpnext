from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from process_simplification import management_access, stock_permissions


class TestBatchBundlePermissions(TestCase):
	def setUp(self):
		self.enterContext(patch.object(frappe, "session", SimpleNamespace(user="warehouse"), create=True))
		self.roles = self.enterContext(patch.object(frappe, "get_roles", return_value=[management_access.WAREHOUSE_OPERATOR_ROLE]))
		self.enterContext(patch.object(management_access, "user_company_scope", return_value={"Factory A"}))
		self.permission = self.enterContext(patch.object(frappe, "has_permission", return_value=True))
		self.db = self.enterContext(patch.object(frappe, "db", Mock(), create=True))
		self.db.exists.return_value = False
		self.enterContext(patch.object(frappe, "throw", side_effect=frappe.PermissionError))

	def document(self, *, previous=None, new=False, **values):
		doc = frappe._dict(doctype="Serial and Batch Bundle", name="BUNDLE", company="Factory A", warehouse="Allowed",
			voucher_type="Stock Entry", voucher_no="STE-1", docstatus=0)
		doc.update(values)
		doc.get_doc_before_save = lambda: previous
		doc.is_new = lambda: new
		return doc

	def test_new_parent_not_yet_inserted_still_checks_bundle_scope(self):
		doc = self.document(new=True, voucher_no="new-stock-entry-1")
		stock_permissions.validate_batch_bundle_scope(doc)
		self.permission.assert_called_once_with("Serial and Batch Bundle", "create", doc=doc, user="warehouse")

	def test_missing_company_uses_warehouse_company_for_native_generation(self):
		self.db.get_value.return_value = "Factory A"
		for roles in (["System Manager"], ["Stock User"], [management_access.WAREHOUSE_OPERATOR_ROLE]):
			self.roles.return_value = roles
			doc = self.document(new=True, company=None)
			stock_permissions.validate_batch_bundle_scope(doc)
			self.assertEqual(doc.company, "Factory A")
		self.db.get_value.assert_called_with("Warehouse", "Allowed", "company")

	def test_explicit_company_is_never_replaced_from_warehouse(self):
		self.roles.return_value = ["Stock User"]
		doc = self.document(company="Explicit company")
		stock_permissions.validate_batch_bundle_scope(doc)
		self.assertEqual(doc.company, "Explicit company")
		self.db.get_value.assert_not_called()

	def test_derived_company_does_not_bypass_company_or_warehouse_scope(self):
		self.db.get_value.return_value = "Foreign"
		with self.assertRaises(frappe.PermissionError):
			stock_permissions.validate_batch_bundle_scope(self.document(new=True, company=None))
		self.db.get_value.return_value = "Factory A"
		self.permission.return_value = False
		with self.assertRaises(frappe.PermissionError):
			stock_permissions.validate_batch_bundle_scope(self.document(new=True, company=None, warehouse="Foreign"))

	def test_existing_parent_requires_current_write_permission(self):
		self.db.exists.return_value = True
		self.permission.side_effect = lambda doctype, *args, **kw: doctype != "Stock Entry"
		with self.assertRaises(frappe.PermissionError):
			stock_permissions.validate_batch_bundle_scope(self.document())

	def test_current_company_and_warehouse_cannot_be_outside_scope(self):
		with self.assertRaises(frappe.PermissionError):
			stock_permissions.validate_batch_bundle_scope(self.document(company="Foreign"))
		self.permission.return_value = False
		with self.assertRaises(frappe.PermissionError):
			stock_permissions.validate_batch_bundle_scope(self.document())

	def test_native_update_rpc_cannot_relabel_a_foreign_bundle_into_allowed_warehouse(self):
		from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import update_serial_batch_no_ledgers

		previous = self.document(warehouse="Foreign")
		doc = self.document(previous=previous)
		doc.set = lambda field, value: doc.update({field: value})
		doc.append = lambda field, value: doc.get(field).append(value)
		doc.save = Mock(side_effect=lambda **kwargs: stock_permissions.validate_batch_bundle_scope(doc))
		self.permission.side_effect = lambda doctype, ptype, doc=None, **kw: doc is None or doc.get("warehouse") != "Foreign"
		with patch.object(frappe, "get_doc", return_value=doc), \
			patch("erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle.combine_datetime", return_value="2026-09-14 10:00:00"):
			with self.assertRaises(frappe.PermissionError):
				update_serial_batch_no_ledgers("BUNDLE", [{"batch_no": "A1", "qty": 1}],
					frappe._dict(name="DETAIL"), {"posting_date": "2026-09-14", "posting_time": "10:00:00"}, warehouse="Allowed")
		doc.save.assert_called_once_with(ignore_permissions=True)

	def test_native_manager_and_unrelated_roles_keep_existing_native_behavior(self):
		for roles in (["System Manager", management_access.WAREHOUSE_OPERATOR_ROLE], ["Stock User"]):
			self.roles.return_value = roles
			self.permission.reset_mock()
			stock_permissions.validate_batch_bundle_scope(self.document(company="Other"))
			self.permission.assert_not_called()
