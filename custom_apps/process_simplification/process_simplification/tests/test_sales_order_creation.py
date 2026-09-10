"""Transaction policy tests; real 1020/HTTP tests run in disposable sites."""

import json
from collections import defaultdict
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from frappe.utils import CallbackManager

from process_simplification import sales_order_creation as creation


class TestSalesOrderCreation(TestCase):
	def setUp(self):
		self.local = SimpleNamespace(
			request=SimpleNamespace(method="POST", path="/api/resource/Sales Order"),
			flags=frappe._dict(read_only=False), form_dict=frappe._dict(),
			message_log=["before"], error_log=[], debug_log=[], response=frappe._dict(docs=[]),
			cache={}, request_cache=defaultdict(dict), new_doc_templates={}, role_permissions={},
			document_cache={}, user_perms=None, session=frappe._dict(user="sales@example.com", sid="keep"),
		)
		self.db = SimpleNamespace(
			db_type="mariadb", transaction_writes=0, _disable_transaction_control=0,
			execute_query=Mock(return_value="ok"), value_cache={}, get_value=Mock(return_value=1),
			exists=Mock(return_value=None),
			**{name: CallbackManager() for name in creation._CALLBACKS},
		)
		self.db.rollback = Mock(side_effect=self.rollback)
		for name, value in (("local", self.local), ("db", self.db), ("flags", self.local.flags),
			("form_dict", self.local.form_dict), ("session", self.local.session)):
			self.enterContext(patch.object(frappe, name, value, create=True))
		self.audited = self.enterContext(patch.object(creation, "_audited_configuration", return_value=True))
		self.matches = self.enterContext(patch.object(creation, "_parent_insert_snapshot_conflict", return_value=True))

	def rollback(self):
		self.db.before_commit.reset()
		self.db.after_commit.reset()
		self.db.before_rollback.run()
		self.db.after_rollback.run()
		self.db.value_cache.clear()
		self.db.transaction_writes = 0

	def run_operation(self, operation):
		return creation._run(operation, eligible=True, entry="rest")

	def test_recovery_rolls_back_reconstructs_payload_and_retains_identity(self):
		payload = {"customer": "C", "items": [{"item_code": "FG", "qty": 2}]}
		seen = []
		error = frappe.QueryDeadlockError("1020 probe")
		def new_doc(doctype, **data):
			seen.append(data)
			if len(seen) == 1:
				data["items"][0]["qty"] = 99
				self.local.message_log.append("failed")
				self.local.response.docs.append("failed")
				self.local.flags.failed = True
				for name in ("cache", "request_cache", "new_doc_templates", "role_permissions", "document_cache"):
					getattr(self.local, name)["failed"] = True
				self.db.after_commit.add(lambda: self.fail("failed attempt callback ran"))
				self.local.ps_page_changes = {("all", "orders")}
				return SimpleNamespace(insert=Mock(side_effect=error))
			self.assertEqual(self.local.message_log, ["before"])
			self.assertEqual(self.local.response.docs, [])
			self.assertNotIn("failed", self.local.flags)
			self.assertEqual(self.local.ps_page_changes, set())
			self.assertEqual(len(self.db.after_commit._functions), 0)
			self.assertEqual(self.local.session.sid, "keep")
			return SimpleNamespace(insert=Mock(return_value="created"))
		with patch.object(frappe, "new_doc", side_effect=new_doc):
			self.assertEqual(creation.create_rest_draft(payload), "created")
		self.assertEqual(seen[1]["items"][0]["qty"], 2)
		self.assertEqual(payload["items"][0]["qty"], 2)
		self.db.rollback.assert_called_once_with()
		self.db.get_value.assert_called_once_with("User", "sales@example.com", "enabled")
		self.assertFalse(self.local.ps_sales_order_creation_active)

	def test_exhaustion_preserves_second_error_after_exactly_two_attempts(self):
		errors = [frappe.QueryDeadlockError("first"), frappe.QueryDeadlockError("second")]
		op = Mock(side_effect=errors)
		with self.assertRaises(frappe.QueryDeadlockError) as caught:
			self.run_operation(op)
		self.assertIs(caught.exception, errors[1])
		self.assertEqual(op.call_count, 2)
		self.db.rollback.assert_called_once()

	def test_nonmatching_error_is_never_retried(self):
		self.matches.return_value = False
		op = Mock(side_effect=frappe.QueryDeadlockError("another table or error code"))
		with self.assertRaises(frappe.QueryDeadlockError):
			self.run_operation(op)
		op.assert_called_once()
		self.db.rollback.assert_not_called()

	def test_permission_or_validation_error_is_never_retried(self):
		for error in (frappe.PermissionError("denied"), frappe.LinkValidationError("invalid customer")):
			with self.subTest(error=type(error)):
				op = Mock(side_effect=error)
				with self.assertRaises(type(error)):
					self.run_operation(op)
				op.assert_called_once()
		self.db.rollback.assert_not_called()

	def test_account_disabled_between_attempts_prevents_creation(self):
		self.db.get_value.return_value = 0
		op = Mock(side_effect=frappe.QueryDeadlockError("first"))
		with self.assertRaises(frappe.PermissionError):
			self.run_operation(op)
		op.assert_called_once()

	def test_configuration_change_between_attempts_preserves_original_error(self):
		self.audited.side_effect = [True, False]
		error = frappe.QueryDeadlockError("first")
		op = Mock(side_effect=error)
		with self.assertRaises(frappe.QueryDeadlockError) as caught:
			self.run_operation(op)
		self.assertIs(caught.exception, error)
		op.assert_called_once()

	def test_preexisting_write_callbacks_read_only_and_internal_calls_are_untouched(self):
		for reason in ("write", "callback", "disabled", "read_only", "nested", "no_request", "custom_rpc"):
			with self.subTest(reason=reason):
				self.setUp()
				if reason == "write": self.db.transaction_writes = 1
				if reason == "callback": self.db.after_commit.add(lambda: None)
				if reason == "disabled": self.db._disable_transaction_control = 1
				if reason == "read_only": self.local.flags.read_only = True
				if reason == "nested": self.local.ps_sales_order_creation_active = True
				if reason == "no_request": self.local.request = None
				if reason == "custom_rpc": self.local.request.path = "/api/method/custom"
				op = Mock(side_effect=frappe.QueryDeadlockError("caller owns transaction"))
				with self.assertRaises(frappe.QueryDeadlockError): self.run_operation(op)
				op.assert_called_once()
				self.db.rollback.assert_not_called()

	def test_transaction_control_and_implicit_commit_disable_recovery(self):
		for query in ("COMMIT", "ROLLBACK", "START TRANSACTION", "SET autocommit=1", "CREATE TABLE x (id int)", "CALL x()"):
			with self.subTest(query=query):
				def operation():
					self.db.execute_query(query)
					raise frappe.QueryDeadlockError("after transaction boundary")
				with self.assertRaises(frappe.QueryDeadlockError): self.run_operation(operation)
		self.db.rollback.assert_not_called()

	def test_successful_parent_insert_prevents_replay_of_later_nested_order_conflict(self):
		calls = []
		def operation():
			calls.append("parent inserted; after_insert may publish externally")
			self.db.execute_query("INSERT INTO `tabSales Order` (`name`) VALUES (%s)", ("SO",))
			raise frappe.QueryDeadlockError("second Sales Order inserted by after_insert")
		with self.assertRaises(frappe.QueryDeadlockError): self.run_operation(operation)
		self.assertEqual(len(calls), 1)
		self.db.rollback.assert_not_called()

	def test_series_statement_is_allowed_and_instance_wrapper_is_restored(self):
		execute = self.db.execute_query
		with creation._observe_transaction_boundary() as boundary:
			self.db.execute_query("SET STATEMENT innodb_snapshot_isolation=OFF FOR INSERT INTO `tabSeries` (`name`) VALUES (%s)")
			self.assertTrue(boundary["intact"])
		self.assertIs(self.db.execute_query, execute)

	def test_missing_baseline_fields_are_removed_without_replacing_session(self):
		del self.local.debug_log
		baseline = creation._snapshot_request_state()
		session = self.local.session
		self.local.debug_log = ["failed"]
		creation._discard_attempt(baseline)
		self.assertFalse(hasattr(self.local, "debug_log"))
		self.assertIs(self.local.session, session)

	def test_desk_rebuilds_temporary_names_and_emits_telemetry_once(self):
		from frappe.desk.form import save
		self.local.request.path = "/api/method/frappe.desk.form.save.savedocs"
		self.local.form_dict.cmd = creation._DESK_METHOD
		data = {"doctype": "Sales Order", "__islocal": 1, "name": "new-sales-order-temp", "items": [{"name": "new-child", "qty": 1}]}
		seen = []
		def body(doc, action):
			seen.append(doc)
			if len(seen) == 1:
				doc["name"] = "failed-number"
				doc["items"][0]["name"] = "failed-child"
				raise frappe.QueryDeadlockError("first")
			self.assertEqual(doc, data)
			return "saved"
		with patch.object(frappe, "get_doc", side_effect=lambda data: data), \
			patch.object(save, "capture_doc") as capture, patch.object(save, "_savedocs", side_effect=body, create=True):
			self.assertEqual(creation.savedocs(json.dumps(data), "Save"), "saved")
			capture.assert_called_once()
		self.assertIsNot(seen[0], seen[1])

	def test_desk_update_submit_cancel_and_other_doctypes_use_native_once(self):
		from frappe.desk.form import save
		for data, action in (({"doctype": "Sales Order", "name": "SO"}, "Save"),
			({"doctype": "Sales Order", "__islocal": 1}, "Submit"),
			({"doctype": "Sales Order", "__islocal": 1}, "Cancel"),
			({"doctype": "Customer", "__islocal": 1}, "Save")):
			with self.subTest(action=action, data=data), patch.object(save, "savedocs", return_value="original") as native:
				payload = json.dumps(data)
				self.assertEqual(creation.savedocs(payload, action), "original")
				native.assert_called_once_with(payload, action)


class TestCreationConfiguration(TestCase):
	def setUp(self):
		from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
		self.hooks = {}
		self.tables = [SimpleNamespace(options="Sales Order Item")]
		self.exists = Mock(return_value=None)
		self.apps = ["frappe", "erpnext", "process_simplification"]
		self.enterContext(patch.object(frappe, "db", SimpleNamespace(exists=self.exists), create=True))
		self.enterContext(patch.object(frappe, "get_hooks", side_effect=lambda name: self.hooks.get(name, [])))
		self.enterContext(patch.object(frappe, "get_installed_apps", side_effect=lambda: self.apps))
		self.enterContext(patch.object(frappe, "get_meta", return_value=SimpleNamespace(get_table_fields=lambda: self.tables)))
		self.controller = self.enterContext(patch("frappe.model.base_document.get_controller", return_value=SalesOrder))

	def test_missing_hook_lists_and_audited_wildcard_validators_are_supported(self):
		self.assertTrue(creation._audited_configuration())
		self.hooks["before_request"] = sorted(creation._AUDITED_BEFORE_REQUEST_HOOKS)
		self.hooks["doc_events"] = {"*": {"validate": sorted(creation._AUDITED_VALIDATE_HOOKS)}}
		self.assertTrue(creation._audited_configuration())

	def test_unknown_preinsert_hooks_including_child_naming_disable_recovery(self):
		for scope, event in (("Sales Order", "before_insert"), ("Sales Order Item", "before_naming"),
			("*", "validate"), (("Sales Order", "Sales Invoice"), "before_save")):
			with self.subTest(scope=scope, event=event):
				self.hooks["doc_events"] = {scope: {event: ["custom.enqueue_external_effect"]}}
				self.assertFalse(creation._audited_configuration())

	def test_configurable_external_effects_disable_recovery(self):
		for doctype in ("Server Script", "Webhook", "Notification"):
			with self.subTest(doctype=doctype):
				self.exists.side_effect = lambda requested, filters: "enabled" if requested == doctype else None
				self.assertFalse(creation._audited_configuration())
		notification_filters = self.exists.call_args.args[1]
		self.assertEqual(notification_filters["event"], "Method")
		self.assertIn("Sales Order Item", notification_filters["document_type"][1])
		self.assertEqual(set(notification_filters["method"][1]), creation._PRE_INSERT_EVENTS)

	def test_controller_permission_request_and_auth_overrides_disable_recovery(self):
		for hook, value in (("override_doctype_class", {"Sales Order": ["custom.Class"]}),
			("extend_doctype_class", {"Sales Order Item": ["custom.Mixin"]}),
			("has_permission", {"Sales Order": ["custom.permission"]}),
			("before_request", ["custom.before_request"]), ("auth_hooks", ["custom.auth"])):
			with self.subTest(hook=hook):
				self.hooks.clear()
				self.hooks[hook] = value
				self.assertFalse(creation._audited_configuration())

	def test_unknown_app_table_and_controller_disable_recovery(self):
		self.apps.append("custom")
		self.assertFalse(creation._audited_configuration())
		self.apps.pop()
		self.tables.append(SimpleNamespace(options="Custom Child"))
		self.assertFalse(creation._audited_configuration())
		self.tables.pop()
		self.controller.return_value = object
		self.assertFalse(creation._audited_configuration())

	def test_unexpected_nonempty_hook_type_is_rejected(self):
		self.hooks["has_permission"] = ["unexpected"]
		self.assertFalse(creation._audited_configuration())


class TestCreationConflictClassifier(TestCase):
	def error(self, *, code=1020, doctype="Sales Order", docstatus=0, table="Sales Order"):
		from frappe.database.database import Database
		from frappe.model.base_document import BaseDocument
		# The SQL exception is wrapped by Frappe; matching uses the cause code and
		# the pinned call frames, never arbitrary exception text containing "1020".
		error = Mock(spec=frappe.QueryDeadlockError)
		error.__cause__ = Exception(code, "arbitrary localized message")
		error.__traceback__ = SimpleNamespace(
			tb_frame=SimpleNamespace(f_code=BaseDocument.db_insert.__code__, f_locals={
				"self": SimpleNamespace(doctype=doctype, docstatus=docstatus),
			}),
			tb_next=SimpleNamespace(
				tb_frame=SimpleNamespace(f_code=Database.sql.__code__, f_locals={
					"query": f"INSERT INTO `tab{table}` (`name`) VALUES (%s)",
				}), tb_next=None,
			),
		)
		return error

	def test_exact_parent_insert_1020_matches(self):
		self.assertTrue(creation._parent_insert_snapshot_conflict(self.error()))

	def test_other_database_codes_do_not_match(self):
		for code in (1062, 1205, 1213):
			with self.subTest(code=code):
				self.assertFalse(creation._parent_insert_snapshot_conflict(self.error(code=code)))

	def test_child_submitted_order_and_unrelated_sql_do_not_match(self):
		for kwargs in ({"doctype": "Sales Order Item"}, {"docstatus": 1}, {"table": "Sales Order Item"}):
			with self.subTest(kwargs=kwargs):
				self.assertFalse(creation._parent_insert_snapshot_conflict(self.error(**kwargs)))

	def test_no_insert_frame_or_untranslated_error_does_not_match(self):
		error = self.error()
		error.__traceback__ = None
		self.assertFalse(creation._parent_insert_snapshot_conflict(error))
		self.assertFalse(creation._parent_insert_snapshot_conflict(Exception(1020, "Sales Order INSERT")))
