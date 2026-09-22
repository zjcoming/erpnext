"""Permission, naming and rollback contracts without a connected site."""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from process_simplification.api import warehouse_management as api


class TestWarehouseManagementUnit(TestCase):
	def test_endpoint_requires_authenticated_post(self):
		registry = frappe.whitelist.__globals__
		self.assertEqual(registry["allowed_http_methods_for_whitelisted_func"][api.rename_warehouse], ["POST"])
		self.assertNotIn(api.rename_warehouse, registry["guest_methods"])

	def setUp(self):
		self.stack = ExitStack()
		self.addCleanup(self.stack.close)
		self.db = MagicMock()
		self.db.exists.return_value = False
		self.old = MagicMock(company="Company A", parent_warehouse="All - CA")
		self.old.name = "Stores - CA"
		self.new = MagicMock()
		self.local = SimpleNamespace(_realtime_log=[("prior-event",)])
		self.stack.enter_context(patch.object(frappe, "local", self.local))
		self.stack.enter_context(patch.object(frappe, "session", SimpleNamespace(user="manager@example.com")))
		self.stack.enter_context(patch.object(frappe, "db", self.db))
		self.stack.enter_context(patch.object(api, "_", lambda message: message))
		def throw(message, exc=frappe.ValidationError, **kwargs):
			raise exc(message)
		self.stack.enter_context(patch.object(frappe, "throw", side_effect=throw))
		self.roles = self.stack.enter_context(patch.object(frappe, "get_roles", return_value=["System Manager"]))
		self.get_doc = self.stack.enter_context(patch.object(frappe, "get_doc", side_effect=[self.old, self.new]))
		self.stack.enter_context(patch.object(frappe, "get_cached_value", return_value="CA"))
		self.stack.enter_context(patch.object(frappe, "generate_hash", return_value="unit1234"))
		self.cache = self.stack.enter_context(patch.object(frappe, "clear_cache"))
		self.enqueue = self.stack.enter_context(patch.object(frappe, "enqueue"))
		self.rename = self.stack.enter_context(patch.object(api, "rename_doc", return_value="原料仓 - CA"))

	def test_existing_references_use_native_non_merge_rename_and_label_save(self):
		result = api.rename_warehouse("Stores - CA", " 原料仓 - CA ")
		self.assertEqual(result, {"old_name": "Stores - CA", "name": "原料仓 - CA", "warehouse_name": "原料仓"})
		self.rename.assert_called_once_with("Warehouse", "Stores - CA", "原料仓 - CA", force=True,
			merge=False, ignore_permissions=False, show_alert=False, rebuild_search=False)
		self.assertEqual(self.new.warehouse_name, "原料仓")
		self.new.save.assert_called_once_with()
		self.assertEqual(self.old.check_permission.call_count, 2)
		self.db.commit.assert_not_called()
		self.enqueue.assert_called_once_with("frappe.utils.global_search.rebuild_for_doctype",
			doctype="Warehouse", enqueue_after_commit=True)

	def test_non_manager_cannot_even_load_the_warehouse(self):
		self.roles.return_value = ["Process Simplification Owner"]
		with self.assertRaises(frappe.PermissionError):
			api.rename_warehouse("Stores - CA", "原料仓")
		self.get_doc.assert_not_called()

	def test_item_manager_is_rejected_without_system_settings_administration(self):
		self.roles.return_value = ["Item Manager"]
		with self.assertRaises(frappe.PermissionError):
			api.rename_warehouse("Stores - CA", "原料仓")
		self.get_doc.assert_not_called()

	def test_manager_still_needs_document_write_scope(self):
		self.old.check_permission.side_effect = frappe.PermissionError
		with self.assertRaises(frappe.PermissionError):
			api.rename_warehouse("Stores - CA", "原料仓")
		self.rename.assert_not_called()

	def test_root_warehouse_is_not_renamed(self):
		self.old.parent_warehouse = None
		with self.assertRaises(frappe.ValidationError):
			api.rename_warehouse("Stores - CA", "原料仓")
		self.rename.assert_not_called()

	def test_empty_control_character_and_other_company_suffix_are_rejected(self):
		for value in ("", "  ", None, "原料仓 - CB", "原料\n仓", " - CA"):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				api._warehouse_label(value, "CA")

	def test_same_name_is_not_treated_as_success(self):
		with self.assertRaises(frappe.ValidationError):
			api.rename_warehouse("Stores - CA", "Stores")
		self.rename.assert_not_called()

	def test_existing_target_cannot_be_overwritten_or_merged(self):
		self.db.exists.return_value = True
		with self.assertRaises(frappe.ValidationError):
			api.rename_warehouse("Stores - CA", "原料仓")
		self.rename.assert_not_called()

	def test_company_change_while_waiting_for_lock_is_rejected(self):
		self.old.reload.side_effect = lambda: setattr(self.old, "company", "Company B")
		with self.assertRaises(frappe.ValidationError):
			api.rename_warehouse("Stores - CA", "原料仓")
		self.rename.assert_not_called()
		self.db.rollback.assert_called_once_with(save_point="warehouse_rename_unit1234")

	def test_failure_after_native_rename_restores_transaction_and_realtime_events(self):
		def rename(*args, **kwargs):
			self.local._realtime_log.append(("doc_rename",))
			return "原料仓 - CA"
		self.rename.side_effect = rename
		self.new.save.side_effect = RuntimeError("label write failed")
		with self.assertRaisesRegex(RuntimeError, "label write failed"):
			api.rename_warehouse("Stores - CA", "原料仓")
		self.db.rollback.assert_called_once_with(save_point="warehouse_rename_unit1234")
		self.assertEqual(self.local._realtime_log, [("prior-event",)])
		self.cache.assert_called_once_with()
		self.enqueue.assert_not_called()

	def test_deadlock_does_not_mask_the_error_with_a_missing_savepoint(self):
		self.rename.side_effect = frappe.QueryDeadlockError
		with self.assertRaises(frappe.QueryDeadlockError):
			api.rename_warehouse("Stores - CA", "原料仓")
		self.db.rollback.assert_not_called()
