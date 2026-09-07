from copy import deepcopy
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import add_days, nowdate

from process_simplification.api import shortage
from process_simplification.api.utils import SimplifiedFlowError, normalize_purchase_qty, normalize_qty
from process_simplification.purchasing import allocation
from process_simplification.tests import test_material_coverage_integration as coverage_fixtures


class TestPurchaseQuantityPrecisionUnit(UnitTestCase):
	def setUp(self):
		super().setUp()
		self.enterContext(patch("process_simplification.api.utils.get_quantity_precision", return_value=2))
		self.enterContext(patch.object(shortage, "get_quantity_precision", return_value=2))

	def test_unplanned_demand_covered_by_existing_request_has_no_float_gap(self):
		row = frappe._dict(item_code="WIRE", warehouse="Stores", current_gap_qty=9999 * 0.55 - 5445)
		supply = [{"name": "MR-EXISTING", "detail_name": "MR-LINE", "outstanding_qty": 54.45}]
		with patch.object(shortage, "_coverage_supply_documents", return_value=(supply, [])):
			shortage._allocate_multilevel_supply(row, "COMPANY", None, {}, {})
		self.assertEqual(row.current_gap_qty, 54.45)
		self.assertEqual(row.open_material_request_qty, 54.45)
		self.assertEqual(row.shortage_qty, 0)
		self.assertEqual(row.status, "purchase_request_pending")

	def test_coverage_normalizes_totals_and_sources_without_rounding_bom_ratio(self):
		row = {
			"item_code": "WIRE",
			"warehouse": "Stores",
			"required_qty": 9999 * 0.55,
			"current_gap_qty": 54.45000000000073,
			"open_material_request_qty": 54.45,
			"shortage_qty": 7.247535904753022e-13,
			"sources": [
				{
					"required_qty": 9999 * 0.55,
					"shortage_qty": 7.247535904753022e-13,
					"bom_qty_per_unit": 0.000001,
				}
			],
		}
		material = shortage._aggregate_multilevel_purchased_rows([row])[0]
		self.assertEqual(material.required_qty, 5499.45)
		self.assertEqual(material.shortage_qty, 0)
		self.assertEqual(material.sources[0]["shortage_qty"], 0)
		self.assertEqual(material.sources[0]["bom_qty_per_unit"], 0.000001)
		self.assertEqual(normalize_qty(0.000001), 0.000001)

	def test_company_boundary_filters_stale_positive_residue(self):
		row = {
			"item_code": "WIRE",
			"warehouse": "Stores",
			"supply_type": "purchased",
			"status": "new_purchase_required",
			"shortage_qty": 7.247535904753022e-13,
			"current_gap_qty": 54.45000000000073,
			"open_material_request_qty": 54.45,
		}
		with (
			patch.object(shortage, "_purchase_inputs_from_production_overview", return_value=({}, [])),
			patch.object(
				shortage, "calculate_multilevel_material_coverage", return_value={"requirements": [row]}
			),
		):
			self.assertEqual(shortage.calculate_company_purchase_shortages("COMPANY"), [])

	def test_small_order_contributions_are_aggregated_before_document_rounding(self):
		rows = []
		for index in range(2):
			row = frappe._dict(item_code="WIRE", warehouse="Stores", current_gap_qty=0.004)
			with patch.object(shortage, "_coverage_supply_documents", return_value=([], [])):
				shortage._allocate_multilevel_supply(row, "COMPANY", None, {}, {})
			row.sources = [{"sales_order_item": f"SOI-{index}", "shortage_qty": row.shortage_qty}]
			rows.append(row)
		material = shortage._aggregate_multilevel_purchased_rows(rows)[0]
		self.assertEqual(material.shortage_qty, 0.01)
		self.assertEqual(material.sources[0]["shortage_qty"], 0.004)
		requested = dict(material, purchase_qty=0.01)
		validated = shortage.revalidate_purchase_rows([requested], [material])
		self.assertEqual(validated[0].shortage_qty, 0.01)

	def _rows(self, purchase_qty, current_qty):
		requested = [
			{
				"item_code": "WIRE",
				"warehouse": "Stores",
				"purchase_qty": purchase_qty,
				"sources": [{"sales_order_item": "SOI-1"}],
			}
		]
		current = [
			{
				"item_code": "WIRE",
				"warehouse": "Stores",
				"sources": [{"sales_order_item": "SOI-1", "shortage_qty": current_qty}],
			}
		]
		return requested, current

	def test_stale_micro_gap_is_reported_as_no_shortage_without_creating_request(self):
		requested, current = self._rows(50, 7.247535904753022e-13)
		with (
			patch.object(shortage, "calculate_company_purchase_shortages", return_value=current),
			patch.object(frappe, "new_doc") as new_doc,
		):
			with self.assertRaisesRegex(SimplifiedFlowError, "已不再缺料"):
				shortage._create_material_request_locked(requested, "COMPANY", frappe._dict())
			new_doc.assert_not_called()

	def test_micro_purchase_and_nonfinite_numbers_are_rejected(self):
		for value in (7.247535904753022e-13, 0.004):
			with self.assertRaisesRegex(SimplifiedFlowError, "采购数量必须大于 0"):
				shortage.revalidate_purchase_rows(*self._rows(value, 54.45))
		for value in (float("nan"), float("inf")):
			with self.assertRaisesRegex(SimplifiedFlowError, "有效数字"):
				shortage._requested_purchase_qty({"purchase_qty": value})

	def test_equal_business_quantities_compare_after_rounding(self):
		validated = shortage.revalidate_purchase_rows(*self._rows(4.95, 4.949999999999996))
		self.assertEqual(validated[0].shortage_qty, 4.95)
		self.assertEqual(shortage._requested_purchase_qty(validated[0]), 4.95)

	def test_configured_high_precision_is_preserved(self):
		with (
			patch("process_simplification.api.utils.get_quantity_precision", return_value=6),
			patch.object(shortage, "get_quantity_precision", return_value=6),
		):
			self.assertEqual(normalize_purchase_qty(0.000001), 0.000001)
			self.assertEqual(shortage._requested_purchase_qty({"purchase_qty": 0.000001}), 0.000001)


class TestPurchaseQuantityPrecisionIntegration(IntegrationTestCase):
	TEST_COMPANY = "Material Coverage Test Company"
	TEST_COMPANY_ABBR = "MCT"
	_ensure_company = classmethod(coverage_fixtures.TestMaterialCoverageIntegration._ensure_company.__func__)
	_make_item = coverage_fixtures.TestMaterialCoverageIntegration._make_item

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._ensure_company(cls.TEST_COMPANY, cls.TEST_COMPANY_ABBR)
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.warehouse = "Stores - MCT"
		self.need_date = add_days(nowdate(), 1)

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def _wire(self):
		item = self._make_item("PRECISION-WIRE")
		item.stock_uom = "Meter"
		item.append("uoms", {"uom": "Meter", "conversion_factor": 1})
		item.save()
		return item

	def _request(self, item, qty):
		return (
			frappe.get_doc(
				{
					"doctype": "Material Request",
					"company": self.TEST_COMPANY,
					"material_request_type": "Purchase",
					"schedule_date": self.need_date,
					"items": [
						{
							"item_code": item.name,
							"qty": qty,
							"uom": "Meter",
							"conversion_factor": 1,
							"warehouse": self.warehouse,
							"schedule_date": self.need_date,
						}
					],
				}
			)
			.insert()
			.submit()
		)

	def test_real_bom_stock_and_request_cover_demand_and_real_new_gap_persists_exactly(self):
		wire = self._wire()
		fg = self._make_item("PRECISION-FG")
		bom = (
			frappe.get_doc(
				{
					"doctype": "BOM",
					"company": self.TEST_COMPANY,
					"item": fg.name,
					"quantity": 1,
					"is_active": 1,
					"is_default": 1,
					"items": [{"item_code": wire.name, "qty": 0.55, "uom": "Meter", "rate": 5}],
				}
			)
			.insert()
			.submit()
		)
		frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.TEST_COMPANY,
				"items": [
					{
						"item_code": wire.name,
						"qty": 5445,
						"uom": "Meter",
						"conversion_factor": 1,
						"t_warehouse": self.warehouse,
						"basic_rate": 5,
					}
				],
			}
		).insert().submit()
		existing = self._request(wire, 54.45)
		demands = [
			{
				"bom_no": bom.name,
				"qty": 9999,
				"source": {
					"sales_order_item": "PRECISION-SOI",
					"finished_item": fg.name,
					"delivery_date": self.need_date,
				},
			}
		]
		defaults = frappe._dict(company=self.TEST_COMPANY, source_warehouse=self.warehouse)
		with (
			patch.object(shortage, "_purchase_inputs_from_production_overview", return_value=({}, demands)),
			patch.object(shortage, "get_company_defaults", return_value=defaults),
		):
			self.assertEqual(shortage.calculate_company_purchase_shortages(self.TEST_COMPANY), [])
			existing.cancel()
			rows = shortage.calculate_company_purchase_shortages(self.TEST_COMPANY)
			self.assertEqual(len(rows), 1)
			self.assertEqual(rows[0].shortage_qty, 54.45)
			bad = deepcopy(rows)
			bad[0].purchase_qty = 7.247535904753022e-13
			before = frappe.db.count("Material Request")
			with self.assertRaisesRegex(SimplifiedFlowError, "必须大于 0"):
				shortage.create_material_request(bad, self.TEST_COMPANY, self.need_date)
			self.assertEqual(frappe.db.count("Material Request"), before)
			with patch.object(frappe.db, "commit"):
				result = shortage.create_material_request(rows, self.TEST_COMPANY, self.need_date)
			created = frappe.get_doc("Material Request", result["material_request"])
			self.assertEqual(created.docstatus, 1)
			self.assertEqual((created.items[0].qty, created.items[0].stock_qty), (54.45, 54.45))
			self.assertEqual(shortage.calculate_company_purchase_shortages(self.TEST_COMPANY), [])
		self.assertEqual(
			frappe.db.get_value("Bin", {"item_code": wire.name, "warehouse": self.warehouse}, "actual_qty"),
			5445,
		)

	def test_zero_requests_are_excluded_and_zero_rounding_allocations_leave_no_po(self):
		wire = self._wire()
		zero = self._request(wire, 7.247535904753022e-13)
		zero.reload()
		valid = self._request(wire, 54.45)
		self.assertEqual(zero.items[0].stock_qty, 0)
		listed = {row.name for row in allocation.list_requests()}
		self.assertNotIn(zero.name, listed)
		self.assertIn(valid.name, listed)
		supplier = frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": "PRECISION Supplier " + frappe.generate_hash(length=6),
				"supplier_group": "All Supplier Groups",
			}
		).insert()
		before = frappe.db.count("Purchase Order")
		with self.assertRaisesRegex(frappe.ValidationError, "精度|小数"):
			allocation.create_purchase_orders(
				valid.name,
				"tiny-quantity",
				{
					"quantities": {valid.items[0].name: 7.247535904753022e-13},
					"rows": [
						{
							"material_request_item": valid.items[0].name,
							"supplier": supplier.name,
							"qty": 7.247535904753022e-13,
							"uom": "Meter",
							"rate": 5,
							"schedule_date": self.need_date,
							"warehouse": self.warehouse,
						}
					],
				},
			)
		self.assertEqual(frappe.db.count("Purchase Order"), before)
