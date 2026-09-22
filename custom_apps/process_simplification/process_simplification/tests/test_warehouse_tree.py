"""Real user permissions, hierarchy traversal and native RPC dispatch on a test site."""

from types import SimpleNamespace

import frappe
from frappe.handler import execute_cmd
from frappe.permissions import add_user_permission, remove_user_permission
from frappe.tests import IntegrationTestCase

from erpnext.stock.doctype.warehouse.warehouse import get_children as native_get_children
from process_simplification.api.access_management import set_user_access
from process_simplification.management_access import WAREHOUSE_OPERATOR_ROLE
from process_simplification.warehouse_tree import get_children


NATIVE_METHOD = "erpnext.stock.doctype.warehouse.warehouse.get_children"


class TestWarehouseTree(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.addCleanup(self._rollback)
		self.company = frappe.get_all("Company", pluck="name", limit=1)[0]
		self.prefix = "WH-TREE-" + frappe.generate_hash(length=8)
		self.root = self._warehouse("Root", is_group=1)
		self.group = self._warehouse("Group", parent=self.root, is_group=1)
		self.allowed = self._warehouse("Allowed", parent=self.group)
		self.forbidden = self._warehouse("Forbidden sibling", parent=self.group)
		self.hidden_group = self._warehouse("Hidden group", parent=self.root, is_group=1)
		self.hidden_leaf = self._warehouse("Hidden leaf", parent=self.hidden_group)
		self.user = frappe.get_doc(dict(
			doctype="User", email=self.prefix.lower() + "@example.test",
			first_name="Warehouse tree test", send_welcome_email=0,
			roles=[dict(role=WAREHOUSE_OPERATOR_ROLE)],
		)).insert().name
		set_user_access(user=self.user, roles=[WAREHOUSE_OPERATOR_ROLE],
			companies=[self.company], warehouses=[self.allowed])

	def _rollback(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		if getattr(self, "user", None):
			frappe.clear_cache(user=self.user)

	def _warehouse(self, label, parent=None, is_group=0, company=None):
		return frappe.get_doc(dict(
			doctype="Warehouse", warehouse_name=f"{self.prefix}-{label}",
			company=company or self.company, parent_warehouse=parent, is_group=is_group,
		)).insert().name

	def _children(self, parent=None, **kwargs):
		return get_children("Warehouse", parent=parent, company=self.company, **kwargs)

	def _rpc(self, method, **params):
		missing = object()
		previous = {key: getattr(frappe.local, key, missing) for key in ("form_dict", "request")}
		frappe.local.form_dict = frappe._dict(cmd=method, **params)
		frappe.local.request = SimpleNamespace(method="POST")
		try:
			return execute_cmd(method)
		finally:
			for key, value in previous.items():
				if value is missing:
					delattr(frappe.local, key)
				else:
					setattr(frappe.local, key, value)

	def test_leaf_scope_reproduces_native_empty_tree_and_reaches_leaf_through_override(self):
		with self.set_user(self.user):
			self.assertEqual(native_get_children("Warehouse", company=self.company, is_root=True), [])
			roots = self._rpc(NATIVE_METHOD, doctype="Warehouse", parent=self.company,
				company=self.company, is_root="true")
			self.assertEqual(roots, [dict(value=self.root, expandable=1, ps_navigation_only=True)])
			self.assertEqual(self._children(self.root),
				[dict(value=self.group, expandable=1, ps_navigation_only=True)])
			self.assertEqual(self._children(self.group, is_root="false"),
				[dict(value=self.allowed, expandable=0)])

	def test_expand_all_uses_override_and_never_returns_siblings_or_other_branches(self):
		with self.set_user(self.user):
			rows = self._rpc("frappe.desk.treeview.get_all_nodes", doctype="Warehouse",
				label=self.company, parent=self.company, tree_method=NATIVE_METHOD,
				company=self.company, is_root=True)
			values = {node["value"] for branch in rows for node in branch["data"]}
			self.assertEqual(values, {self.root, self.group, self.allowed})
			self.assertEqual(self._children(self.hidden_group), [])

	def test_navigation_does_not_change_user_permissions_document_access_or_list(self):
		before = frappe.get_all("User Permission", filters={"user": self.user},
			fields=["name", "allow", "for_value", "applicable_for", "hide_descendants"], order_by="name")
		with self.set_user(self.user):
			self._children(is_root=True)
			self._children(self.root)
			self.assertEqual(frappe.get_list("Warehouse", pluck="name"), [self.allowed])
			for name in (self.root, self.group, self.forbidden, self.hidden_leaf):
				with self.subTest(warehouse=name):
					self.assertFalse(frappe.has_permission("Warehouse", "read", doc=name))
					self.assertFalse(frappe.has_permission("Warehouse", "write", doc=name))
					with self.assertRaises(frappe.PermissionError):
						frappe.get_doc("Warehouse", name).check_permission("read")
		after = frappe.get_all("User Permission", filters={"user": self.user},
			fields=["name", "allow", "for_value", "applicable_for", "hide_descendants"], order_by="name")
		self.assertEqual(before, after)

	def test_disabled_warehouse_only_contributes_a_path_when_requested(self):
		frappe.db.set_value("Warehouse", self.allowed, "disabled", 1)
		with self.set_user(self.user):
			self.assertEqual(self._children(is_root=True, include_disabled="false"), [])
			self.assertEqual(self._children(is_root=True, include_disabled="true")[0].value, self.root)
			self.assertEqual(self._children(self.group, include_disabled="true")[0].value, self.allowed)

	def test_disabled_ancestor_stays_hidden_until_include_disabled(self):
		frappe.db.set_value("Warehouse", self.group, "disabled", 1)
		with self.set_user(self.user):
			self.assertEqual(self._children(is_root=True), [])
			self.assertEqual(self._children(is_root=True, include_disabled=True)[0].value, self.root)

	def test_company_permission_still_filters_an_explicitly_granted_foreign_warehouse(self):
		other_company = frappe.get_doc(dict(
			doctype="Company", company_name=self.prefix + " Other company", abbr="WT" + frappe.generate_hash(length=3),
			default_currency="CNY", country="China", create_chart_of_accounts_based_on="Standard Template",
		)).insert().name
		other_root = self._warehouse("Other root", company=other_company, is_group=1)
		other_leaf = self._warehouse("Other leaf", company=other_company, parent=other_root)
		add_user_permission("Warehouse", other_leaf, self.user)
		with self.set_user(self.user):
			self.assertEqual(get_children("Warehouse", company=other_company, is_root=True), [])
			self.assertEqual(get_children("Warehouse", parent=other_root, company=other_company), [])
			self.assertEqual(self._children(is_root=True)[0].value, self.root)

	def test_user_without_warehouse_read_cannot_obtain_ancestor_names(self):
		doc = frappe.get_doc("User", self.user)
		doc.set("role_profiles", [])
		doc.role_profile_name = None
		doc.set("roles", [])
		doc.save()
		with self.set_user(self.user):
			self.assertFalse(frappe.has_permission("Warehouse", "read"))
			with self.assertRaises(frappe.PermissionError):
				self._children(is_root=True)

	def test_explicit_group_grants_keep_native_descendant_behavior(self):
		remove_user_permission("Warehouse", self.allowed, self.user)
		add_user_permission("Warehouse", self.group, self.user)
		with self.set_user(self.user):
			self.assertEqual(self._children(self.root), [dict(value=self.group, expandable=1)])
			self.assertEqual({row.value for row in self._children(self.group)}, {self.allowed, self.forbidden})
			self.assertEqual(self._children(self.hidden_group), [])

	def test_administrator_and_users_without_warehouse_scope_keep_native_results(self):
		self.assertEqual(self._children(is_root=True),
			native_get_children("Warehouse", company=self.company, is_root=True))
		remove_user_permission("Warehouse", self.allowed, self.user)
		with self.set_user(self.user):
			self.assertEqual(self._children(self.root),
				native_get_children("Warehouse", parent=self.root, company=self.company))

	def test_restricted_result_is_not_truncated_at_twenty_warehouses(self):
		allowed = {self.allowed}
		for index in range(22):
			name = self._warehouse(f"Extra {index:02}", parent=self.group)
			add_user_permission("Warehouse", name, self.user)
			allowed.add(name)
		with self.set_user(self.user):
			self.assertEqual({row.value for row in self._children(self.group)}, allowed)

	def test_unrelated_doctypes_are_rejected(self):
		with self.set_user(self.user), self.assertRaises(frappe.PermissionError):
			get_children("Company", company=self.company, is_root=True)
