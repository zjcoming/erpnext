from base64 import b64decode
from hashlib import sha256
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from process_simplification import document_scan, printing
from process_simplification.api import document_scan as api

SITE_ID = "a" * 32


class TestScanCodes(TestCase):
	def test_names_round_trip_in_address_independent_internal_codes(self):
		for name in ("PUR-ORD-2026-0001", "任务/工序 01?#%", "工单-零件😀", "a" * 140):
			with self.subTest(name=name):
				code = document_scan.document_scan_code("Job Card", name, SITE_ID)
				self.assertTrue(code.startswith(f"HSERP|1|{SITE_ID}|job-card|"))
				self.assertNotIn(":", code)
				self.assertNotIn("/", code)
				self.assertEqual(document_scan.decode_document_scan(code, SITE_ID), ("Job Card", name))

	def test_foreign_installations_versions_malformed_codes_and_legacy_urls_are_rejected(self):
		code = document_scan.document_scan_code("Purchase Order", "PO-1", SITE_ID)
		for value in (
			code.replace(SITE_ID, "b" * 32),
			code.replace("|1|", "|2|"),
			code + "|extra",
			code.replace("|purchase-order|", "|__proto__|"),
			code.replace(SITE_ID, "bad"),
			code.replace("HSERP|", "https:|"),
			None,
			"x" * 1025,
			"https://factory.example/desk/document-scan/purchase-order/UE8tMQ",
			"/desk/document-scan/purchase-order/UE8tMQ",
		):
			with self.subTest(code=value), self.assertRaises(ValueError):
				document_scan.decode_document_scan(value, SITE_ID)
		with self.assertRaisesRegex(ValueError, "尚未初始化"):
			document_scan.decode_document_scan(code, None)

	def test_rejects_unknown_types_invalid_names_and_noncanonical_tokens(self):
		for name in ("", "  ", "a" * 141, "PO\n123", None):
			with self.subTest(name=name), self.assertRaises(ValueError):
				document_scan.encode_document_name(name)
		for kind, token in (
			("Employee", "eA"),
			("job-card", "eA=="),
			("job-card", "eB"),
			("job-card", "_w"),
			("job-card", "a"),
			("job-card", "x" * 751),
		):
			with self.subTest(kind=kind, token=token), self.assertRaises(ValueError):
				document_scan.decode_scan_target(kind, token)

	def test_print_uses_an_internal_code_without_reading_the_request_address(self):
		for doc in (
			{},
			{"doctype": "Employee", "name": "EMP-1"},
			{"doctype": "Job Card", "name": "new-job-card", "__islocal": 1},
		):
			self.assertIsNone(printing.get_document_scan_qr(doc))
		import pyqrcode

		with (
			patch.object(document_scan, "get_scan_site_id", return_value=SITE_ID),
			patch.object(frappe.utils, "get_url") as url,
			patch.object(pyqrcode, "create", wraps=pyqrcode.create) as create,
		):
			result = printing.get_document_scan_qr({"doctype": "Purchase Order", "name": "PO-1"})
			url.assert_not_called()
			create.assert_called_once_with(
				document_scan.document_scan_code("Purchase Order", "PO-1", SITE_ID), error="M"
			)
			self.assertTrue(result.image.startswith("data:image/png;base64,"))
			self.assertTrue(b64decode(result.image.split(",", 1)[1]).startswith(b"\x89PNG\r\n\x1a\n"))

	def test_only_unmodified_legacy_letterhead_is_upgraded(self):
		with (
			patch.object(frappe, "db", Mock(), create=True),
			patch.object(frappe, "get_doc") as get_doc,
			patch.object(printing, "LEGACY_TEMPLATE_HASH", sha256(b"old template").hexdigest()),
		):
			frappe.db.exists.return_value = True
			frappe.db.get_value.return_value = "user customized template"
			printing.ensure_factory_letterhead()
			get_doc.assert_not_called()
			frappe.db.get_value.return_value = "old template"
			get_doc.return_value.source = "HTML"
			printing.ensure_factory_letterhead()
			get_doc.return_value.save.assert_called_once_with(ignore_permissions=True)
			self.assertEqual(get_doc.return_value.content, printing.FACTORY_LETTERHEAD_INCLUDE)
			frappe.db.set_value.assert_not_called()

	def test_unchanged_url_letterhead_gets_internal_scan_caption(self):
		with (
			patch.object(frappe, "db", Mock(), create=True),
			patch.object(frappe, "get_doc") as get_doc,
			patch.object(printing, "URL_TEMPLATE_HASH", sha256(b"url template").hexdigest()),
		):
			frappe.db.exists.return_value = True
			frappe.db.get_value.return_value = "url template"
			get_doc.return_value.source = "HTML"
			printing.ensure_factory_letterhead()
			get_doc.return_value.save.assert_called_once_with(ignore_permissions=True)
			self.assertEqual(get_doc.return_value.content, printing.FACTORY_LETTERHEAD_INCLUDE)
			self.assertIn("仅限系统内扫码", printing.FACTORY_LETTERHEAD_TEMPLATE.read_text())


class TestScanInstallation(TestCase):
	def test_namespace_is_created_once_and_retained_across_upgrades(self):
		defaults = {}
		with (
			patch.object(frappe.defaults, "get_defaults_for", return_value=defaults) as get_defaults,
			patch.object(
				frappe.defaults,
				"set_global_default",
				side_effect=lambda key, value: defaults.update({key: value}),
			) as setter,
		):
			first = document_scan.ensure_scan_site_id()
			self.assertRegex(first, r"^[0-9a-f]{32}$")
			self.assertEqual(document_scan.ensure_scan_site_id(), first)
			self.assertEqual(document_scan.get_scan_site_id(), first)
			setter.assert_called_once()
			get_defaults.assert_called_with("__default")

	def test_missing_namespace_fails_without_writing_during_printing_or_resolution(self):
		with (
			patch.object(frappe.defaults, "get_defaults_for", return_value={}),
			patch.object(frappe.defaults, "set_global_default") as setter,
		):
			with self.assertRaisesRegex(ValueError, "尚未初始化"):
				document_scan.document_scan_code("Job Card", "JC1")
			self.assertIsNone(document_scan.get_scan_site_id())
			setter.assert_not_called()

	def test_namespace_ignores_user_defaults_and_is_available_only_in_authenticated_boot(self):
		with (
			patch.object(
				frappe.defaults, "get_defaults_for", return_value={document_scan.SCAN_SITE_ID_KEY: SITE_ID}
			) as defaults,
			patch.object(frappe.defaults, "get_global_default") as user_defaults,
			patch.object(frappe, "session", SimpleNamespace(user="worker@example.com"), create=True),
		):
			boot = frappe._dict()
			document_scan.boot_session(boot)
			self.assertEqual(boot.process_document_scan["site_id"], SITE_ID)
			defaults.assert_called_with("__default")
			user_defaults.assert_not_called()
			frappe.session.user = "Guest"
			guest_boot = frappe._dict()
			document_scan.boot_session(guest_boot)
			self.assertNotIn("process_document_scan", guest_boot)


class TestScanResolution(TestCase):
	def test_inventory_codes_check_native_read_permission_and_open_only_the_matching_type(self):
		for kind, doctype in [("purchase-receipt", "Purchase Receipt"), ("stock-entry", "Stock Entry"), ("delivery-note", "Delivery Note")]:
			with self.subTest(doctype=doctype):
				self.doc.reset_mock()
				self.doc.return_value.name = "SAME-1"
				self.doc.return_value.check_permission.side_effect = None
				self.assertEqual(self.resolve(kind, "SAME-1")["route"], ["Form", doctype, "SAME-1"])
				self.doc.assert_called_once_with(doctype, "SAME-1")
				self.doc.return_value.check_permission.assert_called_once_with("read")
				self.doc.return_value.check_permission.side_effect = frappe.PermissionError
				self.assertFalse(self.resolve(kind, "SAME-1")["found"])
			self.worker.assert_not_called(); self.db.set_value.assert_not_called()

	def test_inventory_search_lists_return_direction_without_bypassing_permissions(self):
		self.roles.return_value = []
		for doctype, row in [
			("Purchase Receipt", frappe._dict(name="PR-1", supplier_name="供应商", is_return=1, posting_date="2026-09-11")),
			("Delivery Note", frappe._dict(name="DN-1", customer_name="客户", is_return=1)),
			("Stock Entry", frappe._dict(name="SE-1", stock_entry_type="生产发料", docstatus=0, work_order="WO-1")),
		]:
			with self.subTest(doctype=doctype), patch.object(frappe, "get_list", side_effect=[[], [row]]) as reader:
				result = api.search_documents("1", doctype)["results"][0]
				self.assertEqual(result["doctype"], doctype)
				self.assertNotIn("route", result)
				self.assertNotIn("ignore_permissions", reader.call_args.kwargs)
				self.assertIn("退货" if row.get("is_return") else "WO-1", result["detail"])
		self.doc.assert_not_called()

	def setUp(self):
		self.enterContext(patch.object(frappe, "flags", frappe._dict(), create=True))
		self.enterContext(
			patch.object(frappe, "session", SimpleNamespace(user="worker@example.com"), create=True)
		)
		self.db = self.enterContext(patch.object(frappe, "db", Mock(), create=True))
		self.roles = self.enterContext(patch.object(frappe, "get_roles", return_value=["Production Worker"]))
		self.doc = self.enterContext(patch.object(frappe, "get_doc"))
		self.worker = self.enterContext(patch.object(api, "require_worker"))
		self.employee = self.enterContext(patch.object(api, "employee_for_user", return_value="EMP-ME"))
		self.enterContext(patch.object(api, "get_scan_site_id", return_value=SITE_ID))
		self.enterContext(patch.object(api, "_", side_effect=lambda text: text))
		self.enterContext(patch.object(frappe, "throw", side_effect=self._throw))

	@staticmethod
	def _throw(message, exception=frappe.ValidationError):
		raise exception(message)

	def resolve(self, kind="job-card", name="JC-1"):
		return api.resolve(f"HSERP|1|{SITE_ID}|{kind}|{document_scan.encode_document_name(name)}")

	def test_namespace_and_legacy_code_are_checked_on_server_before_any_document_read(self):
		for code in (
			document_scan.document_scan_code("Purchase Order", "PO1", "b" * 32),
			"http://factory.example/desk/document-scan/purchase-order/UE8x",
			None,
		):
			with self.subTest(code=code), self.assertRaises(frappe.ValidationError):
				api.resolve(code)
		self.doc.assert_not_called()
		self.db.get_value.assert_not_called()

	def test_guest_and_invalid_types_cannot_read_documents(self):
		frappe.session.user = "Guest"
		with self.assertRaises(frappe.PermissionError):
			self.resolve()
		frappe.session.user = "worker@example.com"
		with self.assertRaises(frappe.ValidationError):
			self.resolve("Employee")
		self.doc.assert_not_called()
		self.db.get_value.assert_not_called()

	def test_purchase_requires_read_permission_before_returning_form_route(self):
		self.doc.return_value.name = "PO-1"
		result = self.resolve("purchase-order", "PO-1")
		self.doc.return_value.check_permission.assert_called_once_with("read")
		self.assertEqual(result, {"found": True, "route": ["Form", "Purchase Order", "PO-1"]})
		self.doc.return_value.insert.assert_not_called()
		self.doc.return_value.save.assert_not_called()
		self.doc.return_value.submit.assert_not_called()

	def test_missing_and_unreadable_purchase_order_share_a_non_disclosing_result(self):
		self.doc.side_effect = frappe.DoesNotExistError
		missing = self.resolve("purchase-order")
		self.doc.side_effect = None
		self.doc.return_value.check_permission.side_effect = frappe.PermissionError
		denied = self.resolve("purchase-order")
		self.assertEqual(missing, denied)
		self.assertFalse(denied["found"])
		self.assertIsNone(frappe.flags.mute_messages)

	def test_auto_type_detection_suppresses_expected_purchase_error_and_restores_message_flag(self):
		def missing(*args):
			self.assertTrue(frappe.flags.mute_messages)
			raise frappe.DoesNotExistError
		self.doc.side_effect = missing
		self.db.get_value.return_value = "ASSIGN-ME"
		self.assertTrue(api.find_by_name("JC-1")["found"])
		self.assertIsNone(frappe.flags.mute_messages)

	def test_worker_route_only_uses_current_employee_active_assignment(self):
		self.db.get_value.return_value = "ASSIGN-ME"
		for active in (False, True):
			self.db.exists.return_value = active
			result = self.resolve()
			self.assertEqual(
				result["route"], ["active-production-work" if active else "my-production-reporting"]
			)
			self.assertEqual(result["job_card"], "JC-1")
		self.db.get_value.assert_called_with(
			"Job Card Worker Assignment",
			{"employee": "EMP-ME", "job_card": "JC-1", "status": "Active"},
			"name",
		)
		self.db.exists.assert_called_with(
			"Job Card Work Report", {"assignment": "ASSIGN-ME", "status": "In Progress"}
		)
		self.doc.assert_not_called()
		self.db.set_value.assert_not_called()

	def test_other_workers_or_revoked_assignments_do_not_reveal_job_card(self):
		self.db.get_value.return_value = None
		self.assertEqual(self.resolve(), {"found": False, "message": api.NO_TASK_MESSAGE})
		self.db.exists.assert_not_called()
		self.doc.assert_not_called()

	def test_no_worker_role_or_employee_gets_the_same_hint(self):
		self.roles.return_value = ["System Manager"]
		without_role = self.resolve()
		self.employee.assert_not_called()
		self.roles.return_value = ["Production Worker"]
		self.employee.return_value = None
		self.assertEqual(self.resolve(), without_role)
		self.db.get_value.assert_not_called()

	def test_manual_purchase_number_resolves_without_a_qr_and_checks_read_permission(self):
		self.doc.return_value.name = "PUR-ORD-2026-00010"
		self.roles.return_value = ["System Manager"]
		result = api.find_by_name(" PUR-ORD-2026-00010 ")
		self.assertEqual(result["route"], ["Form", "Purchase Order", "PUR-ORD-2026-00010"])
		self.doc.assert_called_once_with("Purchase Order", "PUR-ORD-2026-00010")
		self.doc.return_value.check_permission.assert_called_once_with("read")

	def test_administrator_with_frappes_implicit_worker_role_still_finds_purchase_order(self):
		frappe.session.user = "Administrator"
		self.roles.return_value = ["System Manager", "Production Worker"]
		self.doc.return_value.name = "PO-1"
		self.assertEqual(api.find_by_name("PO-1")["route"], ["Form", "Purchase Order", "PO-1"])
		self.worker.assert_not_called()

	def test_manual_worker_number_uses_the_same_assignment_and_active_route(self):
		self.doc.side_effect = frappe.PermissionError
		self.db.get_value.return_value = "ASSIGN-ME"
		self.db.exists.return_value = True
		self.assertEqual(api.find_by_name("JC-1"), self.resolve())
		self.db.get_value.return_value = None
		self.assertFalse(api.find_by_name("JC-1")["found"])
		self.db.set_value.assert_not_called()

	def test_manual_missing_and_hidden_documents_have_identical_results(self):
		self.roles.return_value = []
		self.doc.side_effect = frappe.DoesNotExistError
		missing = api.find_by_name("PO-1")
		self.doc.side_effect = frappe.PermissionError
		self.assertEqual(api.find_by_name("PO-1"), missing)
		self.assertFalse(missing["found"])

	def test_manual_ambiguous_number_requires_type_without_choosing_a_different_document(self):
		self.doc.return_value.name = "SAME-1"
		self.db.get_value.return_value = "ASSIGN-ME"
		self.assertFalse(api.find_by_name("SAME-1")["found"])
		self.assertEqual(api.find_by_name("SAME-1", "Purchase Order")["route"], ["Form", "Purchase Order", "SAME-1"])
		self.assertEqual(api.find_by_name("SAME-1", "Job Card")["job_card"], "SAME-1")

	def test_manual_endpoint_rejects_guest_urls_payloads_invalid_names_and_unknown_types(self):
		frappe.session.user = "Guest"
		with self.assertRaises(frappe.PermissionError):
			api.find_by_name("PO1")
		frappe.session.user = "worker@example.com"
		for name in (None, "", "a" * 141, "PO\n1", "https://example.com", "javascript:foo", "//example.com", f"HSERP|1|{SITE_ID}|purchase-order|UE8x"):
			with self.subTest(name=name), self.assertRaises(frappe.ValidationError):
				api.find_by_name(name)
		with self.assertRaises(frappe.ValidationError):
			api.find_by_name("EMP-1", "Employee")
		self.doc.assert_not_called()
		self.db.get_value.assert_not_called()

	def test_search_lists_matches_without_opening_and_preserves_native_permission_queries(self):
		self.roles.return_value = ["System Manager"]
		rows = [frappe._dict(name="PO-JOB00208", item_name="焊线线圈", operation="焊接")]
		with patch.object(frappe, "get_list", side_effect=[rows, rows]) as reader:
			result = api.search_documents("PO-JOB00208", "Job Card")
		self.assertEqual([row["name"] for row in result["results"]], ["PO-JOB00208"])
		self.assertNotIn("route", result["results"][0])
		self.assertFalse(result["has_more"])
		self.assertEqual(reader.call_args.args[0], "Job Card")
		self.assertNotIn("ignore_permissions", reader.call_args.kwargs)
		self.doc.assert_not_called(); self.worker.assert_not_called()

	def test_search_is_bounded_and_prioritizes_exact_name_even_if_other_matches_are_newer(self):
		self.roles.return_value = []
		exact = frappe._dict(name="OLD-EXACT", supplier_name="某供应商")
		rows = [frappe._dict(name=f"OTHER-{i}") for i in range(21)]
		with patch.object(frappe, "get_list", side_effect=[[exact], rows]):
			result = api.search_documents("OLD-EXACT", "Purchase Order")
		self.assertEqual(len(result["results"]), 20)
		self.assertTrue(result["has_more"])
		self.assertEqual(result["results"][0]["name"], exact.name)

	def test_worker_search_can_only_read_rows_in_current_employee_active_assignments(self):
		def reader(doctype, **kwargs):
			if doctype == "Job Card Worker Assignment":
				self.assertEqual(kwargs["filters"], {"employee": "EMP-ME", "status": "Active"})
				return ["OWN-JOB"]
			self.assertEqual(doctype, "Job Card")
			if isinstance(kwargs["filters"], dict):
				self.assertEqual(kwargs["filters"]["name"], ["in", ["OWN-JOB"]])
			else:
				self.assertIn(["name", "in", ["OWN-JOB"]], kwargs["filters"])
			return [frappe._dict(name="OWN-JOB", item_name="已派物料")]
		with patch.object(frappe, "get_all", side_effect=reader), patch.object(frappe, "get_list", side_effect=frappe.PermissionError):
			result = api.search_documents("JOB")
		self.assertEqual([row["name"] for row in result["results"]], ["OWN-JOB"])
		self.worker.assert_called_once(); self.doc.assert_not_called()
		self.assertIsNone(frappe.flags.mute_messages)

	def test_no_employee_or_assignment_means_no_worker_job_search_and_no_raw_job_query(self):
		for employee in (None, "EMP-ME"):
			self.employee.return_value = employee
			with patch.object(frappe, "get_all", return_value=[]) as reader:
				self.assertEqual(api.search_documents("JOB", "Job Card")["results"], [])
				self.assertTrue(all(call.args[0] == "Job Card Worker Assignment" for call in reader.call_args_list))

	def test_search_treats_wildcards_literally_and_does_not_reveal_inaccessible_types(self):
		self.roles.return_value = []
		with patch.object(frappe, "get_list", side_effect=[[], []]) as reader:
			api.search_documents("A_%", "Job Card")
			self.assertIn(["name", "like", "%A\\_\\%%"], reader.call_args.kwargs["or_filters"])
		with patch.object(frappe, "get_list", side_effect=frappe.PermissionError):
			self.assertEqual(api.search_documents("JOB"), {"results": [], "has_more": False})
		self.assertIsNone(frappe.flags.mute_messages)

	def test_manager_click_uses_job_read_permission_instead_of_worker_assignment(self):
		frappe.session.user = "Administrator"
		self.doc.return_value.name = "PO-JOB00208"
		result = api.open_result("Job Card", "PO-JOB00208")
		self.assertEqual(result["route"], ["Form", "Job Card", "PO-JOB00208"])
		self.doc.return_value.check_permission.assert_called_once_with("read")
		self.worker.assert_not_called(); self.db.get_value.assert_not_called()

	def test_permissions_and_assignments_are_rechecked_at_click_after_search(self):
		self.roles.return_value = []
		with patch.object(frappe, "get_list", return_value=[frappe._dict(name="PO-1")]):
			self.assertTrue(api.search_documents("PO", "Purchase Order")["results"])
		self.doc.return_value.check_permission.side_effect = frappe.PermissionError
		self.assertFalse(api.open_result("Purchase Order", "PO-1")["found"])
		self.roles.return_value = ["Production Worker"]
		self.db.get_value.return_value = None
		self.assertEqual(api.open_result("Job Card", "JC1")["message"], api.NO_TASK_MESSAGE)
		self.db.exists.assert_not_called()

	def test_search_and_open_reject_guest_invalid_type_and_qr_payloads(self):
		with patch.object(frappe, "get_list") as reader:
			frappe.session.user = "Guest"
			for call in (lambda: api.search_documents("PO"), lambda: api.open_result("Job Card", "JC1")):
				with self.assertRaises(frappe.PermissionError): call()
			frappe.session.user = "worker@example.com"
			for call in (lambda: api.search_documents("", "Job Card"), lambda: api.search_documents("A", "Employee"), lambda: api.open_result("", "JC1"), lambda: api.search_documents(f"HSERP|1|{SITE_ID}|job-card|eA")):
				with self.assertRaises(frappe.ValidationError): call()
			reader.assert_not_called()
		self.doc.assert_not_called()
