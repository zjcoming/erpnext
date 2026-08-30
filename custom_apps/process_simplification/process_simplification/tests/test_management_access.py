from __future__ import annotations

from frappe.tests import IntegrationTestCase
from frappe.utils import random_string

import frappe

from process_simplification.api.access_management import set_user_access
from process_simplification.management_access import (
	ACCESS_MANAGER_ROLE,
	OWNER_ROLE,
	PRODUCTION_MANAGER_ROLE,
	SALES_OPERATOR_ROLE,
	SUPERVISOR_ROLE,
	WAGE_MANAGER_ROLE,
	WAREHOUSE_OPERATOR_ROLE,
	WORKER_ROLE,
	ensure_management_access,
	has_owner_access,
	require_owner_access,
)


class TestManagementAccess(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		ensure_management_access()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		super().tearDown()

	def _make_user(self, *roles):
		email = f"ps-access-{random_string(10).lower()}@example.com"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Process Access Test",
				"send_welcome_email": 0,
				"roles": [{"role": role} for role in roles],
			}
		).insert(ignore_permissions=True)
		return email

	def test_owner_dashboard_requires_owner_role_not_system_manager(self):
		system_manager = self._make_user("System Manager")
		frappe.set_user(system_manager)
		self.assertFalse(has_owner_access())
		with self.assertRaises(frappe.PermissionError):
			require_owner_access()

		owner = self._make_user(OWNER_ROLE)
		frappe.set_user(owner)
		self.assertTrue(has_owner_access())
		require_owner_access()

	def test_access_update_preserves_non_app_roles(self):
		target = self._make_user("Sales User", WAREHOUSE_OPERATOR_ROLE)
		set_user_access(target, [SALES_OPERATOR_ROLE, PRODUCTION_MANAGER_ROLE])

		explicit_roles = {row.role for row in frappe.get_doc("User", target).roles}
		self.assertIn("Sales User", explicit_roles)
		self.assertIn(SALES_OPERATOR_ROLE, explicit_roles)
		self.assertIn(PRODUCTION_MANAGER_ROLE, explicit_roles)
		self.assertNotIn(WAREHOUSE_OPERATOR_ROLE, explicit_roles)

	def test_access_manager_cannot_change_sensitive_roles(self):
		manager = self._make_user(ACCESS_MANAGER_ROLE)
		target = self._make_user("Sales User")
		frappe.set_user(manager)

		with self.assertRaises(frappe.PermissionError):
			set_user_access(target, [OWNER_ROLE])
		with self.assertRaises(frappe.PermissionError):
			set_user_access(target, [WAGE_MANAGER_ROLE])

		set_user_access(target, [WAREHOUSE_OPERATOR_ROLE])
		self.assertIn(WAREHOUSE_OPERATOR_ROLE, {row.role for row in frappe.get_doc("User", target).roles})

	def test_worker_role_rejects_management_or_native_manufacturing_roles(self):
		for role in (
			OWNER_ROLE,
			SALES_OPERATOR_ROLE,
			WAREHOUSE_OPERATOR_ROLE,
			PRODUCTION_MANAGER_ROLE,
			SUPERVISOR_ROLE,
			WAGE_MANAGER_ROLE,
			ACCESS_MANAGER_ROLE,
		):
			with self.subTest(role=role):
				plain_user = self._make_user()
				with self.assertRaises(frappe.ValidationError):
					set_user_access(plain_user, [WORKER_ROLE, role])

		manufacturing_user = self._make_user("Manufacturing User")
		with self.assertRaises(frappe.ValidationError):
			set_user_access(manufacturing_user, [WORKER_ROLE])

	def test_existing_worker_account_with_an_app_management_role_is_blocked(self):
		from process_simplification.production_reporting.domain import assert_worker_user_isolated

		worker = self._make_user(WORKER_ROLE, SALES_OPERATOR_ROLE)
		with self.assertRaises(frappe.ValidationError):
			assert_worker_user_isolated(worker)

	def test_production_manager_is_an_unrestricted_report_reviewer(self):
		from process_simplification.production_reporting.domain import (
			is_admin_reviewer,
			require_reviewer,
		)

		manager = self._make_user(PRODUCTION_MANAGER_ROLE)
		frappe.set_user(manager)
		require_reviewer()
		self.assertTrue(is_admin_reviewer())

	def test_owner_is_an_admin_reviewer_and_wage_manager(self):
		from process_simplification.production_reporting.constants import (
			ADMIN_REVIEW_ROLES,
			WAGE_ROLES,
		)

		self.assertIn(OWNER_ROLE, ADMIN_REVIEW_ROLES)
		self.assertIn(OWNER_ROLE, WAGE_ROLES)

	def test_managed_document_permissions_are_least_privilege(self):
		def permission(doctype, role):
			return frappe.db.get_value(
				"Custom DocPerm",
				{"parent": doctype, "role": role, "permlevel": 0, "if_owner": 0},
				["read", "create", "write", "submit", "cancel", "delete", "amend"],
				as_dict=True,
			)

		sales_order = permission("Sales Order", SALES_OPERATOR_ROLE)
		self.assertTrue(sales_order.read and sales_order.create and sales_order.write and sales_order.submit)
		self.assertFalse(sales_order.cancel or sales_order.delete or sales_order.amend)

		warehouse_stock = permission("Stock Entry", WAREHOUSE_OPERATOR_ROLE)
		self.assertTrue(warehouse_stock.read and warehouse_stock.create and warehouse_stock.write and warehouse_stock.submit)
		self.assertFalse(warehouse_stock.cancel or warehouse_stock.delete or warehouse_stock.amend)

		warehouse_purchase = permission("Purchase Order", WAREHOUSE_OPERATOR_ROLE)
		self.assertTrue(warehouse_purchase.read and warehouse_purchase.create and warehouse_purchase.write)
		self.assertFalse(warehouse_purchase.submit)

		production_work = permission("Work Order", PRODUCTION_MANAGER_ROLE)
		self.assertTrue(production_work.read and production_work.create and production_work.write and production_work.submit)
		self.assertFalse(production_work.cancel or production_work.delete or production_work.amend)

		production_stock = permission("Stock Entry", PRODUCTION_MANAGER_ROLE)
		self.assertTrue(production_stock.read)
		self.assertFalse(production_stock.create or production_stock.write or production_stock.submit)

		owner_purchase = permission("Purchase Order", OWNER_ROLE)
		self.assertTrue(owner_purchase.read and owner_purchase.create and owner_purchase.write and owner_purchase.submit)
