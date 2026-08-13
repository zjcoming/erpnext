from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate


class TestMaterialCoverageIntegration(IntegrationTestCase):
	def _make_item(self, prefix, *, uoms=None):
		item_code = "{0}-{1}".format(prefix, frappe.generate_hash(length=8))
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": item_code,
				"description": item_code,
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"is_purchase_item": 1,
				"valuation_rate": 5,
			}
		)
		for row in uoms or []:
			item.append("uoms", row)
		item.insert()
		return item

	def _make_warehouse(self, prefix, **properties):
		warehouse = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": "{0} {1}".format(prefix, frappe.generate_hash(length=6)),
				"parent_warehouse": "_Test Warehouse Group - _TC",
				"company": "_Test Company",
				**properties,
			}
		)
		warehouse.insert()
		return warehouse.name

	def _make_purchase_order(
		self,
		*,
		item_code,
		warehouse,
		schedule_date,
		qty,
		uom="Nos",
		conversion_factor=1,
		material_request=None,
		material_request_item=None,
	):
		po = frappe.new_doc("Purchase Order")
		po.company = "_Test Company"
		po.supplier = "_Test Supplier"
		po.transaction_date = nowdate()
		po.schedule_date = schedule_date
		po.append(
			"items",
			{
				"item_code": item_code,
				"warehouse": warehouse,
				"schedule_date": schedule_date,
				"qty": qty,
				"uom": uom,
				"conversion_factor": conversion_factor,
				"rate": 5,
				"material_request": material_request,
				"material_request_item": material_request_item,
			},
		)
		po.insert()
		po.submit()
		return po

	def _make_material_request(self, *, item_code, warehouse, schedule_date, qty):
		mr = frappe.new_doc("Material Request")
		mr.material_request_type = "Purchase"
		mr.company = "_Test Company"
		mr.transaction_date = nowdate()
		mr.schedule_date = schedule_date
		mr.append(
			"items",
			{
				"item_code": item_code,
				"warehouse": warehouse,
				"schedule_date": schedule_date,
				"qty": qty,
				"uom": "Nos",
				"conversion_factor": 1,
			},
		)
		mr.insert()
		mr.submit()
		return mr

	def test_real_purchase_supply_uses_stock_uom_and_filters_company_warehouse_and_need_date(self):
		from erpnext.buying.doctype.purchase_order.mapper import make_purchase_receipt
		from process_simplification.api.shortage import _po_outstanding

		need_date = add_days(nowdate(), 5)
		warehouse = "_Test Warehouse - _TC"
		other_warehouse = self._make_warehouse("QO Other")
		uom = "QO Box {0}".format(frappe.generate_hash(length=6))
		frappe.get_doc({"doctype": "UOM", "uom_name": uom}).insert()
		item = self._make_item("QO-PO-RM", uoms=[{"uom": uom, "conversion_factor": 5}])

		early_po = self._make_purchase_order(
			item_code=item.name,
			warehouse=warehouse,
			schedule_date=add_days(need_date, -1),
			qty=10,
			uom=uom,
			conversion_factor=5,
		)
		receipt = make_purchase_receipt(early_po.name)
		receipt.items[0].qty = 4
		receipt.insert()
		receipt.submit()
		early_po.reload()
		self.assertEqual(early_po.items[0].stock_qty, 50)
		self.assertEqual(early_po.items[0].received_qty, 4)

		self._make_purchase_order(
			item_code=item.name,
			warehouse=warehouse,
			schedule_date=add_days(need_date, 1),
			qty=3,
			uom=uom,
			conversion_factor=5,
		)
		self._make_purchase_order(
			item_code=item.name,
			warehouse=other_warehouse,
			schedule_date=add_days(need_date, -1),
			qty=2,
			uom=uom,
			conversion_factor=5,
		)

		self.assertEqual(_po_outstanding(item.name, warehouse, "_Test Company", need_date), 30)
		self.assertEqual(_po_outstanding(item.name, other_warehouse, "_Test Company", need_date), 10)
		self.assertEqual(_po_outstanding(item.name, warehouse, "Not The Company", need_date), 0)

	def test_real_material_request_and_linked_purchase_order_do_not_double_count(self):
		from erpnext.buying.doctype.purchase_order.mapper import make_purchase_receipt
		from erpnext.stock.doctype.material_request.mapper import make_purchase_order

		from process_simplification.api.shortage import (
			_mr_outstanding,
			_po_outstanding,
			calculate_material_coverage,
		)

		need_date = add_days(nowdate(), 5)
		warehouse = "_Test Warehouse - _TC"
		item = self._make_item("QO-MR-RM")
		open_request = self._make_material_request(
			item_code=item.name,
			warehouse=warehouse,
			schedule_date=add_days(need_date, -1),
			qty=10,
		)
		converted_request = self._make_material_request(
			item_code=item.name,
			warehouse=warehouse,
			schedule_date=add_days(need_date, -1),
			qty=20,
		)
		self._make_material_request(
			item_code=item.name,
			warehouse=warehouse,
			schedule_date=add_days(need_date, 1),
			qty=100,
		)

		po = make_purchase_order(converted_request.name)
		po.supplier = "_Test Supplier"
		po.items[0].warehouse = warehouse
		po.items[0].schedule_date = add_days(need_date, -1)
		po.insert()
		po.submit()
		receipt = make_purchase_receipt(po.name)
		receipt.items[0].qty = 5
		receipt.insert()
		receipt.submit()
		finished_good = self._make_item("QO-MR-FG")
		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": finished_good.name,
				"company": "_Test Company",
				"currency": "INR",
				"quantity": 1,
				"is_active": 1,
				"is_default": 1,
				"items": [
					{
						"item_code": item.name,
						"qty": 35,
						"uom": "Nos",
						"stock_uom": "Nos",
						"rate": 5,
					}
				],
			}
		)
		with patch("erpnext.manufacturing.doctype.bom.bom.BOM.check_recursion"):
			bom.insert()
			bom.submit()

		open_request.reload()
		converted_request.reload()
		self.assertEqual(open_request.items[0].ordered_qty, 0)
		self.assertEqual(converted_request.items[0].ordered_qty, 20)
		self.assertEqual(_mr_outstanding(item.name, warehouse, "_Test Company", need_date), 10)
		self.assertEqual(_po_outstanding(item.name, warehouse, "_Test Company", need_date), 15)
		self.assertEqual(_mr_outstanding(item.name, warehouse, "Not The Company", need_date), 0)
		coverage = calculate_material_coverage(
			[{"bom_no": bom.name, "qty": 1}],
			"_Test Company",
			need_by_date=need_date,
			defaults=frappe._dict({"source_warehouse": warehouse}),
		)
		self.assertEqual(coverage.materials[0]["available_qty"], 5)
		self.assertEqual(coverage.materials[0]["open_material_request_qty"], 10)
		self.assertEqual(coverage.materials[0]["open_purchase_order_qty"], 15)
		self.assertEqual(coverage.materials[0]["shortage_qty"], 5)

	def test_operation_bom_uses_item_rows_and_the_same_validated_work_order_source_warehouse(self):
		from process_simplification.api.shortage import calculate_material_coverage

		company = "_Test Company"
		work_order_source = "_Test Warehouse - _TC"
		bom_line_source = self._make_warehouse("QO BOM Source")
		finished_good = self._make_item("QO-OP-FG")
		raw_material = self._make_item("QO-OP-RM")
		workstation_name = "QO Workstation {0}".format(frappe.generate_hash(length=6))
		workstation = frappe.get_doc(
			{"doctype": "Workstation", "workstation_name": workstation_name}
		).insert()
		operation_name = "QO Operation {0}".format(frappe.generate_hash(length=6))
		frappe.get_doc(
			{"doctype": "Operation", "name": operation_name, "workstation": workstation.name}
		).insert()
		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": finished_good.name,
				"company": company,
				"currency": "INR",
				"quantity": 1,
				"is_active": 1,
				"is_default": 1,
				"with_operations": 1,
				"operations": [
					{
						"operation": operation_name,
						"workstation": workstation.name,
						"time_in_mins": 10,
						"operating_cost": 1,
					}
				],
				"items": [
					{
						"item_code": raw_material.name,
						"qty": 2,
						"uom": "Nos",
						"stock_uom": "Nos",
						"rate": 5,
						"operation": operation_name,
						"source_warehouse": bom_line_source,
					}
				],
			}
		)
		with patch("erpnext.manufacturing.doctype.bom.bom.BOM.check_recursion"):
			bom.insert()
			bom.submit()

		result = calculate_material_coverage(
			[
				{
					"bom_no": bom.name,
					"qty": 3,
					"source": {
						"row": 1,
						"finished_item": finished_good.name,
						"sales_order_item_warehouse": bom_line_source,
					},
				}
			],
			company,
			need_by_date=add_days(nowdate(), 3),
			defaults=frappe._dict({"source_warehouse": work_order_source}),
		)

		self.assertEqual(len(result.materials), 1)
		self.assertEqual(result.materials[0]["item_code"], raw_material.name)
		self.assertEqual(result.materials[0]["required_qty"], 6)
		self.assertEqual(result.materials[0]["warehouse"], work_order_source)

	def test_production_source_resolver_rejects_group_disabled_and_other_company_warehouses(self):
		from process_simplification.api.setup import resolve_production_source_warehouse

		fallback = resolve_production_source_warehouse(
			"_Test Company",
			defaults=frappe._dict({"source_warehouse": None}),
			sales_order_item_warehouse="_Test Warehouse - _TC",
		)
		self.assertTrue(fallback.can_use)
		self.assertEqual(fallback.warehouse, "_Test Warehouse - _TC")

		group_warehouse = self._make_warehouse("QO Group", is_group=1)
		disabled_warehouse = self._make_warehouse("QO Disabled", disabled=1)

		for company, source_warehouse in (
			("_Test Company", group_warehouse),
			("_Test Company", disabled_warehouse),
			("_Test Company 1", "_Test Warehouse - _TC"),
		):
			with self.subTest(company=company, source_warehouse=source_warehouse):
				result = resolve_production_source_warehouse(
					company,
					defaults=frappe._dict({"source_warehouse": source_warehouse}),
					sales_order_item_warehouse="_Test Warehouse - _TC",
				)
				self.assertFalse(result.can_use)
				self.assertEqual(result.warehouse, source_warehouse)

	def test_supply_documents_expose_material_request_and_purchase_order_details(self):
		from erpnext.buying.doctype.purchase_order.mapper import make_purchase_receipt

		from process_simplification.api.shortage import (
			_mr_documents,
			_mr_outstanding,
			_po_documents,
			_po_outstanding,
		)

		# Self-contained fixtures (fresh company/warehouse) so the test does not
		# depend on the site-level _Test Warehouse fixtures.
		suffix = frappe.generate_hash(length=8)
		abbr = "V{0}".format(suffix[:4]).upper()
		company = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": "Supply Vis {0}".format(suffix),
				"abbr": abbr,
				"default_currency": "INR",
				"country": "India",
				"create_chart_of_accounts_based_on": "Standard Template",
			}
		).insert()
		warehouse = "Stores - {0}".format(abbr)
		supplier = frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": "Supply Vis Supplier {0}".format(suffix),
				"supplier_group": "All Supplier Groups",
			}
		).insert()
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "SUP-RM-{0}".format(suffix),
				"item_name": "Supply Vis RM",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"is_purchase_item": 1,
				"valuation_rate": 5,
			}
		).insert()
		need_date = add_days(nowdate(), 5)

		mr = frappe.new_doc("Material Request")
		mr.material_request_type = "Purchase"
		mr.company = company.name
		mr.transaction_date = nowdate()
		mr.schedule_date = add_days(need_date, -1)
		mr.append("items", {
			"item_code": item.name, "warehouse": warehouse,
			"schedule_date": add_days(need_date, -1), "qty": 10, "uom": "Nos", "conversion_factor": 1,
		})
		mr.insert()
		mr.submit()

		po = frappe.new_doc("Purchase Order")
		po.company = company.name
		po.supplier = supplier.name
		po.transaction_date = nowdate()
		po.schedule_date = add_days(need_date, -1)
		po.append("items", {
			"item_code": item.name, "warehouse": warehouse,
			"schedule_date": add_days(need_date, -1), "qty": 8, "uom": "Nos", "conversion_factor": 1, "rate": 5,
		})
		po.insert()
		po.submit()
		receipt = make_purchase_receipt(po.name)
		receipt.items[0].qty = 3
		receipt.insert()
		receipt.submit()

		mr_docs = _mr_documents(item.name, warehouse, company.name, need_date)
		po_docs = _po_documents(item.name, warehouse, company.name, need_date)

		# One outstanding MR document (10 open) and one partially received PO (5 open).
		self.assertEqual(len(mr_docs), 1)
		self.assertEqual(mr_docs[0]["doctype"], "Material Request")
		self.assertEqual(mr_docs[0]["name"], mr.name)
		self.assertEqual(mr_docs[0]["outstanding_qty"], 10)

		self.assertEqual(len(po_docs), 1)
		self.assertEqual(po_docs[0]["doctype"], "Purchase Order")
		self.assertEqual(po_docs[0]["name"], po.name)
		self.assertEqual(po_docs[0]["outstanding_qty"], 5)

		# Summed outstanding must exactly equal the detail-based totals (口径不变).
		self.assertEqual(
			_mr_outstanding(item.name, warehouse, company.name, need_date),
			sum(doc["outstanding_qty"] for doc in mr_docs),
		)
		self.assertEqual(
			_po_outstanding(item.name, warehouse, company.name, need_date),
			sum(doc["outstanding_qty"] for doc in po_docs),
		)

		# A Purchase Order due AFTER the deadline is still listed, flagged late,
		# and excluded from the on-time outstanding total.
		late_po = frappe.new_doc("Purchase Order")
		late_po.company = company.name
		late_po.supplier = supplier.name
		late_po.transaction_date = nowdate()
		late_po.schedule_date = add_days(need_date, 10)
		late_po.append("items", {
			"item_code": item.name, "warehouse": warehouse,
			"schedule_date": add_days(need_date, 10), "qty": 4, "uom": "Nos", "conversion_factor": 1, "rate": 5,
		})
		late_po.insert()
		late_po.submit()

		po_docs_after = _po_documents(item.name, warehouse, company.name, need_date)
		late = [d for d in po_docs_after if d["name"] == late_po.name]
		self.assertEqual(len(late), 1)
		self.assertTrue(late[0]["is_late"])
		# On-time PO outstanding is unchanged (still just the 5 from the early PO).
		self.assertEqual(_po_outstanding(item.name, warehouse, company.name, need_date), 5)

	def test_material_coverage_attaches_supply_documents(self):
		from unittest.mock import patch

		from process_simplification.api.shortage import calculate_material_coverage

		supply_docs = [
			{"doctype": "Material Request", "name": "MR-1", "status": "Pending", "outstanding_qty": 10, "is_late": False},
			{"doctype": "Purchase Order", "name": "PO-1", "status": "To Receive", "outstanding_qty": 5, "is_late": False},
		]

		with (
			patch("process_simplification.api.shortage.resolve_production_source_warehouse",
				return_value=frappe._dict({"warehouse": "_Test Warehouse - _TC", "can_use": True, "reason": None})),
			patch("process_simplification.api.shortage.get_bom_items_as_dict", return_value={
				"RM-1": frappe._dict({"item_code": "RM-1", "source_warehouse": "_Test Warehouse - _TC", "qty": 20})}),
			patch("process_simplification.api.shortage.get_material_stock_snapshot",
				return_value=frappe._dict({"can_calculate": True, "actual_qty": 2, "committed_qty": 0, "available_qty": 2})),
			patch("process_simplification.api.shortage._intransit_purchase_for_soi", return_value=15),
			patch("process_simplification.api.shortage._soi_supply_documents", return_value=supply_docs),
		):
			result = calculate_material_coverage(
				[{"bom_no": "BOM-1", "qty": 1, "source": {"sales_order_item": "SOI-1"}}],
				"_Test Company",
				need_by_date=add_days(nowdate(), 3),
				defaults=frappe._dict({"source_warehouse": "_Test Warehouse - _TC"}),
			)

		material = result.materials[0]
		# Per-SOI supply documents are attached; the attributed in-transit (15)
		# covers the gap after stock (18) partially -> 3 still short.
		self.assertEqual([d["name"] for d in material["supply_documents"]], ["MR-1", "PO-1"])
		self.assertEqual(material["intransit_qty"], 15)
		self.assertEqual(material["allocated_qty"], 2)
		self.assertEqual(material["shortage_qty"], 3)
