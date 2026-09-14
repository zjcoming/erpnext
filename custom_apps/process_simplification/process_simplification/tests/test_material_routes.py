import frappe
from frappe.tests import UnitTestCase
from process_simplification.production_exceptions.material_routes import aggregate_routes, stock_rows


class TestMaterialRoutes(UnitTestCase):
	def test_alternative_stock_uom_multiple_sources_and_batch_consumption(self):
		entries = [
			frappe._dict(name="I1", purpose="Material Transfer for Manufacture", docstatus=1, is_return=0),
			frappe._dict(name="I2", purpose="Material Transfer for Manufacture", docstatus=1, is_return=0),
			frappe._dict(name="C", purpose="Manufacture", docstatus=1, is_return=0),
			frappe._dict(
				name="R",
				purpose="Material Transfer for Manufacture",
				docstatus=1,
				is_return=1,
				custom_return_source_warehouse="S2",
			),
		]
		rows = [
			frappe._dict(
				parent="I1",
				item_code="ALT",
				original_item="RM",
				s_warehouse="S1",
				t_warehouse="WIP",
				stock_uom="Kg",
				qty=99,
				transfer_qty=1.5,
				batch_no="B1",
			),
			frappe._dict(
				parent="I2",
				item_code="ALT",
				original_item="RM",
				s_warehouse="S2",
				t_warehouse="WIP",
				stock_uom="Kg",
				transfer_qty=2.25,
				batch_no="B2",
			),
			frappe._dict(
				parent="C",
				item_code="ALT",
				original_item="RM",
				s_warehouse="WIP",
				stock_uom="Kg",
				transfer_qty=0.5,
				batch_no="B1",
			),
			frappe._dict(
				parent="R",
				item_code="ALT",
				original_item="RM",
				s_warehouse="WIP",
				t_warehouse="SCRAP",
				stock_uom="Kg",
				transfer_qty=0.25,
				batch_no="B2",
			),
		]
		routes = aggregate_routes(entries, rows)
		self.assertEqual([r.native_available_qty for r in routes], [1.0, 2.0])
		self.assertEqual(routes[0].original_item, "RM")
		self.assertEqual(stock_rows(routes[1], 1.25, "Q")[0]["qty"], 1.25)
		self.assertEqual(stock_rows(routes[1], 1.25, "Q")[0]["batch_no"], "B2")

	def test_serials_are_removed_by_identity_and_cannot_be_fractionally_returned(self):
		entries = [
			frappe._dict(name="I", purpose="Material Transfer for Manufacture", docstatus=1, is_return=0),
			frappe._dict(name="C", purpose="Manufacture", docstatus=1, is_return=0),
		]
		rows = [
			frappe._dict(
				parent="I",
				item_code="RM",
				s_warehouse="S",
				t_warehouse="WIP",
				stock_uom="Nos",
				transfer_qty=3,
				serial_no="A\nB\nC",
			),
			frappe._dict(
				parent="C", item_code="RM", s_warehouse="WIP", stock_uom="Nos", transfer_qty=1, serial_no="B"
			),
		]
		route = aggregate_routes(entries, rows)[0]
		self.assertEqual(stock_rows(route, 2, "S")[0]["serial_no"], "A\nC")
		with self.assertRaises(frappe.ValidationError):
			stock_rows(route, 0.5, "S")
		with self.assertRaises(frappe.ValidationError):
			stock_rows(route, 3, "S")

	def test_closeout_return_preserves_each_detail_source(self):
		entries = [
			frappe._dict(name="I1", purpose="Material Transfer for Manufacture", docstatus=1, is_return=0),
			frappe._dict(name="I2", purpose="Material Transfer for Manufacture", docstatus=1, is_return=0),
			frappe._dict(name="R", purpose="Material Transfer for Manufacture", docstatus=1, is_return=1),
		]
		rows = [
			frappe._dict(
				parent="I1",
				item_code="RM",
				s_warehouse="S1",
				t_warehouse="WIP",
				stock_uom="Kg",
				transfer_qty=4,
			),
			frappe._dict(
				parent="I2",
				item_code="RM",
				s_warehouse="S2",
				t_warehouse="WIP",
				stock_uom="Kg",
				transfer_qty=6,
			),
			frappe._dict(
				parent="R",
				item_code="RM",
				s_warehouse="WIP",
				t_warehouse="Q",
				stock_uom="Kg",
				transfer_qty=2,
				custom_return_source_warehouse="S1",
			),
			frappe._dict(
				parent="R",
				item_code="RM",
				s_warehouse="WIP",
				t_warehouse="Q",
				stock_uom="Kg",
				transfer_qty=3,
				custom_return_source_warehouse="S2",
			),
		]
		self.assertEqual([r.native_available_qty for r in aggregate_routes(entries, rows)], [2, 3])
