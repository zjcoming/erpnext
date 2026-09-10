"""Atomic net production quantities and physical reservations on real documents."""

from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate

from process_simplification.api import actions
from process_simplification.api import production_plan_adapter as adapter
from process_simplification.production_workflow import service
from process_simplification.production_workflow.stock_reservation import locked_available_qty
from process_simplification.tests.test_replenishment_chain_integration import ManufacturingStockFixture


class TestPlanningStockIntegration(ManufacturingStockFixture):
	def setUp(self):
		if (
			self._testMethodName
			== "test_existing_production_role_can_confirm_with_derived_stock_reservations"
		):
			self.fixture_company = "恒算科技"
		super().setUp()

	def test_stock_pool_lock_serializes_independent_database_sessions(self):
		from concurrent.futures import ThreadPoolExecutor
		from contextvars import Context

		# Use an existing committed fixture pool; neither connection changes its
		# stock. This verifies the row lock, including a pool with no current SRE.
		pool = frappe.db.get_value(
			"Bin", {"item_code": "_Test Item"}, ["item_code", "warehouse"], as_dict=True
		)
		self.assertIsNotNone(pool)
		site = frappe.local.site

		def competitor():
			frappe.init(site=site)
			frappe.connect()
			try:
				frappe.db.sql("SET SESSION innodb_lock_wait_timeout = 1")
				return locked_available_qty(pool.item_code, pool.warehouse)
			except frappe.QueryTimeoutError:
				return "blocked"
			finally:
				frappe.db.rollback()
				frappe.destroy()

		locked_available_qty(pool.item_code, pool.warehouse)
		with ThreadPoolExecutor(max_workers=1) as executor:
			self.assertEqual(executor.submit(Context().run, competitor).result(timeout=5), "blocked")
			frappe.db.rollback()
			self.assertIsInstance(executor.submit(Context().run, competitor).result(timeout=5), float)

	def test_supplement_for_direct_wip_consumption_receives_into_effective_warehouse(self):
		target = frappe.copy_doc(self.target)
		target.update({"skip_transfer": 1, "from_wip_warehouse": 1})
		target.insert().submit()
		created = service.create_replenishment_work_order(target.name, target.required_items[0].name)
		orders = {
			doc.production_item: doc
			for doc in (frappe.get_doc("Work Order", name) for name in created["work_orders"])
		}
		self.assertEqual(orders[self.items[3]].fg_warehouse, self.wip)
		for index in range(1, 4):
			self._issue(orders[self.items[index]])
			receipt = self._receipt(orders[self.items[index]])
		self.assertEqual({row.warehouse for row in self._reservations(receipt)}, {self.wip})
		self.assertEqual(sum(row.reserved_qty for row in self._reservations(receipt)), 2)
		self._receipt(target)
		self.assertEqual(target.produced_qty, 2)
		self.assertEqual(locked_available_qty(self.items[3], self.wip), 0)

	def _stock(self, item, qty):
		return (
			frappe.get_doc(
				{
					"doctype": "Stock Entry",
					"stock_entry_type": "Material Receipt",
					"company": self.company,
					"items": [{"item_code": item, "qty": qty, "t_warehouse": self.stores, "basic_rate": 5}],
				}
			)
			.insert()
			.submit()
		)

	def _sales_order(self, qty=10):
		currency = frappe.get_cached_value("Company", self.company, "default_currency")
		price_list = frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": self.prefix + frappe.generate_hash(length=4),
				"enabled": 1,
				"selling": 1,
				"currency": currency,
			}
		).insert()
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": self.prefix + frappe.generate_hash(length=4),
				"customer_type": "Company",
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
				"territory": "All Territories",
			}
		).insert()
		order = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"company": self.company,
				"customer": customer.name,
				"delivery_date": add_days(nowdate(), 10),
				"currency": currency,
				"selling_price_list": price_list.name,
				"items": [
					{
						"item_code": self.items[-1],
						"qty": qty,
						"rate": 100,
						"warehouse": self.stores,
						"bom_no": self.boms[-1],
						"delivery_date": add_days(nowdate(), 10),
					}
				],
			}
		).insert()
		order.submit()
		return order

	def _plan(self, qty=10, order=None):
		order = order or self._sales_order(qty)
		result = adapter.create_work_orders_via_production_plan(
			sales_order=order.name,
			sales_order_item=order.items[0].name,
			company=self.company,
			item_code=self.items[-1],
			bom_no=self.boms[-1],
			planned_qty=qty,
			fg_warehouse=self.stores,
			sub_assembly_warehouse=self.stores,
			source_warehouse=self.stores,
			delivery_date=order.delivery_date,
		)
		orders = {
			doc.production_item: doc
			for doc in (frappe.get_doc("Work Order", name) for name in result["work_orders"])
		}
		return result, orders

	def _reserved(self, order):
		return sum(service._active_work_order_item_reserved_qty(item) for item in order.required_items)

	def test_existing_one_is_locked_and_nine_produced_feed_all_ten(self):
		self._stock(self.items[3], 1)
		self._stock(self.items[0], 7)
		result, orders = self._plan()
		self.assertEqual(orders[self.items[3]].qty, 9)
		self.assertEqual(self._reserved(orders[self.items[4]]), 1)
		self.assertEqual(self._reserved(orders[self.items[1]]), 0, "Normal plans must not hoard raw material")
		self.assertEqual(locked_available_qty(self.items[3], self.stores), 0)
		self.assertEqual(
			frappe.db.get_value("Production Plan", result["production_plan"], "reserve_stock"), 1
		)
		for index in range(1, 4):
			self._issue(orders[self.items[index]])
			receipt = self._receipt(orders[self.items[index]])
			self.assertEqual(sum(row.reserved_qty for row in self._reservations(receipt)), 9)
			self.assertEqual(
				{row.voucher_no for row in self._reservations(receipt)}, {orders[self.items[index + 1]].name}
			)
		self.assertEqual(self._reserved(orders[self.items[4]]), 10)
		issue = self._issue(orders[self.items[4]])
		self.assertEqual(orders[self.items[4]].required_items[0].transferred_qty, 10)
		issue.cancel()
		self.assertEqual(self._reserved(orders[self.items[4]]), 10)

	def test_all_covered_skips_descendants_and_later_plan_cannot_reuse_stock(self):
		self._stock(self.items[3], 10)
		_, first = self._plan()
		self.assertEqual(len(first), 1)
		self.assertEqual(self._reserved(first[self.items[4]]), 10)
		_, second = self._plan()
		self.assertEqual(len(second), 4)
		self.assertEqual(second[self.items[3]].qty, 10)
		self.assertEqual(self._reserved(second[self.items[4]]), 0)

	def test_positive_projected_stock_without_physical_stock_does_not_reduce_plan(self):
		future = frappe.copy_doc(self.target)
		future.update({"production_item": self.items[3], "bom_no": self.boms[-2], "qty": 10})
		future.get_items_and_operations_from_bom()
		future.insert().submit()
		self.assertGreater(
			frappe.db.get_value(
				"Bin", {"item_code": self.items[3], "warehouse": self.stores}, "projected_qty"
			),
			0,
		)
		_, orders = self._plan()
		self.assertEqual([orders[item].qty for item in self.items[1:]], [10, 10, 10, 10])
		self.assertEqual(self._reserved(orders[self.items[4]]), 0)

	def test_native_partial_receipt_reserves_once_to_its_own_parent(self):
		_, orders = self._plan(qty=2)
		supply = orders[self.items[1]]
		self._issue(supply)
		entry = service._insert_guided_stock_entry(supply, "Manufacture", 1, service.RECEIPT_ACTION)
		entry.custom_process_workflow_action = None
		entry.save().submit()
		reservations = self._reservations(entry)
		self.assertEqual(sum(row.reserved_qty for row in reservations), 1)
		self.assertEqual({row.voucher_no for row in reservations}, {orders[self.items[2]].name})
		entry.cancel()
		self.assertEqual(self._reservations(entry), [])

	def test_existing_production_role_can_confirm_with_derived_stock_reservations(self):
		self._stock(self.items[4], 1)
		self._stock(self.items[3], 1)
		order = self._sales_order()
		frappe.set_user("qa.production@ps.test")
		self.addCleanup(frappe.set_user, "Administrator")
		self.assertFalse(frappe.has_permission("Stock Reservation Entry", "create"))
		with (
			patch.object(
				actions,
				"get_company_defaults",
				return_value=frappe._dict(wip_warehouse=self.wip, fg_warehouse=self.stores),
			),
			patch.object(
				actions,
				"resolve_production_source_warehouse",
				return_value=frappe._dict(can_use=True, warehouse=self.stores),
			),
		):
			result = actions.create_work_order(order.name, order.items[0].name)
		self.assertEqual(result["qty"], 9)
		self.assertEqual(result["reserved_finished_qty"], 1)
		self.assertEqual(
			frappe.db.get_value(
				"Work Order", {"name": ["in", result["work_orders"]], "production_item": self.items[3]}, "qty"
			),
			8,
		)
		self.assertEqual(locked_available_qty(self.items[3], self.stores), 0)

	def test_failed_reservation_leaves_no_plan_work_order_or_stock_lock(self):
		self._stock(self.items[3], 1)
		order = self._sales_order()
		before = {
			doctype: frappe.db.count(doctype)
			for doctype in ["Production Plan", "Work Order", "Stock Reservation Entry"]
		}
		with patch.object(service, "_new_work_order_reservation", return_value=None):
			with self.assertRaisesRegex(frappe.ValidationError, "未能完整预留"):
				self._plan(order=order)
		self.assertEqual(before, {doctype: frappe.db.count(doctype) for doctype in before})
		self.assertEqual(locked_available_qty(self.items[3], self.stores), 1)

	def test_released_stock_coverage_exposes_gap_and_can_create_replenishment(self):
		from process_simplification.api.production_readiness import get_production_plan_readiness

		self._stock(self.items[3], 10)
		result, orders = self._plan()
		target = orders[self.items[4]]
		reservations = frappe.get_all(
			"Stock Reservation Entry",
			filters={"docstatus": 1, "voucher_type": "Work Order", "voucher_no": target.name},
			pluck="name",
		)
		for name in reservations:
			frappe.get_doc("Stock Reservation Entry", name).cancel()
		frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Issue",
				"company": self.company,
				"items": [{"item_code": self.items[3], "qty": 10, "s_warehouse": self.stores}],
			}
		).insert().submit()

		readiness = get_production_plan_readiness(
			company=self.company, sales_order_items=[target.sales_order_item]
		)
		# The public read model identifies the missing source even though no
		# lower WO was ever created for this fully stock-covered level.
		material = next(
			row
			for plan in readiness[target.sales_order_item]
			for row in plan["work_orders"]
			if row["name"] == target.name
		)["required_items"][0]
		self.assertEqual(material["effective_reserved_qty"], 0)
		self.assertEqual(material["current_gap_qty"], 10)
		self.assertTrue(material["replenishment_required"])
		supplement = service.create_replenishment_work_order(target.name, target.required_items[0].name)
		self.assertEqual(frappe.db.get_value("Work Order", supplement["work_order"], "qty"), 10)

	def test_pending_original_production_is_not_duplicated_by_replenishment(self):
		_, orders = self._plan()
		target = orders[self.items[4]]
		before = frappe.db.count("Material Request")
		with self.assertRaisesRegex(frappe.ValidationError, "未覆盖缺口"):
			service.create_replenishment_work_order(target.name, target.required_items[0].name)
		self.assertEqual(frappe.db.count("Material Request"), before)

	def test_released_partial_coverage_is_replenished_before_original_child_finishes(self):
		from process_simplification.api.production_readiness import get_production_plan_readiness

		self._stock(self.items[3], 1)
		_, orders = self._plan()
		target = orders[self.items[4]]
		for name in frappe.get_all(
			"Stock Reservation Entry",
			filters={"docstatus": 1, "voucher_type": "Work Order", "voucher_no": target.name},
			pluck="name",
		):
			frappe.get_doc("Stock Reservation Entry", name).cancel()
		frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Issue",
				"company": self.company,
				"items": [{"item_code": self.items[3], "qty": 1, "s_warehouse": self.stores}],
			}
		).insert().submit()
		readiness = get_production_plan_readiness(
			company=self.company, sales_order_items=[target.sales_order_item]
		)
		material = next(
			row
			for plan in readiness[target.sales_order_item]
			for row in plan["work_orders"]
			if row["name"] == target.name
		)["required_items"][0]
		self.assertEqual(material["linked_pending_output_qty"], 9)
		self.assertEqual(material["uncovered_supply_qty"], 1)
		self.assertTrue(material["replenishment_required"])
		created = service.create_replenishment_work_order(target.name, target.required_items[0].name)
		self.assertEqual(frappe.db.get_value("Work Order", created["work_order"], "qty"), 1)

	def test_other_stock_issue_and_stock_reconciliation_cannot_take_reserved_semi(self):
		self._stock(self.items[3], 10)
		_, orders = self._plan()
		for doctype in ["Stock Entry", "Stock Reconciliation"]:
			frappe.db.savepoint("reserved_stock_guard")
			with (
				self.subTest(doctype=doctype),
				self.assertRaisesRegex(frappe.ValidationError, "[Rr]eserv|预留|units of"),
			):
				if doctype == "Stock Entry":
					frappe.get_doc(
						{
							"doctype": doctype,
							"stock_entry_type": "Material Issue",
							"company": self.company,
							"items": [{"item_code": self.items[3], "qty": 1, "s_warehouse": self.stores}],
						}
					).insert().submit()
				else:
					frappe.get_doc(
						{
							"doctype": doctype,
							"purpose": "Stock Reconciliation",
							"company": self.company,
							"items": [
								{
									"item_code": self.items[3],
									"warehouse": self.stores,
									"qty": 9,
									"valuation_rate": 5,
								}
							],
						}
					).insert().submit()
			frappe.db.rollback(save_point="reserved_stock_guard")
		self.assertEqual(self._reserved(orders[self.items[4]]), 10)
		orders[self.items[4]].reload().cancel()
		self.assertEqual(locked_available_qty(self.items[3], self.stores), 10)

	def test_finished_stock_deduction_is_reserved_and_rolls_back_with_plan_failure(self):
		self._stock(self.items[4], 1)
		order = self._sales_order()
		defaults = frappe._dict(wip_warehouse=self.wip, fg_warehouse=self.stores)
		with (
			patch.object(actions, "get_company_defaults", return_value=defaults),
			patch.object(
				actions,
				"resolve_production_source_warehouse",
				return_value=frappe._dict(can_use=True, warehouse=self.stores),
			),
		):
			with patch.object(
				actions,
				"create_work_orders_via_production_plan",
				side_effect=frappe.ValidationError("plan failure"),
			):
				with self.assertRaisesRegex(frappe.ValidationError, "plan failure"):
					actions.create_work_order(order.name, order.items[0].name)
			self.assertEqual(locked_available_qty(self.items[4], self.stores), 1)
			result = actions.create_work_order(order.name, order.items[0].name)
		self.assertEqual(result["qty"], 9)
		self.assertEqual(result["reserved_finished_qty"], 1)
		reservation = frappe.get_doc("Stock Reservation Entry", result["stock_reservation_entry"])
		self.assertEqual(
			(reservation.docstatus, reservation.voucher_no, reservation.voucher_detail_no),
			(1, order.name, order.items[0].name),
		)
		self.assertEqual(locked_available_qty(self.items[4], self.stores), 0)
