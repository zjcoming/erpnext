from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from process_simplification import role_landing as landing


def settings(*rows, enabled=1):
	return frappe._dict(
		enable_role_landing=enabled,
		role_landing_pages=[
			frappe._dict(role=role, landing_page=page, enabled=active, idx=index)
			for index, (role, page, active) in enumerate(rows, 1)
		],
	)


class TestRoleLanding(UnitTestCase):
	def resolve(self, config, roles, links=None):
		with patch.object(landing, "user_has_capability", return_value=True):
			return landing.resolve_landing(
				config, roles, links if links is not None else {value[:2] for value in landing.DESTINATIONS.values()}
			)

	def test_multiple_roles_follow_configured_order_and_can_be_reordered(self):
		config = settings(("老板", "经营总览", 1), ("销售人员", "订单工作台", 1))
		roles = [landing.SALES_OPERATOR_ROLE, landing.OWNER_ROLE]
		self.assertEqual(self.resolve(config, roles)["route"], "executive-dashboard")
		config.role_landing_pages.reverse()
		self.assertEqual(self.resolve(config, roles)["route"], "order-workbench")

	def test_disabled_and_inaccessible_rows_fall_through(self):
		config = settings(("老板", "经营总览", 1), ("销售人员", "订单工作台", 1))
		roles = [landing.OWNER_ROLE, landing.SALES_OPERATOR_ROLE]
		self.assertEqual(self.resolve(config, roles, {("Page", "order-workbench")})["route"], "order-workbench")
		config.role_landing_pages[0].enabled = 0
		self.assertEqual(self.resolve(config, roles)["route"], "order-workbench")

	def test_no_match_disabled_feature_or_no_permission_keeps_native_entry(self):
		config = settings(("老板", "经营总览", 1))
		self.assertIsNone(self.resolve(config, [landing.WORKER_ROLE]))
		self.assertIsNone(self.resolve(config, [landing.OWNER_ROLE], set()))
		config.enable_role_landing = 0
		self.assertIsNone(self.resolve(config, [landing.OWNER_ROLE]))

	def test_sidebar_visibility_does_not_bypass_business_capability(self):
		with patch.object(landing, "user_has_capability", return_value=False):
			self.assertIsNone(landing.resolve_landing(
				settings(("系统管理员", "经营总览", 1)), [landing.SYSTEM_MANAGER_ROLE],
				{("Page", "executive-dashboard")},
			))

	def test_duplicate_roles_and_arbitrary_urls_are_rejected(self):
		for config in (
			settings(("老板", "经营总览", 1), ("老板", "订单工作台", 1)),
			settings(("老板", "https://example.com", 1)),
			settings(("不存在的岗位", "经营总览", 1)),
		):
			with self.assertRaises(frappe.ValidationError):
				landing.validate_settings(config)

	def test_upgrade_preserves_empty_or_disabled_configuration(self):
		for stored in ({"enable_role_landing": "0"}, {"enable_role_landing": "1"}):
			with patch("frappe.db.get_singles_dict", return_value=stored), patch.object(frappe, "get_single") as get:
				landing.ensure_defaults()
				get.assert_not_called()


class TestRoleLandingIntegration(IntegrationTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.clear_cache()
		super().tearDown()

	def test_fresh_install_seeds_rows_after_single_defaults_are_persisted(self):
		frappe.set_user("Administrator")
		doc = frappe.get_single(landing.SETTINGS_DOCTYPE)
		doc.enable_role_landing = 1
		doc.set("role_landing_pages", [])
		doc.save()
		self.assertIn("enable_role_landing", frappe.db.get_singles_dict(landing.SETTINGS_DOCTYPE))
		landing.ensure_defaults(new_install=True)
		stored = frappe.get_single(landing.SETTINGS_DOCTYPE)
		self.assertEqual([(row.role, row.landing_page) for row in stored.role_landing_pages], list(landing.DEFAULT_PAGES))
		stored.role_landing_pages.reverse()
		stored.save()
		landing.ensure_defaults(new_install=True)
		self.assertEqual([row.role for row in frappe.get_single(landing.SETTINGS_DOCTYPE).role_landing_pages], [role for role, _ in reversed(landing.DEFAULT_PAGES)])

	def test_upgrade_keeps_saved_empty_and_disabled_choices(self):
		frappe.set_user("Administrator")
		for enabled in (0, 1):
			doc = frappe.get_single(landing.SETTINGS_DOCTYPE)
			doc.enable_role_landing = enabled
			doc.set("role_landing_pages", [])
			doc.save()
			landing.ensure_defaults()
			stored = frappe.get_single(landing.SETTINGS_DOCTYPE)
			self.assertEqual(stored.enable_role_landing, enabled)
			self.assertEqual(stored.role_landing_pages, [])

	def test_existing_role_users_receive_permitted_home_pages(self):
		from frappe.boot import get_bootinfo

		frappe.set_user("Administrator")
		doc = frappe.get_single(landing.SETTINGS_DOCTYPE)
		doc.enable_role_landing = 1
		doc.set("role_landing_pages", [
			{"role": role, "landing_page": page, "enabled": 1} for role, page in landing.DEFAULT_PAGES
		])
		doc.save()
		users = {
			"qa.owner@ps.test": "executive-dashboard",
			"qa.production@ps.test": "production-workbench",
			"qa.warehouse@ps.test": "warehouse-workbench",
			"qa.sales@ps.test": "order-workbench",
			"qa.worker@ps.test": "my-production-reporting",
			"qa.wage@ps.test": "monthly-worker-wage-summary",
			"qa.access@ps.test": "process-access-management",
		}
		if not all(frappe.db.exists("User", {"name": user, "enabled": 1}) for user in users):
			self.skipTest("Existing QA users are required; this test does not create accounts")
		for user, route in users.items():
			with self.subTest(user=user):
				frappe.set_user(user)
				boot = get_bootinfo()
				self.assertEqual(boot.process_role_landing["route"], route)
				# Users can hide their desktop icon; generic login must still work.
				for icon in boot.desktop_icons:
					if icon.get("app") == "process_simplification" and icon.get("icon_type") == "App":
						self.assertTrue(icon["link"].startswith(f"/desk/{route}?"))
				self.assertEqual(frappe.get_doc("Workspace Sidebar", "Process Simplification").items[0].link_to, "process-simplification")

	def test_settings_save_clears_cached_boot_and_reordering_is_retained(self):
		frappe.set_user("Administrator")
		doc = frappe.get_single(landing.SETTINGS_DOCTYPE)
		# An empty, intentionally disabled configuration cannot exercise reordering.
		doc.set("role_landing_pages", [
			{"role": role, "landing_page": page, "enabled": 1}
			for role, page in landing.DEFAULT_PAGES
		])
		doc.save()
		with patch.object(frappe, "clear_cache") as clear:
			doc.role_landing_pages.reverse()
			doc.save()
			clear.assert_called()
		self.assertEqual(
			[row.role for row in frappe.get_single(landing.SETTINGS_DOCTYPE).role_landing_pages],
			[row.role for row in doc.role_landing_pages],
		)

	def test_home_settings_are_restricted_to_owner_and_system_manager(self):
		meta = frappe.get_meta(landing.SETTINGS_DOCTYPE)
		for field in landing.LANDING_FIELDS:
			self.assertEqual(meta.get_field(field).permlevel, 1)
		self.assertEqual(
			{row.role for row in meta.permissions if row.permlevel == 1 and row.write},
			{landing.OWNER_ROLE, landing.SYSTEM_MANAGER_ROLE},
		)

	def test_wage_manager_cannot_change_home_settings_when_saving_wage_settings(self):
		if not frappe.db.exists("User", {"name": "qa.wage@ps.test", "enabled": 1}):
			self.skipTest("Existing wage QA user is required")
		frappe.set_user("Administrator")
		doc = frappe.get_single(landing.SETTINGS_DOCTYPE)
		original_enabled = doc.enable_role_landing
		original_roles = [row.role for row in doc.role_landing_pages]
		frappe.set_user("qa.wage@ps.test")
		doc.enable_role_landing = int(not original_enabled)
		doc.set("role_landing_pages", [])
		doc.allow_manual_time_entry = int(not doc.allow_manual_time_entry)
		doc.save()
		stored = frappe.get_single(landing.SETTINGS_DOCTYPE)
		self.assertEqual(stored.enable_role_landing, original_enabled)
		self.assertEqual([row.role for row in stored.role_landing_pages], original_roles)
