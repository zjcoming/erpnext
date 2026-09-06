from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import random_string

from process_simplification import pwa
from process_simplification.management_access import ROLE_DEFINITION_BY_ROLE, WAGE_MANAGER_ROLE


class TestPWAURLs(UnitTestCase):
	def test_https_origin_can_be_changed_without_code_and_is_normalized(self):
		for value, expected in (
			("", ""),
			(" https://ERP.example.com:443/ ", "https://erp.example.com"),
			("https://factory.example.com:8443", "https://factory.example.com:8443"),
			("https://192.168.5.100", "https://192.168.5.100"),
			("https://[::1]:8443/", "https://[::1]:8443"),
		):
			with self.subTest(value=value):
				self.assertEqual(pwa.normalize_https_url(value), expected)

	def test_rejects_http_credentials_paths_and_script_urls(self):
		for value in (
			"http://erp.example.com", "erp.example.com", "javascript:alert(1)",
			"https://user:password@erp.example.com", "https://erp.example.com/desk",
			"https://erp.example.com?next=evil", "https://erp.example.com#desk",
			"https://erp.example.com:99999", "https://erp.example.com\\@evil.example",
			"https://erp.example.com\n.evil.example", "https://-invalid.example",
		):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				pwa.normalize_https_url(value)

	def test_manifest_uses_same_origin_and_its_own_identity_even_with_an_external_https_setting(self):
		settings = frappe._dict({**pwa.PWA_DEFAULTS, "pwa_https_url": "https://factory.example.com"})
		with patch.object(pwa, "_settings", return_value=settings):
			response = pwa.manifest()
			data = json.loads(response.get_data(as_text=True))
			self.assertEqual(response.mimetype, "application/manifest+json")
			self.assertEqual(response.headers["Cache-Control"], "no-store")
			self.assertNotEqual(data["id"], "/hrms")
			self.assertTrue(data["start_url"].startswith(data["scope"]))
			self.assertFalse("/hrms".startswith(data["scope"]))
			self.assertTrue(all(icon["src"].startswith("/assets/process_simplification/") for icon in data["icons"]))
			self.assertNotIn("https://factory.example.com", response.get_data(as_text=True))
			settings.pwa_app_name = "新的工厂名称"
			changed = json.loads(pwa.manifest().get_data(as_text=True))
			self.assertEqual(changed["name"], settings.pwa_app_name)
			self.assertEqual(changed["id"], data["id"])
			settings.enable_pwa = 0
			self.assertEqual(pwa.manifest().status_code, 404)

	def test_worker_is_javascript_with_explicit_desk_scope(self):
		response = pwa.service_worker()
		self.assertEqual(response.mimetype, "application/javascript")
		self.assertEqual(response.headers["Service-Worker-Allowed"], "/desk")
		self.assertIn("no-cache", response.headers["Cache-Control"])

	def test_upgrade_initializes_missing_check_rows_and_preserves_stored_zero(self):
		with (
			patch("frappe.db.get_singles_dict", return_value={"enable_pwa": "0"}),
			patch("frappe.db.set_single_value") as write,
		):
			pwa.ensure_defaults()
			self.assertNotIn("enable_pwa", [call.args[1] for call in write.call_args_list])
			write.assert_any_call(pwa.SETTINGS_DOCTYPE, "pwa_install_prompt", 1)


class TestPWASettings(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.clear_cache()
		super().tearDown()

	def make_user(self, role):
		email = f"ps-pwa-{random_string(10).lower()}@example.com"
		user = {"doctype": "User", "email": email, "first_name": "PWA Test", "send_welcome_email": 0}
		if role in ROLE_DEFINITION_BY_ROLE:
			user["role_profiles"] = [{"role_profile": ROLE_DEFINITION_BY_ROLE[role]["profile"]}]
		else:
			user["roles"] = [{"role": role}]
		frappe.get_doc(user).insert(ignore_permissions=True)
		return email

	def test_owner_settings_take_effect_in_public_manifest_and_boot(self):
		settings = frappe.get_single(pwa.SETTINGS_DOCTYPE)
		settings.pwa_app_name = "生产安装验证"
		settings.pwa_https_url = "https://ERP.example.com/"
		settings.enable_pwa = 1
		settings.save()
		self.assertEqual(pwa.get_config()["https_url"], "https://erp.example.com")
		boot = frappe._dict()
		pwa.boot_session(boot)
		self.assertTrue(boot.process_pwa["available"])
		frappe.set_user("Guest")
		manifest = json.loads(pwa.manifest().get_data(as_text=True))
		self.assertEqual(manifest["name"], "生产安装验证")
		self.assertNotIn("notification_recipients", manifest)
		self.assertNotIn("user", manifest)

	def test_worker_can_install_without_settings_access(self):
		worker = self.make_user("Production Worker")
		frappe.set_user(worker)
		self.assertFalse(frappe.has_permission(pwa.SETTINGS_DOCTYPE, "read"))
		self.assertTrue(pwa.get_config()["available"])
		with self.assertRaises(frappe.PermissionError):
			pwa.get_status()

	def test_wage_manager_cannot_change_pwa_but_can_save_wage_setting(self):
		manager = self.make_user(WAGE_MANAGER_ROLE)
		settings = frappe.get_single(pwa.SETTINGS_DOCTYPE)
		old_name = settings.pwa_app_name
		old_manual = settings.allow_manual_time_entry
		frappe.set_user(manager)
		settings.pwa_app_name = "不能生效的名称"
		settings.allow_manual_time_entry = not old_manual
		settings.save()
		self.assertEqual(frappe.db.get_single_value(pwa.SETTINGS_DOCTYPE, "pwa_app_name"), old_name)
		self.assertEqual(frappe.db.get_single_value(pwa.SETTINGS_DOCTYPE, "allow_manual_time_entry"), int(not old_manual))
		with self.assertRaises(frappe.PermissionError):
			pwa.require_settings_access()

	def test_defaults_preserve_disabled_install_and_reminder_switches(self):
		settings = frappe.get_single(pwa.SETTINGS_DOCTYPE)
		settings.enable_pwa = 0
		settings.pwa_install_prompt = 0
		settings.save()
		pwa.ensure_defaults()
		self.assertEqual(frappe.db.get_single_value(pwa.SETTINGS_DOCTYPE, "enable_pwa"), 0)
		self.assertEqual(frappe.db.get_single_value(pwa.SETTINGS_DOCTYPE, "pwa_install_prompt"), 0)
		self.assertEqual(pwa.manifest().status_code, 404)

	def test_invalid_name_and_prompt_interval_are_rejected(self):
		for field, value in (("pwa_short_name", "x" * 13), ("pwa_prompt_interval_days", 0), ("pwa_prompt_interval_days", 366)):
			settings = frappe.get_single(pwa.SETTINGS_DOCTYPE)
			setattr(settings, field, value)
			with self.subTest(field=field, value=value), self.assertRaises(frappe.ValidationError):
				settings.save()
