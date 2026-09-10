"""Real submitted stock movements; all fixture changes roll back after each test."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from process_simplification.production_workflow import service


class ManufacturingStockFixture(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(self._rollback_fixture)
		frappe.set_user("Administrator")
		self.company = getattr(self, "fixture_company", "_Test Company")
		self.prefix = "SUPPLY-CHAIN-" + frappe.generate_hash(length=8)
		self.stores = self._warehouse("Stores")
		self.wip = self._warehouse("WIP")
		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", 1)
		frappe.db.set_value("Company", self.company, "default_wip_warehouse", self.wip)
		frappe.clear_document_cache("Company", self.company)
		self.items = []
		self.boms = []
		for label in ["RAW", "FRAME", "COIL", "SEMI", "FG"]:
			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": self.prefix + "-" + label,
					"item_name": self.prefix + "-" + label,
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 1,
					"valuation_rate": 5,
				}
			).insert()
			if self.items:
				bom = frappe.get_doc(
					{
						"doctype": "BOM",
						"item": item.name,
						"company": self.company,
						"quantity": 1,
						"is_active": 1,
						"is_default": 1,
						"currency": frappe.get_cached_value("Company", self.company, "default_currency"),
						"default_source_warehouse": self.stores,
						"items": [
							{
								"item_code": self.items[-1],
								"qty": 1,
								"rate": 5,
								"bom_no": self.boms[-1] if self.boms else None,
							}
						],
					}
				).insert()
				bom.submit()
				self.boms.append(bom.name)
			self.items.append(item.name)
		frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [
					{"item_code": self.items[0], "qty": 2, "t_warehouse": self.stores, "basic_rate": 5}
				],
			}
		).insert().submit()
		self.target = frappe.get_doc(
			{
				"doctype": "Work Order",
				"company": self.company,
				"production_item": self.items[-1],
				"bom_no": self.boms[-1],
				"qty": 2,
				"use_multi_level_bom": 0,
				"source_warehouse": self.stores,
				"wip_warehouse": self.wip,
				"fg_warehouse": self.stores,
				"planned_start_date": now_datetime(),
				"transfer_material_against": "Work Order",
			}
		)
		self.target.get_items_and_operations_from_bom()
		self.target.insert().submit()

	def _warehouse(self, label):
		return (
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": self.prefix + "-" + label,
					"company": self.company,
				}
			)
			.insert()
			.name
		)

	def _rollback_fixture(self):
		frappe.db.rollback()
		frappe.clear_document_cache("Company", self.company)

	def _issue(self, work_order):
		request = service.request_material_issue(work_order.name)
		entry = frappe.get_doc("Stock Entry", request["stock_entry"])
		entry.submit()
		work_order.reload()
		return entry

	def _receipt(self, work_order, qty=None):
		if qty:
			entry = service._insert_guided_stock_entry(work_order, "Manufacture", qty, service.RECEIPT_ACTION)
		else:
			request = service.request_manufacture(work_order.name)
			entry = frappe.get_doc("Stock Entry", request["stock_entry"])
		entry.submit()
		work_order.reload()
		return entry

	def _reservations(self, entry):
		return frappe.get_all(
			"Stock Reservation Entry",
			filters={
				"docstatus": 1,
				"from_voucher_type": "Stock Entry",
				"from_voucher_no": entry.name,
			},
			fields=[
				"name",
				"voucher_no",
				"voucher_detail_no",
				"warehouse",
				"reserved_qty",
				"transferred_qty",
			],
		)


class TestReplenishmentChainIntegration(ManufacturingStockFixture):
	def setUp(self):
		super().setUp()
		self.created = service.create_replenishment_work_order(
			self.target.name, self.target.required_items[0].name
		)
		self.orders = {
			doc.production_item: doc
			for doc in (frappe.get_doc("Work Order", name) for name in self.created["work_orders"])
		}

	def test_existing_chain_repeated_creation_internal_receipts_and_final_feed(self):
		repeated = service.create_replenishment_work_order(
			self.target.name, self.target.required_items[0].name
		)
		self.assertTrue(repeated["reused"])
		self.assertEqual(repeated["work_order"], self.created["work_order"])
		self.assertEqual(repeated["production_plan"], self.created["production_plan"])
		self.assertEqual(len(self.orders), 3)
		self.assertTrue(all(not doc.sales_order and not doc.sales_order_item for doc in self.orders.values()))
		receipts = []
		for index in range(1, 4):
			supply = self.orders[self.items[index]]
			target = self.orders[self.items[index + 1]] if index < 3 else self.target
			self._issue(supply)
			receipt = self._receipt(supply)
			receipts.append(receipt)
			reservations = self._reservations(receipt)
			self.assertEqual(sum(row.reserved_qty for row in reservations), 2)
			self.assertEqual({row.voucher_no for row in reservations}, {target.name})
			self.assertEqual({row.warehouse for row in reservations}, {self.stores})
			# Re-running the derived hook cannot reserve the same output twice.
			self.assertEqual(service.reserve_replenishment_output(receipt).reserved_qty, 0)
			self.assertEqual(len(self._reservations(receipt)), len(reservations))
		target_issue = self._issue(self.target)
		self.assertEqual(self.target.required_items[0].transferred_qty, 2)
		self.assertEqual(sum(row.transferred_qty for row in self._reservations(receipts[-1])), 2)
		self.assertEqual(
			frappe.db.get_value("Bin", {"item_code": self.items[3], "warehouse": self.stores}, "actual_qty"),
			0,
		)
		target_issue.cancel()
		self.assertEqual(sum(row.transferred_qty for row in self._reservations(receipts[-1])), 0)
		self.assertEqual(sum(row.reserved_qty for row in self._reservations(receipts[-1])), 2)
		self.assertEqual(
			frappe.db.get_value("Bin", {"item_code": self.items[3], "warehouse": self.stores}, "actual_qty"),
			2,
		)

	def test_partial_receipt_cancel_releases_only_its_own_reservation(self):
		supply = self.orders[self.items[1]]
		self._issue(supply)
		first = self._receipt(supply, qty=1)
		second = self._receipt(supply, qty=1)
		self.assertEqual(sum(row.reserved_qty for row in self._reservations(first)), 1)
		self.assertEqual(sum(row.reserved_qty for row in self._reservations(second)), 1)
		second.cancel()
		self.assertEqual(self._reservations(second), [])
		self.assertEqual(sum(row.reserved_qty for row in self._reservations(first)), 1)
		self.assertEqual(frappe.db.get_value("Work Order", supply.name, "produced_qty"), 1)

	def test_reservation_failure_rolls_back_receipt_in_the_request_transaction(self):
		supply = self.orders[self.items[1]]
		self._issue(supply)
		request = service.request_manufacture(supply.name)
		entry = frappe.get_doc("Stock Entry", request["stock_entry"])
		frappe.db.savepoint("receipt_submit")
		with patch.object(service, "_new_work_order_reservation", return_value=None):
			with self.assertRaises(frappe.ValidationError):
				entry.submit()
		frappe.db.rollback(save_point="receipt_submit")
		self.assertEqual(frappe.db.get_value("Stock Entry", entry.name, "docstatus"), 0)
		self.assertEqual(frappe.db.get_value("Work Order", supply.name, "produced_qty"), 0)
		self.assertEqual(self._reservations(entry), [])

	def test_closed_target_allows_receipt_without_stale_directed_reservation(self):
		supply = self.orders[self.items[1]]
		target = self.orders[self.items[2]]
		self._issue(supply)
		frappe.db.set_value("Work Order", target.name, "status", "Stopped")
		receipt = self._receipt(supply)
		self.assertEqual(self._reservations(receipt), [])
		self.assertEqual(service.reserve_replenishment_output(receipt).reason, "target_terminal")
		self.assertEqual(
			frappe.db.get_value(
				"Bin", {"item_code": supply.production_item, "warehouse": self.stores}, "actual_qty"
			),
			2,
		)
