from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate


class TestQuickOrderIntegration(IntegrationTestCase):
	def test_product_search_prioritizes_products_without_hiding_exact_matches(self):
		from process_simplification.api.quick_order import search_quick_order_products

		suffix = frappe.generate_hash(length=8)
		prefix = "QO-SEARCH-{0}".format(suffix)

		def make_item(suffix_name, *, item_group, is_stock_item):
			return frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "{0}-{1}".format(prefix, suffix_name),
					"item_name": "Quick Order Search {0}".format(suffix_name),
					"item_group": item_group,
					"stock_uom": "Nos",
					"is_stock_item": is_stock_item,
					"is_sales_item": 1,
				}
			).insert()

		product_item = make_item("SERVICE-PRODUCT", item_group="Products", is_stock_item=1)
		service_item = make_item("SERVICE", item_group="Services", is_stock_item=0)

		with patch("process_simplification.api.quick_order.frappe.has_permission", return_value=True):
			rows = search_quick_order_products("Item", prefix, "name", 0, 20, {})
			exact_rows = search_quick_order_products("Item", service_item.name, "name", 0, 20, {})

		self.assertEqual(rows[0][0], product_item.name)
		self.assertEqual(exact_rows[0][0], service_item.name)

	def test_product_search_uses_v16_product_bundle_lifecycle(self):
		from process_simplification.api.quick_order import (
			get_quick_order_item_defaults,
			search_quick_order_products,
		)
		from process_simplification.api.utils import SimplifiedFlowError

		suffix = frappe.generate_hash(length=8)
		prefix = "QO-PB-{0}".format(suffix)
		item_group = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Quick Order Bundle Products {0}".format(suffix),
				"parent_item_group": "All Item Groups",
				"is_group": 0,
			}
		).insert()

		def make_item(suffix_name, *, is_stock_item):
			return frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "{0}-{1}".format(prefix, suffix_name),
					"item_name": "Quick Order {0}".format(suffix_name),
					"item_group": item_group.name,
					"stock_uom": "Nos",
					"is_stock_item": is_stock_item,
					"is_sales_item": 1,
				}
			).insert()

		normal_item = make_item("NORMAL", is_stock_item=1)
		bundle_child = make_item("CHILD", is_stock_item=1)
		enabled_parent = make_item("ENABLED", is_stock_item=0)
		disabled_parent = make_item("DISABLED", is_stock_item=0)
		enabled_bundle = frappe.get_doc(
			{
				"doctype": "Product Bundle",
				"new_item_code": enabled_parent.name,
				"items": [{"item_code": bundle_child.name, "qty": 1}],
			}
		).insert()
		disabled_bundle = frappe.get_doc(
			{
				"doctype": "Product Bundle",
				"new_item_code": disabled_parent.name,
				"disabled": 1,
				"items": [{"item_code": bundle_child.name, "qty": 1}],
			}
		).insert()

		self.assertEqual(enabled_bundle.docstatus, 0)
		self.assertEqual(enabled_bundle.disabled, 0)
		self.assertEqual(disabled_bundle.docstatus, 0)
		self.assertEqual(disabled_bundle.disabled, 1)

		with patch("process_simplification.api.quick_order.frappe.has_permission", return_value=True):
			rows = search_quick_order_products("Item", prefix, "name", 0, 20, {})
			with self.assertRaises(SimplifiedFlowError):
				get_quick_order_item_defaults(enabled_parent.name)

		result_item_codes = {row[0] for row in rows}
		self.assertIn(normal_item.name, result_item_codes)
		self.assertIn(disabled_parent.name, result_item_codes)
		self.assertNotIn(enabled_parent.name, result_item_codes)

	def test_material_risk_preflight_and_cross_order_submission_use_shared_supply(self):
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		from process_simplification.api.quick_order import preflight_quick_sales_order

		suffix = frappe.generate_hash(length=8)
		abbr = "R{0}".format(suffix[:4]).upper()
		company = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": "Quick Order Risk {0}".format(suffix),
				"abbr": abbr,
				"default_currency": "INR",
				"country": "India",
				"create_chart_of_accounts_based_on": "Standard Template",
			}
		).insert()
		customer_group = frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": "QO Risk Customers {0}".format(suffix),
				"parent_customer_group": "All Customer Groups",
				"is_group": 0,
			}
		).insert()
		territory = frappe.get_doc(
			{
				"doctype": "Territory",
				"territory_name": "QO Risk Territory {0}".format(suffix),
				"parent_territory": "All Territories",
				"is_group": 0,
			}
		).insert()
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "QO Risk Customer {0}".format(suffix),
				"customer_type": "Company",
				"customer_group": customer_group.name,
				"territory": territory.name,
			}
		).insert()
		item_group = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "QO Risk Products {0}".format(suffix),
				"parent_item_group": "All Item Groups",
				"is_group": 0,
			}
		).insert()
		finished_good = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "QO-RISK-FG-{0}".format(suffix),
				"item_name": "Quick Order Risk Finished Good",
				"item_group": item_group.name,
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"is_sales_item": 1,
			}
		).insert()
		raw_material = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "QO-RISK-RM-{0}".format(suffix),
				"item_name": "Quick Order Risk Raw Material",
				"item_group": item_group.name,
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"is_purchase_item": 1,
				"valuation_rate": 5,
			}
		).insert()
		warehouse = "Stores - {0}".format(abbr)
		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": finished_good.name,
				"company": company.name,
				"currency": "INR",
				"quantity": 1,
				"is_active": 1,
				"is_default": 1,
				"items": [
					{
						"item_code": raw_material.name,
						"qty": 2,
						"uom": "Nos",
						"stock_uom": "Nos",
						"rate": 5,
						"source_warehouse": warehouse,
					}
				],
			}
		)
		# This environment's PyPika version rejects the recursive keyword used by
		# BOM.check_recursion. Keep the compatibility patch scoped to fixture setup;
		# preflight still reads and explodes the real submitted BOM from the database.
		with patch("erpnext.manufacturing.doctype.bom.bom.BOM.check_recursion"):
			bom.insert()
			bom.submit()
		self.assertEqual(bom.docstatus, 1)
		self.assertEqual(bom.is_default, 1)
		make_stock_entry(
			item_code=finished_good.name,
			to_warehouse=warehouse,
			company=company.name,
			qty=2,
			basic_rate=25,
		)
		make_stock_entry(
			item_code=raw_material.name,
			to_warehouse=warehouse,
			company=company.name,
			qty=1,
			basic_rate=5,
		)
		price_list = frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": "QO Risk Selling {0}".format(suffix),
				"enabled": 1,
				"selling": 1,
				"currency": "INR",
			}
		).insert()
		frappe.db.set_single_value("Selling Settings", "selling_price_list", price_list.name)

		payload = {
			"customer": customer.name,
			"delivery_date": add_days(nowdate(), 3),
			"items": [{"item_code": finished_good.name, "qty": 5, "rate": 25}],
		}
		defaults = frappe._dict(
			{
				"company": company.name,
				"fg_warehouse": warehouse,
				"source_warehouse": warehouse,
				"wip_warehouse": None,
			}
		)
		document_types = (
			"Work Order",
			"Material Request",
			"Purchase Order",
			"Stock Reservation Entry",
		)
		before_counts = {
			doctype: frappe.db.count(doctype, {"company": company.name}) for doctype in document_types
		}

		with (
			patch("process_simplification.api.quick_order.get_company_defaults", return_value=defaults),
			patch(
				"process_simplification.api.quick_order._item_price",
				return_value=frappe._dict(
					{"price_list": price_list.name, "price_list_rate": 25, "currency": "INR"}
				),
			),
		):
			result = preflight_quick_sales_order(payload)

		self.assertEqual(result["production_required"], 3)
		self.assertEqual(result["material_groups"][0]["bom_no"], bom.name)
		self.assertEqual(result["material_groups"][0]["available_to_reserve"], 2)
		self.assertTrue(result["material_groups"][0]["materials"])
		self.assertEqual(result["material_groups"][0]["materials"][0]["item_code"], raw_material.name)
		self.assertEqual(result["material_groups"][0]["materials"][0]["required_qty"], 6)
		self.assertEqual(result["material_coverage"][0]["actual_qty"], 1)
		self.assertEqual(result["material_coverage"][0]["available_qty"], 1)
		self.assertEqual(result["material_coverage"][0]["open_material_request_qty"], 0)
		self.assertEqual(result["material_coverage"][0]["open_purchase_order_qty"], 0)
		self.assertEqual(result["material_coverage"][0]["current_gap_qty"], 5)
		self.assertEqual(result["material_coverage"][0]["shortage_qty"], 5)
		self.assertEqual(result["shortage_item_count"], 1)
		self.assertIn("RAW_MATERIAL_SHORTAGE", [warning["code"] for warning in result["warnings"]])
		self.assertTrue(result["can_submit"])
		self.assertEqual(
			{
				doctype: frappe.db.count(doctype, {"company": company.name})
				for doctype in document_types
			},
			before_counts,
		)
		# A later order initially has enough FG + raw material. A new earlier
		# order takes the free FG, leaving a new raw-material gap only on the later
		# order. Exercise native submit, the notification hook and retry together.
		from process_simplification.api.quick_order import submit_quick_sales_order
		from process_simplification.api.shortage import calculate_company_purchase_shortages
		make_stock_entry(item_code=raw_material.name, to_warehouse=warehouse,
			company=company.name, qty=9, basic_rate=5)
		later = frappe.get_doc(dict(doctype="Sales Order", company=company.name,
			customer=customer.name, delivery_date=add_days(nowdate(), 10), selling_price_list=price_list.name,
			items=[dict(item_code=finished_good.name, qty=7, rate=25, warehouse=warehouse,
				delivery_date=add_days(nowdate(), 10))])).insert()
		later.submit()
		self.assertEqual(calculate_company_purchase_shortages(company.name), [])
		urgent = dict(payload, items=[dict(item_code=finished_good.name, qty=2, rate=25)])
		ledger_count = frappe.db.count("Stock Ledger Entry", {"company": company.name})
		with (
			patch("process_simplification.api.quick_order.get_company_defaults", return_value=defaults),
			patch("process_simplification.notifications.responsibility_recipients", side_effect=lambda company, role:
				["production"] if role == "生产派工" else ["warehouse"]),
			patch("process_simplification.notifications.notify_users", return_value=[]) as notify,
		):
			review = preflight_quick_sales_order(urgent)
			notify.assert_not_called()
			self.assertEqual(review["shortages"], [])
			self.assertEqual(review["downstream_impacts"][0]["sales_order"], later.name)
			self.assertEqual(review["downstream_impacts"][0]["materials"][0]["added_shortage_qty"], 4)
			request_key = frappe.generate_hash(length=32)
			make_stock_entry(item_code=raw_material.name, to_warehouse=warehouse,
				company=company.name, qty=1, basic_rate=5)
			ledger_count = frappe.db.count("Stock Ledger Entry", {"company": company.name})
			changed = submit_quick_sales_order(urgent, review["review_token"], request_key)
			self.assertEqual(changed["status"], "reconfirmation_required")
			self.assertEqual(changed["downstream_impacts"][0]["materials"][0]["added_shortage_qty"], 3)
			notify.assert_not_called()
			submitted = submit_quick_sales_order(urgent, changed["review_token"], request_key)
			replayed = submit_quick_sales_order(urgent, changed["review_token"], request_key)
			self.assertEqual(notify.call_count, 2)
			calls = {call.args[0][0]: call.kwargs for call in notify.call_args_list}
			self.assertIn("后续缺口", calls["warehouse"]["description"])
			self.assertIn("无需安排生产", calls["production"]["description"])
			self.assertIn("后续订单排期", calls["production"]["description"])
			for call in calls.values():
				self.assertIn("影响后续 1 单", call["description"])
			self.assertEqual(replayed["sales_order"], submitted["sales_order"])
			self.assertTrue(replayed["idempotent_replay"])
		after = calculate_company_purchase_shortages(company.name)
		self.assertEqual(after[0]["shortage_qty"], 3)
		self.assertEqual(frappe.db.count("Stock Ledger Entry", {"company": company.name}), ledger_count)
		self.assertEqual(frappe.db.count("Stock Reservation Entry", {"company": company.name}), before_counts["Stock Reservation Entry"])

	def test_stock_covered_order_preflights_and_submits_as_standard_sales_order(self):
		from process_simplification.api.quick_order import (
			preflight_quick_sales_order,
			submit_quick_sales_order,
		)

		suffix = frappe.generate_hash(length=8)
		abbr = "Q{0}".format(suffix[:4]).upper()
		company = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": "Quick Order E2E {0}".format(suffix),
				"abbr": abbr,
				"default_currency": "INR",
				"country": "India",
				"create_chart_of_accounts_based_on": "Standard Template",
			}
		).insert()

		customer_group = frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": "QO E2E Customers {0}".format(suffix),
				"parent_customer_group": "All Customer Groups",
				"is_group": 0,
			}
		).insert()
		territory = frappe.get_doc(
			{
				"doctype": "Territory",
				"territory_name": "QO E2E Territory {0}".format(suffix),
				"parent_territory": "All Territories",
				"is_group": 0,
			}
		).insert()
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "QO E2E Customer {0}".format(suffix),
				"customer_type": "Company",
				"customer_group": customer_group.name,
				"territory": territory.name,
			}
		).insert()
		item_group = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "QO E2E Products {0}".format(suffix),
				"parent_item_group": "All Item Groups",
				"is_group": 0,
			}
		).insert()
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "QO-E2E-ITEM-{0}".format(suffix),
				"item_name": "Quick Order E2E Item",
				"item_group": item_group.name,
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"is_sales_item": 1,
			}
		).insert()
		warehouse = "Stores - {0}".format(abbr)
		price_list = frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": "QO E2E Selling {0}".format(suffix),
				"enabled": 1,
				"selling": 1,
				"currency": "INR",
			}
		).insert()
		frappe.db.set_single_value("Selling Settings", "selling_price_list", price_list.name)

		payload = {
			"customer": customer.name,
			"delivery_date": add_days(nowdate(), 3),
			"po_no": "QO-E2E-{0}".format(frappe.generate_hash(length=8)),
			"remarks": "同批送达",
			"items": [{"item_code": item.name, "qty": 1, "rate": 25}],
		}
		defaults = frappe._dict(
			{
				"company": company.name,
				"fg_warehouse": warehouse,
				"source_warehouse": warehouse,
				"wip_warehouse": None,
			}
		)
		with (
			patch("process_simplification.api.quick_order.get_company_defaults", return_value=defaults),
			patch("process_simplification.api.quick_order.get_available_qty_to_reserve", return_value=10),
			patch("process_simplification.api.quick_order.get_default_bom", return_value=None),
			patch("process_simplification.notifications.responsibility_recipients", side_effect=lambda company, role:
				["production"] if role == "生产派工" else ["warehouse"]),
			patch("process_simplification.notifications.notify_users", return_value=[]) as notify,
			patch(
				"process_simplification.api.quick_order._item_price",
				return_value=frappe._dict(
					{"price_list": price_list.name, "price_list_rate": 25, "currency": "INR"}
				),
			),
		):
			preflight = preflight_quick_sales_order(payload)
			self.assertTrue(preflight["can_submit"])
			self.assertEqual(preflight["production_required"], 0)
			notify.assert_not_called()

			# Production code commits while holding the concurrency lock. Suppress that
			# commit here so IntegrationTestCase can roll the E2E document back.
			with patch("process_simplification.api.quick_order.frappe.db.commit"):
				result = submit_quick_sales_order(
					payload,
					preflight["review_token"],
					"QO-E2E-REQUEST-{0}".format(frappe.generate_hash(length=8)),
				)
			self.assertEqual(notify.call_count, 2)
			calls = {call.args[0][0]: call.kwargs for call in notify.call_args_list}
			self.assertIn("备货发货", calls["warehouse"]["description"])
			self.assertIn("无需安排生产", calls["production"]["description"])
			for call in calls.values():
				self.assertEqual(call["document_name"], result["sales_order"])
				self.assertEqual(call["link"], "/app/order-workbench?sales_order=" + result["sales_order"])

		order = frappe.get_doc("Sales Order", result["sales_order"])
		self.assertEqual(order.docstatus, 1)
		self.assertEqual(order.company, company.name)
		self.assertEqual(order.customer, customer.name)
		self.assertEqual(str(order.delivery_date), payload["delivery_date"])
		self.assertEqual(order.po_no, payload["po_no"])
		self.assertEqual(order.terms, "同批送达")
		self.assertEqual(order.grand_total, 25)
		self.assertEqual(order.items[0].item_code, item.name)
		self.assertEqual(order.items[0].qty, 1)
		self.assertEqual(order.items[0].rate, 25)
		self.assertEqual(order.items[0].uom, "Nos")
		self.assertEqual(order.items[0].warehouse, warehouse)
		self.assertEqual(result["route"], ["order-workbench", {"sales_order": order.name}])

	def test_direct_stock_order_reservation_and_delivery_preserve_quantity_chain(self):
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
		from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
			get_available_qty_to_reserve,
		)

		from process_simplification.api.actions import create_delivery_note, reserve_stock
		from process_simplification.api.quick_order import (
			preflight_quick_sales_order,
			submit_quick_sales_order,
		)
		from process_simplification.api.workbench import (
			get_effective_reserved_qty,
			get_order_workbench,
		)

		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", 1)
		frappe.db.set_single_value("Stock Settings", "allow_partial_reservation", 0)
		if frappe.get_meta("Stock Settings").has_field("auto_reserve_stock"):
			frappe.db.set_single_value("Stock Settings", "auto_reserve_stock", 0)

		suffix = frappe.generate_hash(length=8)
		abbr = "D{0}".format(suffix[:4]).upper()
		company = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": "Direct Stock E2E {0}".format(suffix),
				"abbr": abbr,
				"default_currency": "INR",
				"country": "India",
				"create_chart_of_accounts_based_on": "Standard Template",
			}
		).insert()
		customer_group = frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": "Direct Stock Customers {0}".format(suffix),
				"parent_customer_group": "All Customer Groups",
				"is_group": 0,
			}
		).insert()
		territory = frappe.get_doc(
			{
				"doctype": "Territory",
				"territory_name": "Direct Stock Territory {0}".format(suffix),
				"parent_territory": "All Territories",
				"is_group": 0,
			}
		).insert()
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "Direct Stock Customer {0}".format(suffix),
				"customer_type": "Company",
				"customer_group": customer_group.name,
				"territory": territory.name,
			}
		).insert()
		item_group = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Direct Stock Products {0}".format(suffix),
				"parent_item_group": "All Item Groups",
				"is_group": 0,
			}
		).insert()
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "DIRECT-STOCK-FG-{0}".format(suffix),
				"item_name": "Direct Stock Finished Good",
				"item_group": item_group.name,
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"is_sales_item": 1,
				"is_purchase_item": 0,
				"valuation_rate": 25,
			}
		).insert()
		warehouse = "Stores - {0}".format(abbr)
		price_list = frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": "Direct Stock Selling {0}".format(suffix),
				"enabled": 1,
				"selling": 1,
				"currency": "INR",
			}
		).insert()
		frappe.db.set_single_value("Selling Settings", "selling_price_list", price_list.name)

		make_stock_entry(item_code=item.name, to_warehouse=warehouse, qty=10, rate=25)
		self.assertEqual(
			frappe.db.get_value(
				"Bin", {"item_code": item.name, "warehouse": warehouse}, "actual_qty"
			),
			10,
		)

		guarded_doctypes = ("Production Plan", "Work Order", "Material Request")
		before_counts = {doctype: frappe.db.count(doctype) for doctype in guarded_doctypes}
		payload = {
			"customer": customer.name,
			"delivery_date": add_days(nowdate(), 3),
			"po_no": "DIRECT-STOCK-{0}".format(frappe.generate_hash(length=8)),
			"remarks": "现货直发",
			"items": [{"item_code": item.name, "qty": 6, "rate": 25}],
		}
		defaults = frappe._dict(
			{
				"company": company.name,
				"fg_warehouse": warehouse,
				"source_warehouse": warehouse,
				"wip_warehouse": None,
			}
		)
		with (
			patch("process_simplification.api.quick_order.get_company_defaults", return_value=defaults),
			patch("process_simplification.api.quick_order.get_default_bom", return_value=None),
			patch(
				"process_simplification.api.quick_order._item_price",
				return_value=frappe._dict(
					{"price_list": price_list.name, "price_list_rate": 25, "currency": "INR"}
				),
			),
		):
			preflight = preflight_quick_sales_order(payload)
			self.assertTrue(preflight["can_submit"])
			self.assertEqual(preflight["available_to_reserve"], 6)
			self.assertEqual(preflight["production_required"], 0)
			with patch("process_simplification.api.quick_order.frappe.db.commit"):
				result = submit_quick_sales_order(
					payload,
					preflight["review_token"],
					"DIRECT-STOCK-REQUEST-{0}".format(frappe.generate_hash(length=8)),
				)

		order = frappe.get_doc("Sales Order", result["sales_order"])
		order_item = order.items[0]
		self.assertEqual(order.docstatus, 1)
		self.assertEqual(order_item.qty, 6)
		self.assertEqual(order_item.delivered_qty, 0)
		self.assertEqual(
			frappe.db.count(
				"Stock Reservation Entry",
				{"voucher_type": "Sales Order", "voucher_no": order.name, "docstatus": 1},
			),
			0,
		)

		reservation_result = reserve_stock(order.name, order_item.name, qty=6)
		reservation = frappe.get_doc(
			"Stock Reservation Entry", reservation_result["stock_reservation_entry"]
		)
		self.assertEqual(reservation.docstatus, 1)
		self.assertEqual(reservation.reserved_qty, 6)
		self.assertEqual(get_effective_reserved_qty(order.name, order_item.name), 6)
		self.assertEqual(get_available_qty_to_reserve(item.name, warehouse), 4)
		self.assertEqual(
			frappe.db.get_value(
				"Bin", {"item_code": item.name, "warehouse": warehouse}, "actual_qty"
			),
			10,
		)

		delivery_result = create_delivery_note(order.name, order_item.name)
		delivery_note = frappe.get_doc("Delivery Note", delivery_result["delivery_note"])
		self.assertEqual(delivery_note.docstatus, 0)
		self.assertEqual(len(delivery_note.items), 1)
		self.assertEqual(delivery_note.items[0].qty, 6)
		self.assertEqual(
			frappe.db.get_value(
				"Bin", {"item_code": item.name, "warehouse": warehouse}, "actual_qty"
			),
			10,
		)
		self.assertEqual(get_effective_reserved_qty(order.name, order_item.name), 6)

		delivery_note.submit()
		order.reload()
		reservation.reload()
		self.assertEqual(order.items[0].delivered_qty, 6)
		self.assertEqual(order.per_delivered, 100)
		self.assertEqual(reservation.delivered_qty, 6)
		self.assertEqual(get_effective_reserved_qty(order.name, order_item.name), 0)
		self.assertEqual(
			frappe.db.get_value(
				"Bin", {"item_code": item.name, "warehouse": warehouse}, "actual_qty"
			),
			4,
		)
		self.assertEqual(get_available_qty_to_reserve(item.name, warehouse), 4)
		delivery_ledger = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_type": "Delivery Note", "voucher_no": delivery_note.name},
			fields=["actual_qty"],
		)
		self.assertEqual([entry.actual_qty for entry in delivery_ledger], [-6])
		workbench_row = get_order_workbench(order.name)["rows"][0]
		self.assertEqual(workbench_row["delivered_qty"], 6)
		self.assertEqual(workbench_row["pending_qty"], 0)
		self.assertEqual(workbench_row["reserved_qty"], 0)
		self.assertEqual(
			{doctype: frappe.db.count(doctype) for doctype in guarded_doctypes},
			before_counts,
		)
