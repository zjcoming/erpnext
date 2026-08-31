from __future__ import annotations

from frappe.tests import IntegrationTestCase
from frappe.utils import random_string

import frappe

from process_simplification.api.access_management import get_user_access, set_user_access
from process_simplification.management_access import (
	ACCESS_MANAGER_ROLE,
	APP_MANAGED_ROLE_PROFILES,
	EMPLOYEE_ROLE_PROFILE,
	MANAGED_PAGE_ROLES,
	OWNER_ROLE,
	PRESERVED_PROFILE_PREFIX,
	PRODUCTION_MANAGER_ROLE,
	ROLE_DEFINITION_BY_ROLE,
	ROLE_DEFINITIONS,
	SALES_OPERATOR_ROLE,
	SUPERVISOR_ROLE,
	WAGE_MANAGER_ROLE,
	WAREHOUSE_OPERATOR_ROLE,
	WORKER_ROLE,
	ensure_management_access,
	has_owner_access,
	migrate_legacy_production_supervisor_roles,
	migrate_management_users_to_role_profiles,
	require_owner_access,
)


SCOPED_TEST_ROLES = {
	OWNER_ROLE,
	SALES_OPERATOR_ROLE,
	WAREHOUSE_OPERATOR_ROLE,
	PRODUCTION_MANAGER_ROLE,
	WAGE_MANAGER_ROLE,
}


class TestManagementAccess(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		ensure_management_access()
		warehouses = frappe.get_all(
			"Warehouse",
			filters={"disabled": 0, "is_group": 0},
			fields=["name", "company"],
			order_by="company, name",
			limit=0,
		)
		warehouse = next(
			(row for row in warehouses if row.company and frappe.db.exists("Company", row.company)),
			None,
		)
		if not warehouse:
			self.fail("Management access tests require one enabled leaf Warehouse with a Company.")
		self.company = warehouse.company
		self.warehouse = warehouse.name

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		super().tearDown()

	def _make_user(self, *roles):
		roles = roles or ("Employee",)
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

	def _make_employee(self):
		return frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": f"Access Worker {random_string(6)}",
				"company": self.company,
				"date_of_birth": "1990-05-08",
				"date_of_joining": "2020-01-01",
				"gender": "Female",
				"status": "Active",
			}
		).insert(ignore_permissions=True)

	def _set_access(self, user, roles, *, employee=None):
		roles = set(roles)
		return set_user_access(
			user=user,
			roles=sorted(roles),
			companies=[self.company] if roles.intersection(SCOPED_TEST_ROLES) else [],
			warehouses=[self.warehouse] if WAREHOUSE_OPERATOR_ROLE in roles else [],
			employee=employee,
		)

	def _top_level_permissions(self, user, allow):
		rows = frappe.get_all(
			"User Permission",
			filters={"user": user, "allow": allow},
			fields=["for_value", "applicable_for"],
			limit=0,
		)
		return {row.for_value for row in rows if row.for_value and not row.applicable_for}

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

	def test_access_update_uses_profiles_and_preserves_non_app_roles(self):
		target = self._make_user("Sales User", WAREHOUSE_OPERATOR_ROLE, SUPERVISOR_ROLE)
		self._set_access(target, [SALES_OPERATOR_ROLE, PRODUCTION_MANAGER_ROLE])

		user_doc = frappe.get_doc("User", target)
		effective_roles = {row.role for row in user_doc.roles}
		profiles = {row.role_profile for row in user_doc.role_profiles}
		self.assertIn("Sales User", effective_roles)
		self.assertIn(SALES_OPERATOR_ROLE, effective_roles)
		self.assertIn(PRODUCTION_MANAGER_ROLE, effective_roles)
		self.assertNotIn(WAREHOUSE_OPERATOR_ROLE, effective_roles)
		self.assertNotIn(SUPERVISOR_ROLE, effective_roles)
		self.assertIn(ROLE_DEFINITION_BY_ROLE[SALES_OPERATOR_ROLE]["profile"], profiles)
		self.assertIn(ROLE_DEFINITION_BY_ROLE[PRODUCTION_MANAGER_ROLE]["profile"], profiles)
		self.assertTrue(any(profile.startswith(PRESERVED_PROFILE_PREFIX) for profile in profiles))

	def test_legacy_positional_role_api_remains_compatible(self):
		target = self._make_user()
		set_user_access(target, [ACCESS_MANAGER_ROLE])
		profiles = {row.role_profile for row in frappe.get_doc("User", target).role_profiles}
		self.assertIn(ROLE_DEFINITION_BY_ROLE[ACCESS_MANAGER_ROLE]["profile"], profiles)

	def test_legacy_supervisor_is_migrated_to_production_profile(self):
		# A migrated site keeps the legacy role disabled. Re-enable it only inside
		# this rolled-back test transaction to reproduce a pre-migration account.
		frappe.db.set_value("Role", SUPERVISOR_ROLE, "disabled", 0, update_modified=False)
		target = self._make_user("Sales User", SUPERVISOR_ROLE)

		legacy_migrated = migrate_legacy_production_supervisor_roles()
		profile_migrated = migrate_management_users_to_role_profiles()
		user_doc = frappe.get_doc("User", target)
		effective_roles = {row.role for row in user_doc.roles}
		profiles = {row.role_profile for row in user_doc.role_profiles}

		self.assertIn(target, legacy_migrated)
		self.assertIn(target, profile_migrated)
		self.assertIn("Sales User", effective_roles)
		self.assertIn(PRODUCTION_MANAGER_ROLE, effective_roles)
		self.assertNotIn(SUPERVISOR_ROLE, effective_roles)
		self.assertIn(ROLE_DEFINITION_BY_ROLE[PRODUCTION_MANAGER_ROLE]["profile"], profiles)
		self.assertNotIn(SUPERVISOR_ROLE, {definition["role"] for definition in ROLE_DEFINITIONS})

	def test_access_manager_cannot_change_sensitive_roles(self):
		manager = self._make_user(ACCESS_MANAGER_ROLE)
		target = self._make_user("Sales User")
		frappe.set_user(manager)

		with self.assertRaises(frappe.PermissionError):
			set_user_access(target, [OWNER_ROLE])
		with self.assertRaises(frappe.PermissionError):
			set_user_access(target, [WAGE_MANAGER_ROLE])

		set_user_access(
			user=target,
			roles=[WAREHOUSE_OPERATOR_ROLE],
			companies=[self.company],
			warehouses=[self.warehouse],
		)
		self.assertIn(WAREHOUSE_OPERATOR_ROLE, set(frappe.get_roles(target)))

	def test_worker_role_rejects_management_or_native_manufacturing_roles(self):
		for role in (
			OWNER_ROLE,
			SALES_OPERATOR_ROLE,
			WAREHOUSE_OPERATOR_ROLE,
			PRODUCTION_MANAGER_ROLE,
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

	def test_worker_profile_binds_employee_and_company_scope(self):
		worker = self._make_user()
		employee = self._make_employee()
		result = self._set_access(worker, [WORKER_ROLE], employee=employee.name)

		user_doc = frappe.get_doc("User", worker)
		profiles = {row.role_profile for row in user_doc.role_profiles}
		effective_roles = set(frappe.get_roles(worker))
		self.assertEqual(frappe.db.get_value("Employee", employee.name, "user_id"), worker)
		self.assertIn(ROLE_DEFINITION_BY_ROLE[WORKER_ROLE]["profile"], profiles)
		self.assertIn(EMPLOYEE_ROLE_PROFILE, profiles)
		self.assertIn(WORKER_ROLE, effective_roles)
		self.assertIn("Employee", effective_roles)
		self.assertEqual(self._top_level_permissions(worker, "Company"), {self.company})
		self.assertEqual(result["employee"].name, employee.name)

		payload = get_user_access(worker)
		self.assertEqual(payload["source"], "Role Profile")
		self.assertEqual(payload["employee"].name, employee.name)

	def test_company_and_warehouse_scope_are_replaced_exactly(self):
		target = self._make_user()
		self._set_access(target, [WAREHOUSE_OPERATOR_ROLE])
		self.assertEqual(self._top_level_permissions(target, "Company"), {self.company})
		self.assertEqual(self._top_level_permissions(target, "Warehouse"), {self.warehouse})

		self._set_access(target, [SALES_OPERATOR_ROLE])
		self.assertEqual(self._top_level_permissions(target, "Company"), {self.company})
		self.assertEqual(self._top_level_permissions(target, "Warehouse"), set())

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

	def test_fixed_role_profiles_have_exactly_one_managed_role(self):
		for definition in ROLE_DEFINITIONS:
			with self.subTest(profile=definition["profile"]):
				roles = set(
					frappe.get_all(
						"Has Role",
						filters={"parenttype": "Role Profile", "parent": definition["profile"]},
						pluck="role",
						limit=0,
					)
				)
				self.assertEqual(roles, {definition["role"]})
		self.assertEqual(
			set(
				frappe.get_all(
					"Has Role",
					filters={"parenttype": "Role Profile", "parent": EMPLOYEE_ROLE_PROFILE},
					pluck="role",
					limit=0,
				)
			),
			{"Employee"},
		)
		self.assertEqual(len(APP_MANAGED_ROLE_PROFILES), len(ROLE_DEFINITIONS))

	def test_page_roles_exactly_match_capability_mapping(self):
		for page_name, expected_roles in MANAGED_PAGE_ROLES.items():
			with self.subTest(page=page_name):
				roles = set(
					frappe.get_all(
						"Has Role",
						filters={"parenttype": "Page", "parent": page_name},
						pluck="role",
						limit=0,
					)
				)
				self.assertEqual(roles, expected_roles)
				self.assertNotIn(SUPERVISOR_ROLE, roles)

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
