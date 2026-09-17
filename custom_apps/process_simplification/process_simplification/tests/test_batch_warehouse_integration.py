"""Real restricted warehouse-role transactions through ERPNext's batch selector.

Run only on an isolated test site. Business permissions and native posting are
never mocked; per-test settings, users, items and stock transactions roll back.
"""

from types import SimpleNamespace

import frappe
from frappe.handler import execute_cmd
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, getdate, nowdate, nowtime

from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import add_serial_batch_ledgers
from process_simplification.api.access_management import set_user_access
from process_simplification.batch_display import batch_navigation, get_print_batch_summary
from process_simplification.management_access import WAREHOUSE_OPERATOR_ROLE, ensure_management_access
from process_simplification.printing import FACTORY_FORMATS, ensure_factory_print_formats
from process_simplification.stock_permissions import ensure_company_warehouse_reference_permissions


class TestBatchWarehouseIntegration(IntegrationTestCase):
	COMPANY = "Batch Warehouse Role Test Company"
	OTHER_COMPANY = "Batch Warehouse Foreign Test Company"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# Do not expand native test-record dependencies: Item Group recursively
		# pulls in unrelated integrations such as Payment Gateway. These are the
		# only baseline stock references this isolated fixture actually needs.
		if not frappe.db.exists("UOM", "Nos"):
			frappe.get_doc(dict(doctype="UOM", uom_name="Nos", must_be_whole_number=1)).insert()
		if not frappe.db.exists("Item Group", "All Item Groups"):
			frappe.get_doc(dict(doctype="Item Group", item_group_name="All Item Groups", is_group=1)).insert()
		if not frappe.db.exists("Item Group", "Products"):
			frappe.get_doc(dict(doctype="Item Group", item_group_name="Products", parent_item_group="All Item Groups")).insert()
		if not frappe.db.exists("Stock Entry Type", "Material Receipt"):
			frappe.get_doc(dict(doctype="Stock Entry Type", name="Material Receipt", purpose="Material Receipt")).insert()
		for company, abbreviation in [(cls.COMPANY, "BWR"), (cls.OTHER_COMPANY, "BWF")]:
			if not frappe.db.exists("Company", company):
				frappe.get_doc(dict(doctype="Company", company_name=company, abbr=abbreviation,
					default_currency="INR", country="India",
					create_chart_of_accounts_based_on="Standard Template")).insert()
		year = getdate().year
		fiscal_year = f"Batch Warehouse Roles {year}"
		if not frappe.db.exists("Fiscal Year", fiscal_year):
			frappe.get_doc(dict(doctype="Fiscal Year", year=fiscal_year,
				year_start_date=f"{year}-01-01", year_end_date=f"{year}-12-31",
				companies=[dict(company=cls.COMPANY), dict(company=cls.OTHER_COMPANY)])).insert()
		ensure_management_access()
		ensure_company_warehouse_reference_permissions()
		ensure_factory_print_formats()
		# Only reusable master fixtures and normal app permission installation are
		# committed. No operator business transaction is committed by these tests.
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.addCleanup(self._rollback_test)
		# Earlier disposable-site runs installed Batch.read=1. Explicitly exercise
		# the new fresh-install matrix without changing preserved customer grants.
		frappe.db.set_value("Custom DocPerm", {"parent": "Batch", "role": WAREHOUSE_OPERATOR_ROLE,
			"permlevel": 0, "if_owner": 0}, "read", 0, update_modified=False)
		frappe.clear_cache(doctype="Batch")
		self.prefix = "BATCH-WH-" + frappe.generate_hash(length=8)
		self.allowed = self._warehouse("Allowed")
		self.foreign = self._warehouse("Other warehouse")
		self.foreign_company_warehouse = self._warehouse("Other company", self.OTHER_COMPANY)
		for field, value in {
			"enable_serial_and_batch_no_for_item": 1,
			"auto_create_serial_and_batch_bundle_for_outward": 0,
			"default_warehouse": self.foreign,
			"sample_retention_warehouse": self.foreign,
		}.items():
			frappe.db.set_single_value("Stock Settings", field, value)
		frappe.clear_document_cache("Stock Settings", "Stock Settings")
		self.item = frappe.get_doc(dict(doctype="Item", item_code=self.prefix + "-ITEM",
			item_name="Batch warehouse permission fixture", item_group="Products", stock_uom="Nos",
			is_stock_item=1, is_purchase_item=1, is_sales_item=1, has_batch_no=1,
			create_new_batch=0, valuation_rate=10)).insert().name
		self.operator = frappe.get_doc(dict(doctype="User", email=self.prefix.lower() + "@example.com",
			first_name="Batch warehouse operator", send_welcome_email=0,
			roles=[dict(role=WAREHOUSE_OPERATOR_ROLE)])).insert().name
		set_user_access(user=self.operator, roles=[WAREHOUSE_OPERATOR_ROLE],
			companies=[self.COMPANY], warehouses=[self.allowed])
		roles = set(frappe.get_roles(self.operator))
		self.assertIn(WAREHOUSE_OPERATOR_ROLE, roles)
		self.assertFalse(roles.intersection({"System Manager", "Stock User", "Stock Manager", "Item Manager",
			"Purchase User", "Purchase Manager", "Manufacturing User", "Manufacturing Manager"}))

	def _rollback_test(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.clear_document_cache("Stock Settings", "Stock Settings")
		frappe.clear_cache(doctype="Batch")
		if getattr(self, "operator", None):
			frappe.clear_cache(user=self.operator)

	def _warehouse(self, label, company=None):
		return frappe.get_doc(dict(doctype="Warehouse", warehouse_name=self.prefix + "-" + label,
			company=company or self.COMPANY)).insert().name

	def _batch(self, label):
		return frappe.get_doc(dict(doctype="Batch", item=self.item, batch_id=self.prefix + "-" + label)).insert()

	def _entry(self, warehouse, qty=5, company=None):
		company = company or self.COMPANY
		defaults = frappe.db.get_value("Company", company, ["stock_adjustment_account", "cost_center"], as_dict=True)
		return frappe.get_doc(dict(doctype="Stock Entry", company=company,
			purpose="Material Receipt", stock_entry_type="Material Receipt",
			posting_date=nowdate(), posting_time=nowtime(), set_posting_time=1,
			items=[dict(item_code=self.item, qty=qty, uom="Nos", stock_uom="Nos",
				t_warehouse=warehouse, conversion_factor=1, transfer_qty=qty, basic_rate=10,
				cost_center=defaults.cost_center, expense_account=defaults.stock_adjustment_account,
				use_serial_batch_fields=0)]))

	def _select(self, entry, batch, warehouse=None, qty=None):
		"""Use the same whitelisted method that the native selector calls."""
		row = entry.items[0]
		bundle = add_serial_batch_ledgers(
			entries=[dict(batch_no=batch, qty=flt(qty if qty is not None else row.qty))],
			child_row=row.as_dict(), doc=entry.as_dict(), warehouse=warehouse or row.t_warehouse,
		)
		row.serial_and_batch_bundle = bundle.name
		return bundle

	def _saved_draft(self, warehouse, label):
		batch = self._batch(label)
		entry = self._entry(warehouse)
		bundle = self._select(entry, batch.name)
		entry.insert()
		return entry, frappe.get_doc("Serial and Batch Bundle", bundle.name), batch

	def _bundle_state(self, name):
		bundle = frappe.get_doc("Serial and Batch Bundle", name)
		return (bundle.company, bundle.warehouse, bundle.voucher_no,
			[(row.batch_no, flt(row.qty), row.warehouse) for row in bundle.entries])

	def _rpc(self, method, **params):
		"""Dispatch the original browser method through the installed hook registry.

		Only request transport state is replaced. The dispatcher, whitelist checks,
		method override, permission checks and native stock queries are all real.
		"""
		missing = object()
		previous = {key: getattr(frappe.local, key, missing) for key in ("form_dict", "request")}
		frappe.local.form_dict = frappe._dict(cmd=method, **params)
		frappe.local.request = SimpleNamespace(method="POST")
		try:
			return execute_cmd(method)
		finally:
			# Frappe's context-local attributes do not live in __dict__; explicitly
			# restore them so later native printing keeps its original form_dict.
			for key, value in previous.items():
				if value is missing:
					if hasattr(frappe.local, key):
						delattr(frappe.local, key)
				else:
					setattr(frappe.local, key, value)

	def _receive_batch(self, batch, warehouse, qty):
		entry = self._entry(warehouse, qty=qty)
		self._select(entry, batch)
		return entry.insert().submit()

	def test_pure_warehouse_role_creates_selects_submits_and_prints_native_batch_receipt(self):
		with self.set_user(self.operator):
			# Native Stock Entry refresh and its selector read these singleton
			# settings through ordinary client RPCs; operators cannot modify them.
			setting = "enable_serial_and_batch_no_for_item"
			self.assertEqual(self._rpc("frappe.client.get_single_value",
				doctype="Stock Settings", field=setting), 1)
			self.assertEqual(self._rpc("frappe.client.get_value", doctype="Stock Settings",
				fieldname=setting)[setting], 1)
			self.assertFalse(frappe.has_permission("Stock Settings", "write"))
			self.assertFalse(frappe.has_permission("Stock Settings", "create"))
			self.assertFalse(frappe.has_permission("Warehouse", "read", doc=self.foreign))
			batch = self._batch("OPERATOR")
			self.assertEqual(batch.owner, self.operator)
			self.assertFalse(frappe.has_permission("Batch", "read", doc=batch))
			self.assertTrue(frappe.has_permission("Batch", "select", doc=batch))
			self.assertTrue(frappe.has_permission("Batch", "create", doc=batch))
			self.assertFalse(frappe.has_permission("Batch", "write", doc=batch))
			entry = self._entry(self.allowed, qty=5)
			# The selector saves its bundle before the parent Stock Entry exists.
			bundle = self._select(entry, batch.name)
			self.assertEqual(bundle.owner, self.operator)
			self.assertTrue(frappe.has_permission("Serial and Batch Bundle", "write", doc=bundle))
			self.assertTrue(frappe.has_permission("Serial and Batch Bundle", "submit", doc=bundle))
			self.assertFalse(frappe.has_permission("Serial and Batch Bundle", "cancel", doc=bundle))
			entry.insert()
			# Reopening the selector really saves an existing draft bundle, too.
			self._select(entry, batch.name)
			entry.save().submit()
			self.assertEqual(entry.docstatus, 1)
			self.assertTrue(frappe.has_permission("Stock Entry", "print", doc=entry))
			for doctype in ("Purchase Receipt", "Delivery Note"):
				self.assertTrue(frappe.has_permission(doctype, "print"))
			bundle.reload()
			self.assertEqual(bundle.docstatus, 1)
			self.assertEqual([(row.batch_no, flt(row.qty)) for row in bundle.entries], [(batch.name, 5)])
			self.assertEqual(flt(frappe.db.get_value("Bin", {"item_code": self.item, "warehouse": self.allowed}, "actual_qty")), 5)
			summary = get_print_batch_summary(entry)[entry.items[0].name]
			self.assertEqual(summary["batches"], [{"batch_no": batch.name, "qty": 5, "stock_uom": "Nos"}])
			html = frappe.get_print("Stock Entry", entry.name,
				print_format=FACTORY_FORMATS["Stock Entry"][0], no_letterhead=1)
			self.assertIn(batch.name, html)
			self.assertEqual(batch_navigation()["reports"], [])

	def test_native_selector_cannot_relabel_a_foreign_original_bundle_into_allowed_warehouse(self):
		entry, bundle, batch = self._saved_draft(self.foreign, "FOREIGN-ORIGINAL")
		before = self._bundle_state(bundle.name)
		with self.set_user(self.operator):
			self.assertFalse(frappe.has_permission("Serial and Batch Bundle", "write", doc=bundle))
			with self.assertRaises(frappe.PermissionError):
				self._select(entry, batch.name, warehouse=self.allowed, qty=4)
		self.assertEqual(self._bundle_state(bundle.name), before)

	def test_native_selector_cannot_move_an_allowed_bundle_to_a_foreign_target_warehouse(self):
		with self.set_user(self.operator):
			entry, bundle, batch = self._saved_draft(self.allowed, "ALLOWED-ORIGINAL")
			before = self._bundle_state(bundle.name)
			self.assertTrue(frappe.has_permission("Serial and Batch Bundle", "write", doc=bundle))
			with self.assertRaises(frappe.PermissionError):
				self._select(entry, batch.name, warehouse=self.foreign, qty=4)
		self.assertEqual(self._bundle_state(bundle.name), before)

	def test_native_selector_cannot_create_a_bundle_in_a_foreign_company(self):
		batch = self._batch("OTHER-COMPANY")
		entry = self._entry(self.foreign_company_warehouse, company=self.OTHER_COMPANY)
		before = frappe.db.count("Serial and Batch Bundle", {"item_code": self.item})
		with self.set_user(self.operator):
			with self.assertRaises(frappe.PermissionError):
				self._select(entry, batch.name)
		self.assertEqual(frappe.db.count("Serial and Batch Bundle", {"item_code": self.item}), before)

	def test_native_quantity_and_auto_rpc_names_enforce_real_warehouse_scope(self):
		batch = self._batch("RPC-SHARED")
		self._receive_batch(batch.name, self.allowed, 5)
		self._receive_batch(batch.name, self.foreign, 9)
		batch_rpc = "erpnext.stock.doctype.batch.batch.get_batch_qty"
		auto_rpc = "erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle.get_auto_data"
		self.assertEqual(frappe.override_whitelisted_method(batch_rpc),
			"process_simplification.batch_permissions.get_batch_qty")
		self.assertEqual(frappe.override_whitelisted_method(auto_rpc),
			"process_simplification.batch_permissions.get_auto_data")
		with self.set_user(self.operator):
			self.assertFalse(frappe.has_permission("Batch", "read"))
			self.assertEqual(flt(self._rpc(batch_rpc, item_code=self.item,
				batch_no=batch.name, warehouse=self.allowed)), 5)
			rows = self._rpc(auto_rpc, item_code=self.item, warehouse=self.allowed, has_batch_no=1)
			self.assertEqual([(row.batch_no, row.warehouse, flt(row.qty)) for row in rows],
				[(batch.name, self.allowed, 5)])
			for warehouse in (self.foreign, self.foreign_company_warehouse, None):
				with self.subTest(method=batch_rpc, warehouse=warehouse), self.assertRaises(frappe.PermissionError):
					self._rpc(batch_rpc, item_code=self.item, batch_no=batch.name, warehouse=warehouse)
				with self.subTest(method=auto_rpc, warehouse=warehouse), self.assertRaises(frappe.PermissionError):
					self._rpc(auto_rpc, item_code=self.item, warehouse=warehouse, has_batch_no=1)

	def test_native_search_link_batch_query_exposes_only_authorized_warehouse_quantities(self):
		shared, foreign_only = self._batch("RPC-SHARED"), self._batch("RPC-FOREIGN-ONLY")
		self._receive_batch(shared.name, self.allowed, 5)
		self._receive_batch(shared.name, self.foreign, 9)
		self._receive_batch(foreign_only.name, self.foreign, 17)
		# These are actual global Batch totals, populated by native stock posting.
		# The inward search must not display either one as this operator's stock.
		self.assertEqual(flt(frappe.db.get_value("Batch", shared.name, "batch_qty")), 14)
		self.assertEqual(flt(frappe.db.get_value("Batch", foreign_only.name, "batch_qty")), 17)
		search_rpc = "frappe.desk.search.search_link"
		query = "erpnext.controllers.queries.get_batch_no"
		self.assertEqual(frappe.override_whitelisted_method(search_rpc),
			"process_simplification.batch_permissions.search_link")
		filters = dict(item_code=self.item, warehouse=self.allowed, is_inward=0)
		with self.set_user(self.operator):
			self.assertFalse(frappe.has_permission("Batch", "read"))
			self.assertTrue(frappe.has_permission("Batch", "select"))
			rows = self._rpc(search_rpc, doctype="Batch", txt=self.prefix,
				query=query, filters=frappe.as_json(filters))
			self.assertEqual([row["value"] for row in rows], [shared.name])
			self.assertEqual(flt(rows[0]["description"].split(", ", 1)[0]), 5)
			rows = self._rpc(search_rpc, doctype="Batch", txt=self.prefix, query=query,
				filters=frappe.as_json({**filters, "is_inward": 1}))
			by_name = {row["value"]: row for row in rows}
			self.assertEqual(set(by_name), {shared.name, foreign_only.name})
			self.assertEqual(flt(by_name[shared.name]["description"].split(", ", 1)[0]), 5)
			# Native autosuggest omits zero descriptions; the global batch name is
			# usable for receipts without disclosing its 17 units in another warehouse.
			self.assertEqual(by_name[foreign_only.name]["description"], "")

	def test_native_bundle_read_rpc_name_rejects_foreign_original_voucher(self):
		foreign_entry, foreign_bundle, _ = self._saved_draft(self.foreign, "RPC-FOREIGN")
		method = "erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle.get_serial_batch_ledgers"
		self.assertEqual(frappe.override_whitelisted_method(method),
			"process_simplification.batch_permissions.get_serial_batch_ledgers")
		with self.set_user(self.operator):
			entry, bundle, batch = self._saved_draft(self.allowed, "RPC-OWN")
			rows = self._rpc(method, item_code=self.item, name=bundle.name, child_row="")
			self.assertEqual([(row.batch_no, row.warehouse, flt(row.qty)) for row in rows],
				[(batch.name, self.allowed, 5)])
			for params in ({"name": foreign_bundle.name}, {"voucher_no": foreign_entry.name}, {}):
				with self.subTest(params=params), self.assertRaises(frappe.PermissionError):
					self._rpc(method, item_code=self.item, **params)

	def test_generic_bundle_read_and_child_list_hide_other_creators_unlinked_drafts(self):
		batch = self._batch("UNLINKED")
		other = self._select(self._entry(self.allowed), batch.name)
		with self.set_user(self.operator):
			own = self._select(self._entry(self.allowed), batch.name)
			self.assertEqual(self._rpc("frappe.client.get", doctype="Serial and Batch Bundle", name=own.name)["name"], own.name)
			with self.assertRaises(frappe.PermissionError):
				self._rpc("frappe.client.get", doctype="Serial and Batch Bundle", name=other.name)
			rows = frappe.get_list("Serial and Batch Bundle", filters={"item_code": self.item},
				fields=["name", "entries.qty as batch_qty"], limit=0)
			self.assertEqual([(row.name, flt(row.batch_qty)) for row in rows], [(own.name, 5)])

	def test_generic_bundle_read_and_list_keep_original_stock_entry_child_warehouse_permissions(self):
		batch = self._batch("MIXED-WAREHOUSE-PARENT")
		entry = self._entry(self.allowed)
		entry.append("items", self._entry(self.foreign, qty=2).items[0].as_dict())
		allowed_bundle = self._select(entry, batch.name)
		foreign_bundle = add_serial_batch_ledgers(entries=[dict(batch_no=batch.name, qty=2)],
			child_row=entry.items[1].as_dict(), doc=entry.as_dict(), warehouse=self.foreign)
		entry.items[1].serial_and_batch_bundle = foreign_bundle.name
		entry.insert()
		allowed_bundle.reload()
		with self.set_user(self.operator):
			self.assertFalse(frappe.has_permission("Stock Entry", "read", doc=entry))
			self.assertFalse(frappe.has_permission("Serial and Batch Bundle", "read", doc=allowed_bundle))
			with self.assertRaises(frappe.PermissionError):
				self._rpc("frappe.client.get", doctype="Serial and Batch Bundle", name=allowed_bundle.name)
			self.assertEqual(frappe.get_list("Serial and Batch Bundle",
				filters={"item_code": self.item}, fields=["name", "entries.qty as batch_qty"], limit=0), [])

	def test_generic_bundle_list_rejects_legacy_foreign_child_in_allowed_header(self):
		with self.set_user(self.operator):
			batch = self._batch("LEGACY-CHILD")
			bundle = self._select(self._entry(self.allowed), batch.name)
		# Deliberately model old inconsistent data; the native write path already
		# rejects it. This fixture alteration and every document roll back.
		frappe.db.set_value("Serial and Batch Entry", bundle.entries[0].name, "warehouse", self.foreign)
		with self.set_user(self.operator):
			with self.assertRaises(frappe.PermissionError):
				self._rpc("frappe.client.get", doctype="Serial and Batch Bundle", name=bundle.name)
			self.assertEqual(frappe.get_list("Serial and Batch Bundle",
				filters={"name": bundle.name}, fields=["name", "entries.qty as batch_qty"], limit=0), [])

	def test_bundle_list_preserves_original_customer_user_permission_in_same_warehouse(self):
		customers = []
		for label in ("ALLOWED-CUSTOMER", "PRIVATE-CUSTOMER"):
			customers.append(frappe.get_doc(dict(doctype="Customer", customer_name=self.prefix + label,
				customer_type="Company", territory="All Territories",
				customer_group=frappe.db.get_value("Customer Group", {"is_group": 0}, "name"))).insert().name)
		batch = self._batch("CUSTOMER-SCOPE")
		self._receive_batch(batch.name, self.allowed, 5)
		deliveries, bundles = [], []
		for customer in customers:
			delivery = frappe.get_doc(dict(doctype="Delivery Note", company=self.COMPANY,
				customer=customer, currency="INR", conversion_rate=1,
				posting_date=nowdate(), posting_time=nowtime(), set_posting_time=1,
				items=[dict(item_code=self.item, qty=1, uom="Nos", stock_uom="Nos", conversion_factor=1,
					warehouse=self.allowed, rate=10, use_serial_batch_fields=0)]))
			bundle = add_serial_batch_ledgers(entries=[dict(batch_no=batch.name, qty=1)],
				child_row=delivery.items[0].as_dict(), doc=delivery.as_dict(), warehouse=self.allowed)
			delivery.items[0].serial_and_batch_bundle = bundle.name
			delivery.insert()
			deliveries.append(delivery)
			bundles.append(bundle)
		frappe.get_doc(dict(doctype="User Permission", user=self.operator, allow="Customer",
			for_value=customers[0], apply_to_all_doctypes=1)).insert()
		frappe.clear_cache(user=self.operator)
		with self.set_user(self.operator):
			self.assertTrue(frappe.has_permission("Delivery Note", "read", doc=deliveries[0]))
			self.assertFalse(frappe.has_permission("Delivery Note", "read", doc=deliveries[1]))
			self.assertEqual(self._rpc("frappe.client.get", doctype="Serial and Batch Bundle", name=bundles[0].name)["name"], bundles[0].name)
			with self.assertRaises(frappe.PermissionError):
				self._rpc("frappe.client.get", doctype="Serial and Batch Bundle", name=bundles[1].name)
			rows = frappe.get_list("Serial and Batch Bundle", filters={"name": ["in", [row.name for row in bundles]]},
				fields=["name", "entries.qty as batch_qty"], limit=0)
			self.assertEqual([(row.name, flt(row.batch_qty)) for row in rows], [(bundles[0].name, -1)])
