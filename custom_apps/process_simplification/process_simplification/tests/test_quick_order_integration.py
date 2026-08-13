from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate


class TestQuickOrderIntegration(IntegrationTestCase):
	def test_production_required_preflight_explains_material_risk_without_creating_documents(self):
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
		self.assertEqual(result["material_coverage"][0]["allocated_qty"], 1)
		self.assertEqual(result["material_coverage"][0]["intransit_qty"], 0)
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
