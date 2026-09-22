"""Group grants and warmed permission caches on an isolated site."""

from unittest.mock import patch

import frappe
from frappe.core.doctype.user_permission.user_permission import get_user_permissions
from frappe.tests import IntegrationTestCase

from process_simplification.api.access_management import get_user_access, set_user_access
from process_simplification.management_access import WAREHOUSE_OPERATOR_ROLE


class TestWarehouseGroupScope(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.addCleanup(self._rollback)
		self.users = []
		self.company = frappe.get_all("Company", pluck="name", limit=1)[0]
		self.prefix = "WH-SCOPE-" + frappe.generate_hash(length=8)
		self.root = self._warehouse("Root", is_group=1)
		self.group = self._warehouse("Allowed group", self.root, is_group=1)
		self.nested = self._warehouse("Nested group", self.group, is_group=1)
		self.leaf = self._warehouse("Allowed leaf", self.nested)
		self.other_group = self._warehouse("Other group", self.root, is_group=1)
		self.other_leaf = self._warehouse("Other leaf", self.other_group)
		self.user = self._user()

	def _rollback(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for user in self.users:
			frappe.clear_cache(user=user)

	def _warehouse(self, label, parent=None, is_group=0):
		return frappe.get_doc(dict(
			doctype="Warehouse", warehouse_name=f"{self.prefix}-{label}",
			company=self.company, parent_warehouse=parent, is_group=is_group,
		)).insert().name

	def _user(self):
		user = frappe.get_doc(dict(
			doctype="User", email=frappe.generate_hash(length=12) + "@example.test",
			first_name="Warehouse scope test", send_welcome_email=0,
			roles=[dict(role=WAREHOUSE_OPERATOR_ROLE)],
		)).insert().name
		self.users.append(user)
		return user

	def _grant(self, *warehouses, user=None, **kwargs):
		return set_user_access(
			user=user or self.user, roles=[WAREHOUSE_OPERATOR_ROLE],
			companies=[self.company], warehouses=list(warehouses), **kwargs,
		)

	def _allowed(self, user=None):
		return {row.doc for row in get_user_permissions(user or self.user).get("Warehouse", [])}

	def _move(self, warehouse, parent):
		doc = frappe.get_doc("Warehouse", warehouse)
		doc.parent_warehouse = parent
		doc.save()

	def test_scope_options_include_groups_and_hierarchy_and_exclude_disabled(self):
		disabled = frappe.get_doc("Warehouse", self.other_leaf)
		disabled.disabled = 1
		disabled.save()
		data = get_user_access(self.user)
		options = {row.name: row for row in data["scope_options"]["warehouses"]}
		self.assertTrue(options[self.group].is_group)
		self.assertEqual(options[self.nested].parent_warehouse, self.group)
		self.assertLess(options[self.group].lft, options[self.leaf].lft)
		self.assertNotIn(self.other_leaf, options)

	def test_group_grant_saves_one_rule_and_round_trips_without_leaf_snapshot(self):
		result = self._grant(self.group, self.nested, self.leaf)
		self.assertEqual(result["warehouses"], [self.group])
		self.assertEqual(get_user_access(self.user)["warehouses"], [self.group])
		self.assertEqual(frappe.get_all("User Permission", filters={"user": self.user, "allow": "Warehouse"},
			pluck="for_value"), [self.group])
		self.assertEqual(self._allowed(), {self.group, self.nested, self.leaf})
		with self.set_user(self.user):
			self.assertEqual(set(frappe.get_list("Warehouse", pluck="name")), self._allowed())
			self.assertFalse(frappe.has_permission("Warehouse", "read", doc=self.other_leaf))

	def test_new_descendants_refresh_all_scoped_users_without_resaving_access(self):
		second = self._user()
		self._grant(self.group)
		self._grant(self.group, user=second)
		for user in self.users:
			self.assertEqual(self._allowed(user), {self.group, self.nested, self.leaf})
		new_leaf = self._warehouse("New descendant", self.nested)
		for user in self.users:
			self.assertIn(new_leaf, self._allowed(user))
			self.assertTrue(frappe.has_permission("Warehouse", "read", doc=new_leaf, user=user))
			self.assertNotIn(self.other_leaf, self._allowed(user))
			self.assertEqual(get_user_access(user)["warehouses"], [self.group])

	def test_moving_subtree_revokes_old_group_and_grants_new_group_with_warm_caches(self):
		second = self._user()
		self._grant(self.group)
		self._grant(self.other_group, user=second)
		self.assertIn(self.leaf, self._allowed())
		self.assertNotIn(self.leaf, self._allowed(second))
		self._move(self.nested, self.other_group)
		self.assertEqual(self._allowed(), {self.group})
		self.assertTrue({self.nested, self.leaf}.issubset(self._allowed(second)))
		self.assertFalse(frappe.has_permission("Warehouse", "read", doc=self.leaf, user=self.user))
		self.assertTrue(frappe.has_permission("Warehouse", "read", doc=self.leaf, user=second))

	def test_moving_leaf_in_then_out_updates_native_stock_document_permissions(self):
		self._grant(self.group)
		entry = frappe.get_doc(dict(doctype="Stock Entry", company=self.company,
			items=[dict(t_warehouse=self.other_leaf)]))
		self.assertFalse(frappe.has_permission("Stock Entry", "read", doc=entry, user=self.user))
		self._move(self.other_leaf, self.group)
		self.assertTrue(frappe.has_permission("Stock Entry", "read", doc=entry, user=self.user))
		self._move(self.other_leaf, self.other_group)
		self.assertFalse(frappe.has_permission("Stock Entry", "read", doc=entry, user=self.user))

	def test_leaf_only_grant_never_expands_to_siblings_or_new_warehouses(self):
		self._grant(self.leaf)
		self.assertEqual(self._allowed(), {self.leaf})
		self._warehouse("New sibling", self.nested)
		self._move(self.leaf, self.other_group)
		self.assertEqual(self._allowed(), {self.leaf})
		self.assertEqual(get_user_access(self.user)["warehouses"], [self.leaf])

	def test_existing_exact_group_grant_stays_exact_until_explicitly_expanded(self):
		self._grant(self.group, self.leaf, warehouse_hide_descendants=[self.group])
		self.assertEqual(self._allowed(), {self.group, self.leaf})
		self.assertEqual(get_user_access(self.user)["warehouse_hide_descendants"], [self.group])
		# Legacy API callers omit the option; unrelated saves retain the restriction.
		self._grant(self.group, self.leaf)
		self.assertEqual(self._allowed(), {self.group, self.leaf})
		self._grant(self.group, self.leaf, warehouse_hide_descendants=[])
		self.assertEqual(self._allowed(), {self.group, self.nested, self.leaf})
		self.assertEqual(get_user_access(self.user)["warehouses"], [self.group])

	def test_empty_group_can_be_granted_before_warehouses_are_created(self):
		empty = self._warehouse("Empty group", self.root, is_group=1)
		self._grant(empty)
		self.assertEqual(self._allowed(), {empty})
		first = self._warehouse("First warehouse", empty)
		self.assertEqual(self._allowed(), {empty, first})

	def test_disabled_and_out_of_company_groups_are_rejected_before_permissions_change(self):
		self._grant(self.leaf)
		doc = frappe.get_doc("Warehouse", self.group)
		doc.disabled = 1
		doc.save()
		with self.assertRaises(frappe.ValidationError):
			self._grant(self.group)
		doc.disabled = 0
		doc.save()
		with self.assertRaises(frappe.ValidationError):
			set_user_access(user=self.user, roles=[WAREHOUSE_OPERATOR_ROLE], companies=[], warehouses=[self.group])
		self.assertEqual(get_user_access(self.user)["warehouses"], [self.leaf])

	def test_deleting_inherited_leaf_removes_it_from_warm_cache(self):
		self._grant(self.group)
		self.assertIn(self.leaf, self._allowed())
		frappe.delete_doc("Warehouse", self.leaf)
		self.assertNotIn(self.leaf, self._allowed())

	def test_disabled_existing_grant_remains_visible_so_it_can_be_removed(self):
		self._grant(self.leaf)
		doc = frappe.get_doc("Warehouse", self.leaf)
		doc.disabled = 1
		doc.save()
		options = {row.name: row for row in get_user_access(self.user)["scope_options"]["warehouses"]}
		self.assertTrue(options[self.leaf].disabled)
		self._grant(self.other_leaf)
		self.assertNotIn(self.leaf, {row.name for row in get_user_access(self.user)["scope_options"]["warehouses"]})

	def test_group_from_another_company_cannot_be_granted(self):
		other_company = frappe.get_doc(dict(
			doctype="Company", company_name=self.prefix + " Other company",
			abbr="GS" + frappe.generate_hash(length=3), default_currency="CNY", country="China",
			create_chart_of_accounts_based_on="Standard Template",
		)).insert().name
		other_root = frappe.db.get_value("Warehouse", {"company": other_company, "is_group": 1}, "name")
		self._grant(self.group)
		with self.assertRaises(frappe.ValidationError):
			self._grant(other_root)
		self.assertEqual(get_user_access(self.user)["warehouses"], [self.group])

	def test_renaming_group_preserves_descendants_and_refreshes_the_stored_grant(self):
		self._grant(self.group)
		self.assertIn(self.group, self._allowed())
		new_name = self.group.replace("Allowed group", "Renamed group")
		frappe.rename_doc("Warehouse", self.group, new_name, force=True, rebuild_search=False)
		self.assertEqual(get_user_access(self.user)["warehouses"], [new_name])
		self.assertEqual(self._allowed(), {new_name, self.nested, self.leaf})
		self.assertEqual(frappe.db.get_value("Warehouse", self.nested, "parent_warehouse"), new_name)

	def test_removing_group_grant_and_retaining_one_leaf_revokes_the_other_descendants(self):
		self._grant(self.group)
		self.assertIn(self.nested, self._allowed())
		self._grant(self.leaf)
		self.assertEqual(self._allowed(), {self.leaf})

	def test_cache_refilled_before_commit_is_cleared_again_and_clients_are_notified(self):
		self._grant(self.group)
		stale = get_user_permissions(self.user)
		new_leaf = self._warehouse("Commit boundary", self.group)
		# Reproduce another request refilling Redis from the old committed tree.
		frappe.cache.hset("user_permissions", self.user, stale)
		self.assertNotIn(new_leaf, self._allowed())
		with patch.object(frappe, "publish_realtime") as publish:
			frappe.db.after_commit.run()
			publish.assert_any_call("update_user_permissions", user=self.user)
		self.assertIn(new_leaf, self._allowed())

	def test_rollback_callback_discards_permissions_cached_from_unsaved_tree(self):
		self._grant(self.group)
		self._warehouse("Rolled back child", self.group)
		self._allowed()
		self.assertIsNotNone(frappe.cache.hget("user_permissions", self.user))
		frappe.db.after_rollback.run()
		self.assertIsNone(frappe.cache.hget("user_permissions", self.user))
