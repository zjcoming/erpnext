from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from process_simplification import operation_dispatch
from process_simplification.management_access import CAPABILITY_PRODUCTION_REVIEW, CAPABILITY_WAREHOUSE_WORKBENCH


class TestOperationDispatchBoot(TestCase):
	def test_unconfigured_site_exposes_both_views_to_administrator(self):
		with (
			patch.object(frappe, "conf", frappe._dict()),
			patch.object(frappe, "session", SimpleNamespace(user="Administrator")),
		):
			boot = {}
			operation_dispatch.boot_session(boot)
		self.assertIs(boot["enable_operation_dispatch_pool"], True)
		self.assertIs(boot["enable_production_materials"], True)

	def test_disabled_rollout_preserves_the_old_entry_without_permission_lookup(self):
		boot = {"enable_operation_dispatch_pool": True}
		with (
			patch("process_simplification.production_reporting.dispatch_pool.is_enabled", return_value=False),
			patch.object(operation_dispatch, "user_has_capability") as capability,
		):
			operation_dispatch.boot_session(boot)
		self.assertIs(boot["enable_operation_dispatch_pool"], False)
		capability.assert_not_called()

	def test_enabled_rollout_uses_the_existing_review_capability(self):
		for allowed in (False, True):
			with (
				self.subTest(allowed=allowed),
				patch("process_simplification.production_reporting.dispatch_pool.is_enabled", return_value=True),
				patch.object(frappe, "session", SimpleNamespace(user="operator@example.test")),
				patch.object(operation_dispatch, "user_has_capability", return_value=allowed) as capability,
			):
				boot = {}
				operation_dispatch.boot_session(boot)
				self.assertIs(boot["enable_operation_dispatch_pool"], allowed)
				self.assertEqual(capability.call_args_list[0].args, (CAPABILITY_PRODUCTION_REVIEW,))
				self.assertIs(boot["enable_production_materials"], allowed)

	def test_guest_does_not_get_the_new_view_even_when_enabled(self):
		with (
			patch("process_simplification.production_reporting.dispatch_pool.is_enabled", return_value=True),
			patch.object(frappe, "session", SimpleNamespace(user="Guest")),
			patch.object(operation_dispatch, "user_has_capability") as capability,
		):
			boot = {}
			operation_dispatch.boot_session(boot)
		self.assertIs(boot["enable_operation_dispatch_pool"], False)
		capability.assert_not_called()

	def test_warehouse_only_gets_materials_without_dispatch_authority(self):
		with (
			patch("process_simplification.production_reporting.dispatch_pool.is_enabled", return_value=True),
			patch.object(frappe, "session", SimpleNamespace(user="warehouse@example.test")),
			patch.object(operation_dispatch, "user_has_capability", side_effect=lambda name: name == CAPABILITY_WAREHOUSE_WORKBENCH),
		):
			boot = {}
			operation_dispatch.boot_session(boot)
		self.assertIs(boot["enable_operation_dispatch_pool"], False)
		self.assertIs(boot["enable_production_materials"], True)
