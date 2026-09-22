"""Run only on an isolated test site: real native links, ledger and permission scope."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from erpnext.stock.doctype.warehouse.warehouse import Warehouse
from process_simplification.api.warehouse_management import rename_warehouse


class TestWarehouseManagementIntegration(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(self._rollback)
		frappe.set_user("Administrator")
		self._ensure_fixture_prerequisites()
		self.prefix = "WH-RENAME-" + frappe.generate_hash(length=8)
		self.abbr = "W" + frappe.generate_hash(length=4)
		self.company = frappe.get_doc(dict(
			doctype="Company", company_name=self.prefix, abbr=self.abbr,
			default_currency="INR", country="India", create_chart_of_accounts_based_on="Standard Template",
		)).insert().name
		self.parent = frappe.db.get_value("Warehouse", {"company": self.company, "is_group": 1}, "name")
		self.warehouse = self._warehouse("原料")
		self.sibling = self._warehouse("其他")
		self.target = f"{self.prefix}-新原料仓 - {self.abbr}"
		# Both permissions are required: Warehouse write and permission to update
		# Stock Settings' derived Property Setters during native link migration.
		self.manager = self._user("System Manager", "Item Manager")
		self.permission = frappe.get_doc(dict(
			doctype="User Permission", user=self.manager, allow="Warehouse", for_value=self.warehouse,
		)).insert()
		frappe.get_doc(dict(doctype="User Permission", user=self.manager, allow="Company", for_value=self.company)).insert()
		frappe.db.set_single_value("Stock Settings", "default_warehouse", self.warehouse)
		frappe.db.set_value("Company", self.company, {
			"default_wip_warehouse": self.warehouse, "default_fg_warehouse": self.warehouse,
		})
		frappe.clear_cache()
		self.item = frappe.get_doc(dict(
			doctype="Item", item_code=self.prefix, item_group=self.item_group, stock_uom="Nos",
			is_stock_item=1, item_defaults=[dict(company=self.company, default_warehouse=self.warehouse)],
		)).insert()

	def _ensure_fixture_prerequisites(self):
		# A test-only install has not run ERPNext's setup wizard. Keep these
		# dependencies explicit and transactional instead of patching the site
		# outside the test or relying on another test module to seed them.
		for doctype, name, values in (
			("Warehouse Type", "Transit", {}),
			("UOM", "Nos", {"uom_name": "Nos", "enabled": 1, "must_be_whole_number": 1}),
			("Stock Entry Type", "Material Receipt", {"purpose": "Material Receipt"}),
		):
			if not frappe.db.exists(doctype, name):
				frappe.get_doc({"doctype": doctype, "name": name, **values}).insert()
		self.item_group = frappe.db.get_value("Item Group", {"lft": 1}, "name")
		if not self.item_group:
			self.item_group = frappe.get_doc(dict(
				doctype="Item Group", item_group_name="All Item Groups", is_group=1,
			)).insert().name

	def _rollback(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.clear_cache()

	def _warehouse(self, label):
		return frappe.get_doc(dict(doctype="Warehouse", warehouse_name=f"{self.prefix}-{label}",
			company=self.company, parent_warehouse=self.parent)).insert().name

	def _user(self, *roles):
		return frappe.get_doc(dict(doctype="User", email=f"{frappe.generate_hash(length=12)}@example.com",
			first_name="Warehouse Rename Test", user_type="System User", send_welcome_email=0,
			roles=[dict(role=role) for role in roles])).insert().name

	def _receive(self):
		if not frappe.db.exists("Fiscal Year", {"year_start_date": ["<=", nowdate()], "year_end_date": [">=", nowdate()]}):
			year = nowdate()[:4]
			frappe.get_doc(dict(doctype="Fiscal Year", year=year,
				year_start_date=f"{year}-01-01", year_end_date=f"{year}-12-31")).insert()
		return frappe.get_doc(dict(doctype="Stock Entry", company=self.company, stock_entry_type="Material Receipt",
			items=[dict(item_code=self.item.name, qty=7, basic_rate=10, t_warehouse=self.warehouse)],
		)).insert().submit()

	def _assert_references(self, expected):
		self.assertEqual(frappe.db.get_single_value("Stock Settings", "default_warehouse"), expected)
		for field in ("default_wip_warehouse", "default_fg_warehouse"):
			self.assertEqual(frappe.db.get_value("Company", self.company, field), expected)
		self.assertEqual(frappe.db.get_value("Item Default", self.item.item_defaults[0].name, "default_warehouse"), expected)
		self.assertEqual(frappe.db.get_value("User Permission", self.permission.name, "for_value"), expected)

	def test_native_links_dynamic_permissions_and_posted_stock_survive_rename(self):
		entry = self._receive()
		before = frappe.get_all("Stock Ledger Entry", filters={"warehouse": self.warehouse},
			fields=["name", "actual_qty", "stock_value_difference"], order_by="name")
		frappe.set_user(self.manager)
		self.assertTrue(frappe.has_permission("Warehouse", "write", doc=self.warehouse))
		self.assertFalse(frappe.has_permission("Warehouse", "write", doc=self.sibling))
		with patch.object(frappe, "enqueue"):
			result = rename_warehouse(self.warehouse, self.target)
		self.assertEqual(result["name"], self.target)
		self.assertTrue(frappe.has_permission("Warehouse", "write", doc=self.target))
		self.assertFalse(frappe.has_permission("Warehouse", "write", doc=self.sibling))
		frappe.set_user("Administrator")
		self._assert_references(self.target)
		self.assertFalse(frappe.db.exists("Warehouse", self.warehouse))
		doc = frappe.get_doc("Warehouse", self.target)
		self.assertEqual(doc.warehouse_name, f"{self.prefix}-新原料仓")
		self.assertEqual((doc.company, doc.parent_warehouse), (self.company, self.parent))
		self.assertEqual(frappe.db.get_value("Stock Entry Detail", entry.items[0].name, "t_warehouse"), self.target)
		self.assertEqual(frappe.db.get_value("Bin", {"item_code": self.item.name, "warehouse": self.target}, "actual_qty"), 7)
		after = frappe.get_all("Stock Ledger Entry", filters={"warehouse": self.target},
			fields=["name", "actual_qty", "stock_value_difference"], order_by="name")
		self.assertEqual(before, after)

	def test_failure_after_native_link_migration_rolls_back_name_and_every_reference(self):
		entry = self._receive()
		save = Warehouse.save
		def fail_after_rename(doc, *args, **kwargs):
			if doc.name == self.target:
				raise RuntimeError("injected warehouse label failure")
			return save(doc, *args, **kwargs)
		with patch.object(Warehouse, "save", fail_after_rename), self.assertRaisesRegex(RuntimeError, "injected"):
			rename_warehouse(self.warehouse, self.target)
		self.assertTrue(frappe.db.exists("Warehouse", self.warehouse))
		self.assertFalse(frappe.db.exists("Warehouse", self.target))
		self._assert_references(self.warehouse)
		self.assertEqual(frappe.db.get_value("Stock Entry Detail", entry.items[0].name, "t_warehouse"), self.warehouse)
		self.assertEqual(frappe.db.get_value("Bin", {"item_code": self.item.name, "warehouse": self.warehouse}, "actual_qty"), 7)
		self.assertFalse(any(event[0] == "doc_rename" and event[1].get("new") == self.target
			for event in (getattr(frappe.local, "_realtime_log", []) or [])))

	def test_restricted_manager_and_ordinary_user_cannot_rename_other_warehouses(self):
		frappe.set_user(self.manager)
		with self.assertRaises(frappe.PermissionError):
			rename_warehouse(self.sibling, "越权")
		frappe.set_user("Administrator")
		for role in ("Stock User", "Item Manager"):
			frappe.set_user("Administrator")
			ordinary = self._user(role)
			frappe.set_user(ordinary)
			with self.subTest(role=role), self.assertRaises(frappe.PermissionError):
				rename_warehouse(self.warehouse, "越权")
		frappe.set_user("Administrator")
		self.assertTrue(frappe.db.exists("Warehouse", self.warehouse))
		self._assert_references(self.warehouse)

	def test_invalid_name_foreign_suffix_and_existing_target_leave_links_unchanged(self):
		for name in ("", " ", self.warehouse, "原料仓 - ANOTHER", self.sibling):
			with self.subTest(name=name), self.assertRaises(frappe.ValidationError):
				rename_warehouse(self.warehouse, name)
		self._assert_references(self.warehouse)
		self.assertTrue(frappe.db.exists("Warehouse", self.sibling))
