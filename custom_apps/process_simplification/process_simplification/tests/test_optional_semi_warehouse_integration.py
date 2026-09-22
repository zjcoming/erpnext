"""Optional semi-finished storage: real plans, reservations and stock ledgers.

Run only on a disposable test site. Every prerequisite is created through native
documents in the test transaction; no dependency on a customer's test accounts,
company, pre-existing warehouses, or committed business fixtures is required.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from process_simplification.api import actions
from process_simplification.production_exceptions.material_routes import load_routes, stock_rows
from process_simplification.production_workflow import service
from process_simplification.production_workflow.stock_reservation import locked_available_qty


class TestOptionalSemiWarehouseIntegration(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.addCleanup(self._rollback)
		self.prefix = "OPTIONAL-SEMI-" + frappe.generate_hash(length=8)
		self._prerequisites()
		self.company = frappe.get_doc(dict(
			doctype="Company", company_name=self.prefix, abbr="S" + frappe.generate_hash(length=4),
			default_currency="INR", country="India", create_chart_of_accounts_based_on="Standard Template",
		)).insert().name
		self.stores = self._warehouse("Raw")
		self.semi = self._warehouse("Semi")
		self.other = self._warehouse("New-Semi")
		self.wip = self._warehouse("WIP")
		self.finished = self._warehouse("Finished")
		frappe.db.set_value("Company", self.company, dict(
			default_wip_warehouse=self.wip, default_fg_warehouse=self.finished,
			custom_default_semi_finished_warehouse=self.semi,
		))
		frappe.clear_document_cache("Company", self.company)
		for field, value in dict(enable_stock_reservation=1, auto_reserve_stock=0,
			allow_partial_reservation=1, allow_negative_stock=0, default_warehouse=self.stores).items():
			frappe.db.set_single_value("Stock Settings", field, value)
		frappe.clear_document_cache("Stock Settings", "Stock Settings")
		self.raw = self._item("Raw")
		self.direct_raw = self._item("Direct-Raw")
		self.lower = self._item("Lower-Semi")
		self.upper = self._item("Upper-Semi")
		self.fg = self._item("FG")
		self.lower_bom = self._bom(self.lower, [(self.raw, 1, None)])
		self.upper_bom = self._bom(self.upper, [(self.lower, 1, self.lower_bom), (self.direct_raw, 2, None)])
		self.fg_bom = self._bom(self.fg, [(self.upper, 1, self.upper_bom), (self.direct_raw, 1, None)])
		self._stock(self.raw, 200, self.stores)
		self._stock(self.direct_raw, 300, self.stores)

	def _prerequisites(self):
		for doctype, name, values in (
			("Warehouse Type", "Transit", {}),
			("UOM", "Nos", dict(uom_name="Nos", enabled=1, must_be_whole_number=1)),
			*(("Stock Entry Type", purpose, dict(purpose=purpose, is_standard=1)) for purpose in (
				"Material Receipt", "Material Issue", "Material Transfer for Manufacture", "Manufacture")),
		):
			if not frappe.db.exists(doctype, name):
				frappe.get_doc(dict(doctype=doctype, name=name, **values)).insert()
		for doctype, field, name in (
			("Item Group", "item_group_name", "All Item Groups"),
			("Customer Group", "customer_group_name", "All Customer Groups"),
			("Territory", "territory_name", "All Territories"),
		):
			if not frappe.db.exists(doctype, name):
				frappe.get_doc(dict(doctype=doctype, **{field: name}, is_group=1)).insert()
		self.customer_group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		if not self.customer_group:
			self.customer_group = frappe.get_doc(dict(doctype="Customer Group",
				customer_group_name=self.prefix, parent_customer_group="All Customer Groups")).insert().name
		if not frappe.db.exists("Fiscal Year", {"year_start_date": ["<=", nowdate()], "year_end_date": [">=", nowdate()]}):
			year = nowdate()[:4]
			frappe.get_doc(dict(doctype="Fiscal Year", year=year,
				year_start_date=f"{year}-01-01", year_end_date=f"{year}-12-31")).insert()

	def _rollback(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.clear_cache()

	def _warehouse(self, label):
		return frappe.get_doc(dict(doctype="Warehouse", warehouse_name=self.prefix + "-" + label,
			company=self.company)).insert().name

	def _item(self, label):
		return frappe.get_doc(dict(doctype="Item", item_code=self.prefix + "-" + label,
			item_group="All Item Groups", stock_uom="Nos", is_stock_item=1, valuation_rate=5,
			item_defaults=[dict(company=self.company, default_warehouse=self.stores)])).insert().name

	def _bom(self, item, components):
		return frappe.get_doc(dict(doctype="BOM", item=item, company=self.company, quantity=1,
			is_active=1, is_default=1, currency="INR", default_source_warehouse=self.stores,
			items=[dict(item_code=code, qty=qty, rate=5, bom_no=bom) for code, qty, bom in components],
		)).insert().submit().name

	def _stock(self, item, qty, warehouse):
		return frappe.get_doc(dict(doctype="Stock Entry", company=self.company, stock_entry_type="Material Receipt",
			items=[dict(item_code=item, qty=qty, basic_rate=5, t_warehouse=warehouse)])).insert().submit()

	def _actual(self, item, warehouse):
		return flt(frappe.db.get_value("Bin", dict(item_code=item, warehouse=warehouse), "actual_qty"))

	def _configure(self, warehouse):
		frappe.db.set_value("Company", self.company, "custom_default_semi_finished_warehouse", warehouse)
		frappe.clear_document_cache("Company", self.company)

	def _sales_order(self, qty):
		price_list = frappe.get_doc(dict(doctype="Price List", price_list_name=self.prefix + frappe.generate_hash(length=4),
			enabled=1, selling=1, currency="INR")).insert().name
		customer = frappe.get_doc(dict(doctype="Customer", customer_name=self.prefix + frappe.generate_hash(length=4),
			customer_type="Company", customer_group=self.customer_group, territory="All Territories")).insert().name
		return frappe.get_doc(dict(doctype="Sales Order", company=self.company, customer=customer,
			delivery_date=add_days(nowdate(), 10), currency="INR", selling_price_list=price_list,
			items=[dict(item_code=self.fg, qty=qty, rate=100, warehouse=self.finished,
				bom_no=self.fg_bom, delivery_date=add_days(nowdate(), 10))])).insert().submit()

	def _plan(self, qty=60):
		order = self._sales_order(qty)
		result = actions.create_work_order(order.name, order.items[0].name)
		orders = {doc.production_item: doc for doc in (
			frappe.get_doc("Work Order", name) for name in result["work_orders"])}
		return result, orders

	def _reserved(self, work_order, item=None):
		return sum(service._active_work_order_item_reserved_qty(row) for row in work_order.required_items
			if item is None or row.item_code == item)

	def _issue(self, work_order):
		entry = frappe.get_doc("Stock Entry", service.request_material_issue(work_order.name)["stock_entry"])
		entry.submit()
		work_order.reload()
		return entry

	def _receipt(self, work_order):
		entry = frappe.get_doc("Stock Entry", service.request_manufacture(work_order.name)["stock_entry"])
		entry.submit()
		work_order.reload()
		return entry

	def _sources(self, work_order):
		return {row.item_code: row.source_warehouse for row in work_order.required_items}

	def test_semi_100_demand_60_only_root_is_planned_and_stock_40_remains(self):
		self._stock(self.upper, 100, self.semi)
		self._stock(self.upper, 17, self.stores)  # Same item in the wrong pool must stay untouched.
		_, orders = self._plan()
		self.assertEqual(set(orders), {self.fg})
		root = orders[self.fg]
		self.assertEqual(root.custom_semi_finished_warehouse, self.semi)
		self.assertEqual(self._sources(root), {self.upper: self.semi, self.direct_raw: self.stores})
		self.assertEqual(self._reserved(root, self.upper), 60)
		self.assertEqual(locked_available_qty(self.upper, self.semi), 40)
		issue = self._issue(root)
		self.assertEqual({row.item_code: row.s_warehouse for row in issue.items},
			{self.upper: self.semi, self.direct_raw: self.stores})
		self.assertEqual(self._actual(self.upper, self.semi), 40)
		self.assertEqual(self._actual(self.upper, self.stores), 17)
		self._receipt(root)
		self.assertEqual(self._actual(self.fg, self.finished), 60)
		self.assertEqual(self._actual(self.upper, self.wip), 0)

	def test_existing_40_only_gap_20_produced_through_both_levels_and_reserved_to_parent(self):
		self._stock(self.upper, 40, self.semi)
		_, orders = self._plan()
		self.assertEqual({item: order.qty for item, order in orders.items()},
			{self.fg: 60, self.upper: 20, self.lower: 20})
		root = orders[self.fg]
		self.assertEqual(self._reserved(root, self.upper), 40)
		self.assertEqual(self._sources(orders[self.lower]), {self.raw: self.stores})
		self.assertEqual(self._sources(orders[self.upper]), {self.lower: self.semi, self.direct_raw: self.stores})
		for item in (self.lower, self.upper):
			child = orders[item]
			self.assertEqual(child.fg_warehouse, self.semi)
			self.assertEqual(child.custom_semi_finished_warehouse, self.semi)
			self._issue(child)
			receipt = self._receipt(child)
			self.assertEqual({row.t_warehouse for row in receipt.items if row.is_finished_item}, {self.semi})
		self.assertEqual(self._reserved(root, self.upper), 60)
		self._issue(root)
		self._receipt(root)
		self.assertEqual(self._actual(self.fg, self.finished), 60)
		self.assertEqual(self._actual(self.upper, self.semi), 0)
		self.assertEqual(self._actual(self.lower, self.semi), 0)
		self.assertEqual(self._actual(self.raw, self.stores), 180)
		self.assertEqual(self._actual(self.direct_raw, self.stores), 200)

	def test_blank_configuration_preserves_shared_warehouse_and_freezes_that_choice(self):
		self._configure(None)
		self._stock(self.upper, 40, self.stores)
		_, orders = self._plan()
		self.assertEqual({orders[self.upper].qty, orders[self.lower].qty}, {20})
		for order in orders.values():
			self.assertEqual(order.custom_semi_finished_warehouse, self.stores)
			self.assertEqual(set(self._sources(order).values()), {self.stores})
		self.assertEqual(orders[self.upper].fg_warehouse, self.stores)
		self._configure(self.semi)
		for item in (self.lower, self.upper, self.fg):
			self._issue(orders[item])
			self._receipt(orders[item])
		self.assertEqual(self._actual(self.fg, self.finished), 60)
		self.assertEqual(self._actual(self.upper, self.stores), 0)
		self.assertEqual(self._actual(self.upper, self.semi), 0)

	def test_configuration_change_affects_new_plan_but_not_existing_issue_or_receipt(self):
		self._stock(self.upper, 60, self.semi)
		_, old_orders = self._plan()
		self._configure(self.other)
		self._stock(self.upper, 60, self.other)
		_, new_orders = self._plan()
		for orders, expected in ((old_orders, self.semi), (new_orders, self.other)):
			root = orders[self.fg]
			root.reload()
			self.assertEqual(root.custom_semi_finished_warehouse, expected)
			self.assertEqual(self._sources(root)[self.upper], expected)
			issue = self._issue(root)
			self.assertEqual(next(row.s_warehouse for row in issue.items if row.item_code == self.upper), expected)
			self._receipt(root)
		self.assertEqual(self._actual(self.fg, self.finished), 120)

	def test_replenishment_of_old_plan_uses_original_semi_pool_after_configuration_change(self):
		self._stock(self.upper, 60, self.semi)
		_, orders = self._plan()
		root = orders[self.fg]
		for name in frappe.get_all("Stock Reservation Entry", filters=dict(docstatus=1,
			voucher_type="Work Order", voucher_no=root.name), pluck="name"):
			frappe.get_doc("Stock Reservation Entry", name).cancel()
		frappe.get_doc(dict(doctype="Stock Entry", company=self.company, stock_entry_type="Material Issue",
			items=[dict(item_code=self.upper, qty=60, s_warehouse=self.semi)])).insert().submit()
		self._configure(self.other)
		row = next(row for row in root.required_items if row.item_code == self.upper)
		created = service.create_replenishment_work_order(root.name, row.name)
		supplies = {doc.production_item: doc for doc in (
			frappe.get_doc("Work Order", name) for name in created["work_orders"])}
		self.assertEqual(set(supplies), {self.lower, self.upper})
		for item in (self.lower, self.upper):
			supply = supplies[item]
			self.assertEqual(supply.qty, 60)
			self.assertEqual(supply.fg_warehouse, self.semi)
			self.assertEqual(supply.custom_semi_finished_warehouse, self.semi)
			self._issue(supply)
			self._receipt(supply)
		self.assertEqual(self._reserved(root, self.upper), 60)
		self._issue(root)
		self._receipt(root)
		self.assertEqual(self._actual(self.fg, self.finished), 60)
		self.assertEqual(self._actual(self.upper, self.other), 0)

	def test_unused_good_material_returns_to_original_semi_pool_and_can_be_reissued(self):
		self._stock(self.upper, 60, self.semi)
		_, orders = self._plan()
		root = orders[self.fg]
		self._issue(root)
		self._configure(self.other)
		route = next(row for row in load_routes(root.name) if row.item_code == self.upper)
		self.assertEqual((route.source_warehouse, route.return_warehouse), (self.wip, self.semi))
		self.assertEqual(route.native_available_qty, 60)
		returned = frappe.get_doc(dict(doctype="Stock Entry", company=self.company,
			stock_entry_type="Material Transfer for Manufacture", work_order=root.name,
			is_return=1, custom_return_source_warehouse=route.return_warehouse,
			items=stock_rows(route, 10, route.return_warehouse))).insert().submit()
		self.assertEqual(self._actual(self.upper, self.semi), 10)
		self.assertEqual(self._actual(self.upper, self.other), 0)
		reissue = self._issue(root)
		self.assertEqual([(row.item_code, row.qty, row.s_warehouse) for row in reissue.items],
			[(self.upper, 10, self.semi)])
		self.assertEqual(reissue.fg_completed_qty, 0)
		self._receipt(root)
		self.assertEqual(self._actual(self.fg, self.finished), 60)
		self.assertEqual(self._actual(self.upper, self.semi), 0)
		self.assertEqual(returned.docstatus, 1)

	def test_covered_semi_stock_is_not_reused_by_a_second_order(self):
		self._stock(self.upper, 100, self.semi)
		_, first = self._plan()
		_, second = self._plan()
		self.assertEqual(set(first), {self.fg})
		self.assertEqual(self._reserved(first[self.fg], self.upper), 60)
		self.assertEqual(self._reserved(second[self.fg], self.upper), 40)
		self.assertEqual(second[self.upper].qty, 20)
		self.assertEqual(second[self.lower].qty, 20)
		self.assertEqual(locked_available_qty(self.upper, self.semi), 0)

	def test_company_rejects_foreign_group_disabled_and_wip_warehouse(self):
		foreign_company = frappe.get_doc(dict(doctype="Company", company_name=self.prefix + "-Foreign",
			abbr="F" + frappe.generate_hash(length=4), default_currency="INR", country="India",
			create_chart_of_accounts_based_on="Standard Template")).insert().name
		foreign = frappe.db.get_value("Warehouse", dict(company=foreign_company, is_group=0), "name")
		group = frappe.db.get_value("Warehouse", dict(company=self.company, is_group=1), "name")
		disabled = self._warehouse("Disabled")
		frappe.db.set_value("Warehouse", disabled, "disabled", 1)
		for warehouse in (foreign, group, disabled, self.wip):
			with self.subTest(warehouse=warehouse):
				doc = frappe.get_doc("Company", self.company)
				doc.custom_default_semi_finished_warehouse = warehouse
				with self.assertRaises(frappe.ValidationError):
					doc.save()
				self.assertEqual(frappe.db.get_value("Company", self.company,
					"custom_default_semi_finished_warehouse"), self.semi)

	def test_warehouse_disabled_after_configuration_rejects_plan_without_partial_documents(self):
		order = self._sales_order(60)
		frappe.db.set_value("Warehouse", self.semi, "disabled", 1)
		frappe.clear_document_cache("Warehouse", self.semi)
		before = {dt: frappe.db.count(dt) for dt in ("Production Plan", "Work Order", "Stock Reservation Entry")}
		with self.assertRaises(frappe.ValidationError):
			actions.create_work_order(order.name, order.items[0].name)
		self.assertEqual(before, {dt: frappe.db.count(dt) for dt in before})

	def test_pre_feature_work_order_without_snapshot_replenishes_to_original_shared_warehouse(self):
		self._configure(None)
		self._stock(self.upper, 60, self.stores)
		_, orders = self._plan()
		root = orders[self.fg]
		# Emulate an existing submitted record from before the snapshot column.
		# Persisted required-item warehouses and native PP facts remain intact.
		frappe.db.set_value("Work Order", root.name, "custom_semi_finished_warehouse", None)
		for name in frappe.get_all("Stock Reservation Entry", filters=dict(docstatus=1,
			voucher_type="Work Order", voucher_no=root.name), pluck="name"):
			frappe.get_doc("Stock Reservation Entry", name).cancel()
		frappe.get_doc(dict(doctype="Stock Entry", company=self.company, stock_entry_type="Material Issue",
			items=[dict(item_code=self.upper, qty=60, s_warehouse=self.stores)])).insert().submit()
		self._configure(self.semi)
		row = next(row for row in root.required_items if row.item_code == self.upper)
		created = service.create_replenishment_work_order(root.name, row.name)
		supplies = {doc.production_item: doc for doc in (
			frappe.get_doc("Work Order", name) for name in created["work_orders"])}
		for item in (self.lower, self.upper):
			supply = supplies[item]
			self.assertEqual(supply.fg_warehouse, self.stores)
			self.assertEqual(set(self._sources(supply).values()), {self.stores})
			self._issue(supply)
			self._receipt(supply)
		self._issue(root)
		self._receipt(root)
		self.assertEqual(self._actual(self.fg, self.finished), 60)
		self.assertEqual(self._actual(self.upper, self.semi), 0)

	def test_direct_wip_replenishment_receives_into_effective_wip_but_lower_levels_use_semi(self):
		self._stock(self.upper, 2, self.semi)
		_, orders = self._plan(qty=2)
		original = orders[self.fg]
		original.cancel()
		# Recreate the same native plan branch using ERPNext's supported direct
		# WIP-consumption mode. Its old source-stock reservation was cancelled.
		target = frappe.copy_doc(original)
		target.update(dict(docstatus=0, status="Draft", skip_transfer=1, from_wip_warehouse=1, reserve_stock=0,
			custom_semi_finished_warehouse=self.semi))
		target.insert().submit()
		self._stock(self.direct_raw, 2, self.wip)
		self._configure(self.other)
		row = next(row for row in target.required_items if row.item_code == self.upper)
		created = service.create_replenishment_work_order(target.name, row.name)
		supplies = {doc.production_item: doc for doc in (
			frappe.get_doc("Work Order", name) for name in created["work_orders"])}
		self.assertEqual(supplies[self.upper].fg_warehouse, self.wip)
		self.assertEqual(supplies[self.lower].fg_warehouse, self.semi)
		for item in (self.lower, self.upper):
			self.assertEqual(supplies[item].custom_semi_finished_warehouse, self.semi)
			self._issue(supplies[item])
			self._receipt(supplies[item])
		reservations = frappe.get_all("Stock Reservation Entry", filters=dict(docstatus=1,
			voucher_type="Work Order", voucher_no=target.name, item_code=self.upper),
			fields=["warehouse", "reserved_qty"])
		self.assertEqual([(r.warehouse, r.reserved_qty) for r in reservations], [(self.wip, 2)])
		self._receipt(target)
		self.assertEqual(self._actual(self.fg, self.finished), 2)
		self.assertEqual(self._actual(self.upper, self.wip), 0)
		self.assertEqual(self._actual(self.upper, self.semi), 2)
