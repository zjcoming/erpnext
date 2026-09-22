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
	MANAGED_REPORT_ROLE_ADDITIONS,
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

	def test_warehouse_report_company_selection_keeps_transaction_warehouse_scope(self):
		from frappe.desk.query_report import validate_filters_permissions
		from process_simplification.stock_permissions import ensure_company_warehouse_reference_permissions

		other_warehouse = frappe.get_doc({
			"doctype": "Warehouse", "warehouse_name": "Warehouse Scope " + random_string(8),
			"company": self.company, "is_group": 0,
			"parent_warehouse": frappe.db.get_value("Warehouse", {"company": self.company, "is_group": 1}, "name"),
		}).insert(ignore_permissions=True).name
		frappe.db.set_value("Company", self.company, "default_fg_warehouse", other_warehouse)
		for field in ("default_warehouse", "sample_retention_warehouse"):
			frappe.db.set_single_value("Stock Settings", field, other_warehouse)
		frappe.clear_document_cache("Stock Settings", "Stock Settings")
		self.addCleanup(frappe.clear_document_cache, "Stock Settings", "Stock Settings")
		ensure_company_warehouse_reference_permissions()
		user = self._make_user(WAREHOUSE_OPERATOR_ROLE)
		self._set_access(user, [WAREHOUSE_OPERATOR_ROLE])
		frappe.set_user(user)
		self.assertTrue(frappe.has_permission("Company", "read", doc=self.company))
		self.assertTrue(frappe.has_permission("Stock Settings", "read"))
		self.assertFalse(frappe.has_permission("Stock Settings", "write"))
		self.assertFalse(frappe.has_permission("Warehouse", "read", doc=other_warehouse))
		self.assertFalse(frappe.has_permission("Purchase Order", "submit"))
		validate_filters_permissions(
			"Stock Ledger", {"company": self.company}, user,
			[{"fieldname": "company", "fieldtype": "Link", "options": "Company"}],
		)

	def test_custom_company_warehouses_allow_settings_checks_without_granting_stock_access(self):
		from frappe.custom.doctype.property_setter.property_setter import make_property_setter
		from process_simplification.initialization import get_status
		from process_simplification.stock_permissions import ensure_company_warehouse_reference_permissions

		fields = (
			"custom_default_semi_finished_warehouse",
			"custom_material_quarantine_warehouse",
			"custom_material_rework_warehouse",
		)
		blocked_warehouses = []
		for field in fields:
			warehouse = frappe.get_doc({
				"doctype": "Warehouse", "warehouse_name": "Settings Scope " + random_string(8),
				"company": self.company, "is_group": 0,
			}).insert(ignore_permissions=True).name
			blocked_warehouses.append(warehouse)
			frappe.db.set_value("Company", self.company, field, warehouse)
			# Reproduce an existing site whose custom fields still use the old default.
			make_property_setter("Company", field, "ignore_user_permissions", 0, "Check")
		frappe.clear_document_cache("Company", self.company)
		self.addCleanup(frappe.clear_document_cache, "Company", self.company)
		self.addCleanup(frappe.clear_cache, doctype="Company")
		user = self._make_user(OWNER_ROLE, WAREHOUSE_OPERATOR_ROLE)
		self._set_access(user, [OWNER_ROLE, WAREHOUSE_OPERATOR_ROLE])
		frappe.set_user(user)
		self.assertTrue(frappe.has_permission("Process Simplification Settings", "read"))
		with self.assertRaises(frappe.PermissionError):
			get_status(company=self.company)

		frappe.set_user("Administrator")
		ensure_company_warehouse_reference_permissions()
		frappe.set_user(user)
		self.assertEqual(get_status(company=self.company)["company"], self.company)
		self.assertEqual(frappe.get_list("Company", pluck="name"), [self.company])
		self.assertEqual(self._top_level_permissions(user, "Warehouse"), {self.warehouse})
		for warehouse in (self.warehouse, *blocked_warehouses):
			with self.subTest(warehouse=warehouse):
				allowed = warehouse == self.warehouse
				self.assertEqual(bool(frappe.has_permission("Warehouse", "read", doc=warehouse)), allowed)
				entry = frappe.get_doc({
					"doctype": "Stock Entry", "company": self.company,
					"items": [{"t_warehouse": warehouse}],
				})
				self.assertEqual(bool(frappe.has_permission("Stock Entry", "read", doc=entry)), allowed)

	def test_worker_rejects_foreign_company_before_binding(self):
		worker = self._make_user()
		employee = self._make_employee()
		profiles_before = [row.role_profile for row in frappe.get_doc("User", worker).role_profiles]
		for companies in (["Foreign worker company"], [self.company, "Foreign worker company"]):
			with self.subTest(companies=companies):
				with self.assertRaisesRegex(frappe.ValidationError, "工人账号只能选择关联员工所属公司"):
					set_user_access(worker, roles=[WORKER_ROLE], companies=companies, employee=employee.name)
				self.assertFalse(frappe.db.get_value("Employee", employee.name, "user_id"))
				self.assertEqual(self._top_level_permissions(worker, "Company"), set())
				self.assertEqual(
					[row.role_profile for row in frappe.get_doc("User", worker).role_profiles], profiles_before
				)
		result = self._set_access(worker, [WORKER_ROLE], employee=employee.name)
		self.assertEqual(result["companies"], [self.company])
		with self.assertRaisesRegex(frappe.ValidationError, "工人账号只能选择关联员工所属公司"):
			set_user_access(worker, roles=[WORKER_ROLE], companies=["Foreign worker company"])
		self.assertEqual(frappe.db.get_value("Employee", employee.name, "user_id"), worker)
		self.assertEqual(self._top_level_permissions(worker, "Company"), {self.company})

	def test_setup_validation_rejects_unreadable_company_defaults(self):
		from unittest.mock import patch

		from process_simplification.api.setup import validate_setup

		other_company = frappe.db.get_value("Company", {"name": ["!=", self.company]}, "name")
		if not other_company:
			other_company = frappe.get_doc({
				"doctype": "Company", "company_name": f"Access Other {random_string(8)}",
				"abbr": random_string(5), "default_currency": "USD", "country": "United States",
				"chart_of_accounts": "Standard",
			}).insert().name
		warehouses = frappe.get_all("Warehouse", filters={"company": self.company,
			"disabled": 0, "is_group": 0}, pluck="name")
		for role in sorted(SCOPED_TEST_ROLES):
			with self.subTest(role=role):
				frappe.set_user("Administrator")
				target = self._make_user()
				set_user_access(target, roles=[role], companies=[self.company],
					warehouses=warehouses if role == WAREHOUSE_OPERATOR_ROLE else [])
				frappe.set_user(target)
				if role == WAGE_MANAGER_ROLE:
					# This role can select its wage company, but cannot read Company settings.
					with self.assertRaises(frappe.PermissionError):
						validate_setup(company=self.company)
				else:
					self.assertEqual(validate_setup(company=self.company)["defaults"].company, self.company)
				with self.assertRaises(frappe.PermissionError):
					validate_setup(company=other_company)
				with patch("process_simplification.api.setup.get_default_company", return_value=other_company):
					with self.assertRaises(frappe.PermissionError):
						validate_setup()

	def test_stock_entry_list_checks_every_child_warehouse(self):
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		other = frappe.get_doc({
			"doctype": "Warehouse", "warehouse_name": f"Access Scope {random_string(8)}",
			"company": self.company,
		}).insert(ignore_permissions=True)
		item = frappe.get_doc({
			"doctype": "Item", "item_code": f"Access Scope {random_string(8)}",
			"item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 1, "valuation_rate": 5,
		}).insert(ignore_permissions=True)
		def receipt(warehouse):
			return make_stock_entry(item_code=item.name, to_warehouse=warehouse, company=self.company,
				qty=1, basic_rate=5, do_not_submit=True)
		own = receipt(self.warehouse)
		foreign = receipt(other.name)
		mixed = frappe.copy_doc(own)
		mixed.append("items", dict(own.items[0].as_dict(), name=None, t_warehouse=other.name))
		mixed.insert(ignore_permissions=True)
		transfer = make_stock_entry(item_code=item.name, from_warehouse=self.warehouse,
			to_warehouse=other.name, company=self.company, qty=1, basic_rate=5, do_not_submit=True)
		target = self._make_user()
		self._set_access(target, [WAREHOUSE_OPERATOR_ROLE])
		frappe.set_user(target)
		filters = {"name": ("in", [own.name, foreign.name, mixed.name, transfer.name])}
		self.assertEqual(frappe.get_list("Stock Entry", filters=filters, pluck="name"), [own.name])
		self.assertEqual(frappe.get_list("Stock Entry", filters=filters, fields=[{"COUNT": "name", "as": "total"}])[0].total, 1)
		self.assertTrue(frappe.get_doc("Stock Entry", own.name).has_permission("read"))
		for denied in (foreign, mixed, transfer):
			self.assertFalse(frappe.get_doc("Stock Entry", denied.name).has_permission("read"))
		# A scope applying only to another DocType must not narrow Stock Entry.
		frappe.set_user("Administrator")
		frappe.db.set_value("User Permission", {"user": target, "allow": "Warehouse"},
			{"applicable_for": "Delivery Note", "apply_to_all_doctypes": 0})
		frappe.clear_cache(user=target)
		frappe.set_user(target)
		self.assertEqual(set(frappe.get_list("Stock Entry", filters=filters, pluck="name")),
			{own.name, foreign.name, mixed.name, transfer.name})

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

	def test_report_roles_keep_native_access_and_add_required_app_roles(self):
		for report_name, app_roles in MANAGED_REPORT_ROLE_ADDITIONS.items():
			with self.subTest(report=report_name):
				report = frappe.get_doc("Report", report_name)
				native_roles = {row.role for row in report.roles}
				custom_role = frappe.get_doc("Custom Role", {"report": report_name})
				self.assertEqual(
					{row.role for row in custom_role.roles},
					native_roles.union(app_roles),
				)
				self.assertEqual(custom_role.ref_doctype, report.ref_doctype)

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
				["read", "select", "create", "write", "submit", "cancel", "delete", "amend", "report"],
				as_dict=True,
			)

		sales_order = permission("Sales Order", SALES_OPERATOR_ROLE)
		self.assertTrue(sales_order.read and sales_order.create and sales_order.write and sales_order.submit)
		self.assertFalse(sales_order.cancel or sales_order.delete or sales_order.amend)

		sales_account = permission("Account", SALES_OPERATOR_ROLE)
		self.assertTrue(sales_account.read and sales_account.select)
		self.assertFalse(
			sales_account.create
			or sales_account.write
			or sales_account.submit
			or sales_account.cancel
			or sales_account.delete
			or sales_account.amend
			or sales_account.report
		)

		sales_reservation = permission("Stock Reservation Entry", SALES_OPERATOR_ROLE)
		self.assertTrue(
			sales_reservation.read
			and sales_reservation.select
			and sales_reservation.create
			and sales_reservation.write
			and sales_reservation.submit
		)
		self.assertFalse(
			sales_reservation.cancel
			or sales_reservation.delete
			or sales_reservation.amend
			or sales_reservation.report
		)

		sales_delivery = permission("Delivery Note", SALES_OPERATOR_ROLE)
		self.assertTrue(
			sales_delivery.read
			and sales_delivery.select
			and sales_delivery.create
			and sales_delivery.write
		)
		self.assertFalse(
			sales_delivery.submit
			or sales_delivery.cancel
			or sales_delivery.delete
			or sales_delivery.amend
			or sales_delivery.report
		)

		warehouse_stock = permission("Stock Entry", WAREHOUSE_OPERATOR_ROLE)
		self.assertTrue(warehouse_stock.read and warehouse_stock.create and warehouse_stock.write and warehouse_stock.submit)
		self.assertFalse(warehouse_stock.cancel or warehouse_stock.delete or warehouse_stock.amend)

		warehouse_bundle = permission("Serial and Batch Bundle", WAREHOUSE_OPERATOR_ROLE)
		self.assertTrue(warehouse_bundle.read and warehouse_bundle.select and warehouse_bundle.create
			and warehouse_bundle.write and warehouse_bundle.submit)
		self.assertFalse(warehouse_bundle.cancel or warehouse_bundle.delete or warehouse_bundle.amend
			or warehouse_bundle.report)

		warehouse_stock_entry_type = permission("Stock Entry Type", WAREHOUSE_OPERATOR_ROLE)
		self.assertTrue(warehouse_stock_entry_type.read and warehouse_stock_entry_type.select)
		self.assertFalse(
			warehouse_stock_entry_type.create
			or warehouse_stock_entry_type.write
			or warehouse_stock_entry_type.submit
			or warehouse_stock_entry_type.cancel
			or warehouse_stock_entry_type.delete
			or warehouse_stock_entry_type.amend
			or warehouse_stock_entry_type.report
		)

		warehouse_stock_ledger = permission("Stock Ledger Entry", WAREHOUSE_OPERATOR_ROLE)
		self.assertTrue(
			warehouse_stock_ledger.read
			and warehouse_stock_ledger.select
			and warehouse_stock_ledger.report
		)
		self.assertFalse(
			warehouse_stock_ledger.create
			or warehouse_stock_ledger.write
			or warehouse_stock_ledger.submit
			or warehouse_stock_ledger.cancel
			or warehouse_stock_ledger.delete
			or warehouse_stock_ledger.amend
		)

		warehouse_stock_settings = permission("Stock Settings", WAREHOUSE_OPERATOR_ROLE)
		self.assertTrue(warehouse_stock_settings.read)
		self.assertFalse(
			warehouse_stock_settings.create
			or warehouse_stock_settings.write
			or warehouse_stock_settings.submit
			or warehouse_stock_settings.cancel
			or warehouse_stock_settings.delete
			or warehouse_stock_settings.amend
		)

		warehouse_prepared_report = permission("Prepared Report", WAREHOUSE_OPERATOR_ROLE)
		self.assertTrue(warehouse_prepared_report.read and warehouse_prepared_report.select)
		self.assertFalse(
			warehouse_prepared_report.create
			or warehouse_prepared_report.write
			or warehouse_prepared_report.submit
			or warehouse_prepared_report.cancel
			or warehouse_prepared_report.delete
			or warehouse_prepared_report.amend
			or warehouse_prepared_report.report
		)

		for reference_doctype in ("Account", "Price List", "Supplier Group"):
			warehouse_reference = permission(reference_doctype, WAREHOUSE_OPERATOR_ROLE)
			self.assertTrue(warehouse_reference.read and warehouse_reference.select)
			self.assertFalse(
				warehouse_reference.create
				or warehouse_reference.write
				or warehouse_reference.submit
				or warehouse_reference.cancel
				or warehouse_reference.delete
				or warehouse_reference.amend
			)

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

	def test_warehouse_operator_can_open_stock_balance_and_read_material_transfer_type(self):
		from frappe.desk.query_report import get_report_doc

		warehouse_user = self._make_user()
		self._set_access(warehouse_user, [WAREHOUSE_OPERATOR_ROLE])
		frappe.set_user(warehouse_user)

		self.assertEqual(get_report_doc("Stock Balance").ref_doctype, "Stock Ledger Entry")
		self.assertTrue(frappe.has_permission("Stock Ledger Entry", "report"))
		self.assertTrue(frappe.has_permission("Stock Entry Type", "read", "Material Transfer"))
		self.assertEqual(
			frappe.get_doc("Stock Entry Type", "Material Transfer").purpose,
			"Material Transfer",
		)

	def test_warehouse_receipt_can_read_buying_policy_but_cannot_change_settings(self):
		from frappe.client import get_single_value

		warehouse_user = self._make_user()
		self._set_access(warehouse_user, [WAREHOUSE_OPERATOR_ROLE])
		expected = frappe.db.get_single_value("Buying Settings", "maintain_same_rate")
		frappe.set_user(warehouse_user)
		self.assertEqual(get_single_value("Buying Settings", "maintain_same_rate"), expected)
		self.assertFalse(frappe.has_permission("Buying Settings", "write"))
		self.assertFalse(frappe.has_permission("Buying Settings", "create"))

	def test_transaction_operators_can_read_posting_date_policy_but_cannot_change_it(self):
		from frappe.client import get_single_value

		expected = frappe.db.get_single_value("Accounts Settings", "confirm_before_resetting_posting_date")
		for role in (WAREHOUSE_OPERATOR_ROLE, SALES_OPERATOR_ROLE):
			with self.subTest(role=role):
				frappe.set_user("Administrator")
				user = self._make_user()
				self._set_access(user, [role])
				frappe.set_user(user)
				self.assertEqual(get_single_value("Accounts Settings", "confirm_before_resetting_posting_date"), expected)
				for ptype in ("write", "create", "delete", "share", "export"):
					self.assertFalse(frappe.has_permission("Accounts Settings", ptype))
				settings = frappe.get_doc("Accounts Settings")
				settings.confirm_before_resetting_posting_date = not expected
				with self.assertRaises(frappe.PermissionError):
					settings.save()

	def test_warehouse_operator_can_read_only_prepared_reports_it_may_run(self):
		warehouse_user = self._make_user()
		self._set_access(warehouse_user, [WAREHOUSE_OPERATOR_ROLE])
		frappe.set_user(warehouse_user)

		stock_balance = frappe.get_doc(
			{
				"doctype": "Prepared Report",
				"report_name": "Stock Balance",
				"status": "Completed",
				"owner": warehouse_user,
			}
		)
		inaccessible_report = frappe.get_doc(
			{
				"doctype": "Prepared Report",
				"report_name": "General Ledger",
				"status": "Completed",
				"owner": warehouse_user,
			}
		)

		self.assertTrue(
			frappe.has_permission("Prepared Report", "read", doc=stock_balance, user=warehouse_user)
		)
		self.assertFalse(
			frappe.has_permission(
				"Prepared Report",
				"read",
				doc=inaccessible_report,
				user=warehouse_user,
			)
		)
