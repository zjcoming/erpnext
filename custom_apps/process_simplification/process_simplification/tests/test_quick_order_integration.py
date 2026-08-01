from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate


class TestQuickOrderIntegration(IntegrationTestCase):
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

			# Production code commits while holding the concurrency lock. Suppress that
			# commit here so IntegrationTestCase can roll the E2E document back.
			with patch("process_simplification.api.quick_order.frappe.db.commit"):
				result = submit_quick_sales_order(
					payload,
					preflight["review_token"],
					"QO-E2E-REQUEST-{0}".format(frappe.generate_hash(length=8)),
				)

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
		self.assertEqual(result["route"], ["order-workbench", order.name])
