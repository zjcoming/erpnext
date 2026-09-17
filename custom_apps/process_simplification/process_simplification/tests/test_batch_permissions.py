"""Read-boundary tests for the native batch selector, without a database."""

import unittest
from unittest.mock import MagicMock, patch

import frappe

from process_simplification import batch_permissions as permissions


class TestBatchReadPermissions(unittest.TestCase):
	def setUp(self):
		self.scope = frappe._dict(user="warehouse@example.test", companies={"CO"})
		self.docs = {
			("Warehouse", "ALLOWED"): frappe._dict(doctype="Warehouse", name="ALLOWED", company="CO"),
			("Warehouse", "FORBIDDEN"): frappe._dict(doctype="Warehouse", name="FORBIDDEN", company="CO"),
			("Warehouse", "OTHER-COMPANY"): frappe._dict(doctype="Warehouse", name="OTHER-COMPANY", company="OTHER"),
			("Delivery Note", "DN"): frappe._dict(doctype="Delivery Note", name="DN", company="CO"),
			("Delivery Note", "PRIVATE-DN"): frappe._dict(doctype="Delivery Note", name="PRIVATE-DN", company="CO"),
		}
		self.native = self._patch("_native_call", return_value=[])
		self._patch("_scope", return_value=self.scope)
		self._patch_frappe("get_doc", side_effect=lambda doctype, name: self.docs[(doctype, name)])
		self._patch_frappe("has_permission", side_effect=lambda doctype, ptype, doc, user:
			doc.name not in {"FORBIDDEN", "PRIVATE-DN"})
		self.db = self._patch_frappe("db", MagicMock(exists=lambda doctype, name: (doctype, name) in self.docs))
		self._patch_frappe("throw", side_effect=lambda message, exception: self._raise(exception(message)))

	@staticmethod
	def _raise(error):
		raise error

	def _patch(self, name, **kwargs):
		mock = patch.object(permissions, name, **kwargs)
		self.addCleanup(mock.stop)
		return mock.start()

	def _patch_frappe(self, name, value=None, **kwargs):
		mock = patch.object(frappe, name, value, **kwargs) if value is not None else patch.object(frappe, name, **kwargs)
		self.addCleanup(mock.stop)
		return mock.start()

	def bundle(self, name="BUNDLE", warehouse="ALLOWED", voucher="DN", owner=None, **kwargs):
		doc = frappe._dict(doctype="Serial and Batch Bundle", name=name, warehouse=warehouse,
			company="CO", item_code="ITEM", owner=owner or self.scope.user, docstatus=0,
			voucher_type="Delivery Note", voucher_no=voucher,
			entries=[frappe._dict(warehouse=warehouse, batch_no="BATCH", qty=2)], **kwargs)
		self.docs[(doc.doctype, name)] = doc
		return doc

	def test_explicit_forbidden_warehouse_is_rejected_before_quantity_query(self):
		with self.assertRaises(frappe.PermissionError):
			permissions.get_batch_qty(item_code="ITEM", warehouse="FORBIDDEN")
		self.native.assert_not_called()

	def test_company_scope_applies_even_when_native_document_permission_would_allow(self):
		with self.assertRaises(frappe.PermissionError):
			permissions.get_batch_qty(item_code="ITEM", warehouse="OTHER-COMPANY")
		self.native.assert_not_called()

	def test_missing_warehouse_cannot_enumerate_all_stock(self):
		for warehouse in (None, "", []):
			with self.subTest(warehouse=warehouse), self.assertRaises(frappe.PermissionError):
				permissions.get_batch_qty(batch_no="BATCH", warehouse=warehouse)
		self.native.assert_not_called()

	def test_all_requested_warehouses_must_be_authorized(self):
		with self.assertRaises(frappe.PermissionError):
			permissions.get_batch_qty(item_code="ITEM", warehouse=["ALLOWED", "FORBIDDEN"])
		self.native.assert_not_called()

	def test_allowed_quantity_keeps_native_arguments_and_result(self):
		self.native.return_value = 3.5
		result = permissions.get_batch_qty(batch_no="BATCH", warehouse="ALLOWED", item_code="ITEM",
			posting_date="2026-09-14", posting_time="11:45:00", ignore_reserved_stock=True)
		self.assertEqual(result, 3.5)
		self.assertEqual(self.native.call_args.args[0], f"{permissions.NATIVE_BATCH}.get_batch_qty")
		self.assertEqual(self.native.call_args.kwargs["posting_date"], "2026-09-14")
		self.assertTrue(self.native.call_args.kwargs["ignore_reserved_stock"])

	def test_unrestricted_native_user_keeps_missing_warehouse_contract(self):
		with patch.object(permissions, "_scope", return_value=None):
			self.native.return_value = [{"warehouse": "FORBIDDEN", "qty": 9}]
			self.assertEqual(permissions.get_batch_qty(batch_no="BATCH"), self.native.return_value)

	def test_inward_global_batch_names_do_not_expose_global_quantity(self):
		self.native.side_effect = [[("LOCAL", 7, "Local batch")], [("GLOBAL", 912), ("EMPTY", 0)]]
		filters = {"warehouse": "ALLOWED", "item_code": "ITEM", "is_inward": True}
		rows = permissions.get_batch_no("Batch", "", "name", 0, 20, filters)
		self.assertEqual(rows, [("LOCAL", 7, "Local batch"), ("GLOBAL", 0), ("EMPTY", 0)])
		self.assertFalse(self.native.call_args_list[0].args[-1]["is_inward"])
		self.assertTrue(filters["is_inward"])

	def test_batch_candidates_require_warehouse_before_native_query(self):
		with self.assertRaises(frappe.PermissionError):
			permissions.get_batch_no("Batch", "", "name", 0, 20, {"item_code": "ITEM"})
		self.native.assert_not_called()

	def test_auto_candidates_check_actual_sre_warehouse_not_only_supplied_warehouse(self):
		rows = [frappe._dict(company="CO", warehouse="FORBIDDEN", item_code="ITEM", voucher_type="Delivery Note", voucher_no="DN")]
		with patch.object(frappe, "get_all", return_value=rows), self.assertRaises(frappe.PermissionError):
			permissions.get_auto_data(item_code="ITEM", warehouse="ALLOWED", has_batch_no=1, scio_detail="SOURCE")
		self.native.assert_not_called()

	def test_auto_candidates_require_original_reservation_voucher_read(self):
		rows = [frappe._dict(company="CO", warehouse="ALLOWED", item_code="ITEM", voucher_type="Delivery Note", voucher_no="PRIVATE-DN")]
		with patch.object(frappe, "get_all", return_value=rows), self.assertRaises(frappe.PermissionError):
			permissions.get_auto_data(item_code="ITEM", warehouse="ALLOWED", has_batch_no=1, scio_detail="SOURCE")
		self.native.assert_not_called()

	def test_auto_candidates_cannot_return_an_unchecked_child_warehouse(self):
		self.native.return_value = [frappe._dict(batch_no="BATCH", warehouse="FORBIDDEN", qty=12)]
		with self.assertRaises(frappe.PermissionError):
			permissions.get_auto_data(item_code="ITEM", warehouse="ALLOWED", has_batch_no=1)

	def test_auto_candidates_require_the_requested_sales_order_to_be_readable(self):
		self.docs[("Sales Order", "PRIVATE-DN")] = frappe._dict(doctype="Sales Order", name="PRIVATE-DN", company="CO")
		with self.assertRaises(frappe.PermissionError):
			permissions.get_auto_data(item_code="ITEM", warehouse="ALLOWED", has_batch_no=1, against_sales_order="PRIVATE-DN")
		self.native.assert_not_called()

	def test_bundle_header_warehouse_does_not_bypass_original_voucher_access(self):
		self.bundle(voucher="PRIVATE-DN")
		with self.assertRaises(frappe.PermissionError):
			permissions.get_serial_batch_ledgers(name="BUNDLE", item_code="ITEM")
		self.native.assert_not_called()

	def test_bundle_children_are_checked_in_addition_to_header(self):
		self.bundle().entries.append(frappe._dict(warehouse="FORBIDDEN", batch_no="HIDDEN", qty=4))
		with self.assertRaises(frappe.PermissionError):
			permissions.get_serial_batch_ledgers(name="BUNDLE")
		self.native.assert_not_called()

	def test_bundle_list_rejects_any_out_of_scope_name(self):
		self.bundle()
		self.bundle(name="OTHER", warehouse="FORBIDDEN")
		with self.assertRaises(frappe.PermissionError):
			permissions.get_serial_batch_ledgers(name=["BUNDLE", "OTHER"])
		self.native.assert_not_called()

	def test_unbounded_bundle_query_is_rejected(self):
		with self.assertRaises(frappe.PermissionError):
			permissions.get_serial_batch_ledgers(item_code="ITEM")
		self.native.assert_not_called()

	def test_unlinked_draft_can_only_be_reopened_by_its_creator(self):
		self.bundle(voucher=None, owner="other@example.test")
		with self.assertRaises(frappe.PermissionError):
			permissions.get_serial_batch_ledgers(name="BUNDLE")
		self.native.assert_not_called()

	def test_direct_bundle_permission_checks_parent_without_recursing_into_bundle(self):
		doc = self.bundle(voucher="PRIVATE-DN")
		self.assertFalse(permissions.bundle_permission(doc, ptype="read"))
		self.assertNotIn("Serial and Batch Bundle", [call.args[0] for call in frappe.has_permission.call_args_list])

	def test_direct_bundle_permission_checks_actual_child_warehouses(self):
		doc = self.bundle()
		doc.entries.append(frappe._dict(warehouse="FORBIDDEN"))
		self.assertFalse(permissions.bundle_permission(doc, ptype="read"))

	def test_direct_bundle_permission_accepts_readable_parent_and_own_draft(self):
		self.assertTrue(permissions.bundle_permission(self.bundle(), ptype="read"))
		self.assertTrue(permissions.bundle_permission(self.bundle(voucher=None), ptype="read"))
		self.assertFalse(permissions.bundle_permission(self.bundle(voucher=None, owner="someone-else"), ptype="read"))

	def test_unlinked_submitted_bundle_and_self_reference_are_not_readable(self):
		doc = self.bundle(voucher=None)
		doc.docstatus = 1
		self.assertFalse(permissions.bundle_permission(doc, ptype="read"))
		doc.voucher_type, doc.voucher_no = "Serial and Batch Bundle", "BUNDLE"
		self.assertFalse(permissions.bundle_permission(doc, ptype="read"))

	def test_bundle_read_hook_does_not_replace_native_write_permissions(self):
		doc = self.bundle(voucher="PRIVATE-DN")
		self.assertTrue(permissions.bundle_permission(doc, ptype="write"))
		with patch.object(permissions, "_scope", return_value=None):
			self.assertTrue(permissions.bundle_permission(doc, ptype="read"))

	def test_own_unlinked_draft_and_empty_child_argument_keep_native_selector_working(self):
		self.bundle(voucher=None)
		permissions.get_serial_batch_ledgers(name="BUNDLE", child_row="")
		self.assertEqual(self.native.call_args.kwargs["name"], ["BUNDLE"])

	def test_return_resolves_saved_source_row_and_checks_original(self):
		self.bundle()
		self.docs[("Delivery Note Item", "SOURCE")] = frappe._dict(parenttype="Delivery Note", parent="DN", item_code="ITEM", serial_and_batch_bundle="BUNDLE")
		child = frappe._dict(doctype="Delivery Note Item", dn_detail="SOURCE", qty=-2)
		permissions.get_serial_batch_ledgers(item_code="ITEM", child_row=child, voucher_no="UNSAVED-RETURN")
		self.assertEqual(self.native.call_args.kwargs["name"], ["BUNDLE"])
		self.assertIsNone(self.native.call_args.kwargs["voucher_no"])

	def test_legacy_return_without_bundle_returns_empty_not_all_bundles(self):
		self.docs[("Delivery Note Item", "LEGACY")] = frappe._dict(parenttype="Delivery Note", parent="DN", item_code="ITEM")
		child = frappe._dict(doctype="Delivery Note Item", dn_detail="LEGACY", qty=-2)
		self.assertEqual(permissions.get_serial_batch_ledgers(item_code="ITEM", child_row=child), [])
		self.native.assert_not_called()

	def test_return_cannot_read_a_private_original_even_with_an_allowed_warehouse(self):
		self.docs[("Delivery Note Item", "SOURCE")] = frappe._dict(parenttype="Delivery Note", parent="PRIVATE-DN", item_code="ITEM", serial_and_batch_bundle="BUNDLE")
		child = frappe._dict(doctype="Delivery Note Item", dn_detail="SOURCE", qty=-2, warehouse="ALLOWED")
		with self.assertRaises(frappe.PermissionError):
			permissions.get_serial_batch_ledgers(item_code="ITEM", child_row=child)
		self.native.assert_not_called()

	def test_search_link_redirects_only_native_batch_query(self):
		permissions.search_link("Batch", "B", query=permissions.NATIVE_BATCH_QUERY, filters={"warehouse": "ALLOWED"})
		self.assertEqual(self.native.call_args.kwargs["query"], permissions.SCOPED_BATCH_QUERY)
		permissions.search_link("Item", "I", query="other.custom.query", page_length=35)
		self.assertEqual(self.native.call_args.kwargs["query"], "other.custom.query")
		self.assertEqual(self.native.call_args.kwargs["page_length"], 35)

	def test_search_widget_redirects_direct_native_batch_query(self):
		permissions.search_widget("Batch", "B", query=permissions.NATIVE_BATCH_QUERY, for_link_validation=True)
		self.assertEqual(self.native.call_args.kwargs["query"], permissions.SCOPED_BATCH_QUERY)
		self.assertTrue(self.native.call_args.kwargs["for_link_validation"])
		permissions.search_widget("Warehouse", "W")
		self.assertIsNone(self.native.call_args.kwargs["query"])


class TestBatchScopeSelection(unittest.TestCase):
	def scope_for(self, user, roles):
		with patch.object(frappe, "session", frappe._dict(user=user)), \
			patch.object(frappe, "get_roles", return_value=roles), \
			patch.object(permissions, "user_company_scope", return_value={"CO"}):
			return permissions._scope()

	def test_administrator_and_system_manager_keep_native_access(self):
		self.assertIsNone(self.scope_for("Administrator", [permissions.WAREHOUSE_OPERATOR_ROLE]))
		self.assertIsNone(self.scope_for("manager", [permissions.OWNER_ROLE, "System Manager"]))

	def test_native_stock_role_is_not_restricted_by_this_adapter(self):
		self.assertIsNone(self.scope_for("native", ["Stock User"]))

	def test_simplified_roles_use_explicit_company_scope(self):
		for role in (permissions.OWNER_ROLE, permissions.WAREHOUSE_OPERATOR_ROLE):
			with self.subTest(role=role):
				self.assertEqual(self.scope_for("operator", [role]).companies, {"CO"})


class TestBundleListPermissionQueries(unittest.TestCase):
	def test_list_uses_full_parent_permission_query_not_just_matching_warehouse(self):
		scope = frappe._dict(user="warehouse@example.test", companies={"CO"})
		queries = {
			"Warehouse": "select name from wh where name in ('ALLOWED')",
			"Delivery Note": "select name from dn where customer = 'ALLOWED-CUSTOMER' and warehouse = 'ALLOWED'",
		}
		with patch.object(permissions, "_scope", return_value=scope), \
			patch.object(permissions, "_readable_names_query", side_effect=lambda doctype, user, filters=None: queries[doctype]), \
			patch.object(frappe, "get_meta", return_value=MagicMock(has_field=lambda field: field == "company")), \
			patch.object(frappe, "get_all", return_value=[frappe._dict(voucher_type=name) for name in ("Delivery Note", "Purchase Invoice", "Serial and Batch Bundle")]), \
			patch.object(frappe, "has_permission", side_effect=lambda doctype, *args, **kwargs: doctype == "Delivery Note"), \
			patch.object(frappe, "db", MagicMock(escape=lambda value: "'" + value.replace("'", "''") + "'")):
			condition = permissions.bundle_query()
		self.assertIn(queries["Warehouse"], condition)
		self.assertIn(queries["Delivery Note"], condition)
		self.assertNotIn("Purchase Invoice", condition)
		self.assertIn("company in ('CO')", condition)
		self.assertIn("docstatus = 0", condition)
		self.assertIn("owner = 'warehouse@example.test'", condition)
		self.assertIn("ifnull(`tabSerial and Batch Bundle`.voucher_no, '') = ''", condition)
		self.assertIn("scope_batch.parenttype = 'Serial and Batch Bundle'", condition)
		self.assertIn("scope_batch.warehouse not in (" + queries["Warehouse"] + ")", condition)

	def test_no_company_or_warehouse_read_scope_returns_no_list_rows(self):
		with patch.object(permissions, "_scope", return_value=frappe._dict(user="user", companies=set())):
			self.assertEqual(permissions.bundle_query(), "1=0")
		with patch.object(permissions, "_scope", return_value=frappe._dict(user="user", companies={"CO"})), \
			patch.object(frappe, "db", MagicMock(escape=lambda value: repr(value))), \
			patch.object(permissions, "_readable_names_query", side_effect=frappe.PermissionError):
			self.assertEqual(permissions.bundle_query(), "1=0")

	def test_native_users_keep_existing_list_conditions(self):
		with patch.object(permissions, "_scope", return_value=None), \
			patch.object(frappe, "get_all") as query:
			self.assertEqual(permissions.bundle_query(), "")
			query.assert_not_called()

	def test_native_subquery_is_compiled_with_user_permissions_and_without_pagination(self):
		query = MagicMock()
		query.get_sql.return_value = "select permitted_names"
		with patch.object(frappe, "get_list", return_value=query) as get_list:
			self.assertEqual(permissions._readable_names_query("Delivery Note", "user"), "select permitted_names")
		get_list.assert_called_once_with("Delivery Note", fields=["name"], user="user", limit=0, order_by="", run=False)
