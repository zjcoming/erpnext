from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from process_simplification.api.quick_order import validate_no_duplicate_finished_goods
from process_simplification.api.utils import SimplifiedFlowError, WorkbenchRow, remaining_qty
from process_simplification.api.workbench import _remaining_reserved_qty, get_active_work_order_qty


class TestSimplifiedFlow(UnitTestCase):
	def test_quick_order_rejects_duplicate_finished_goods(self):
		items = [
			frappe._dict({"item_code": "FG-001"}),
			frappe._dict({"item_code": "FG-001"}),
		]

		with self.assertRaises(SimplifiedFlowError):
			validate_no_duplicate_finished_goods(items)

	def test_remaining_reserved_qty_ignores_delivered_and_consumed_quantities(self):
		entry = frappe._dict(
			{
				"reserved_qty": 10,
				"delivered_qty": 3,
				"transferred_qty": 2,
				"consumed_qty": 1,
			}
		)

		self.assertEqual(_remaining_reserved_qty(entry), 4)

	def test_remaining_reserved_qty_never_goes_negative(self):
		entry = frappe._dict(
			{
				"reserved_qty": 2,
				"delivered_qty": 3,
				"transferred_qty": 0,
				"consumed_qty": 0,
			}
		)

		self.assertEqual(_remaining_reserved_qty(entry), 0)

	def test_active_work_order_qty_excludes_terminal_statuses(self):
		work_orders = [
			frappe._dict({"qty": 10, "produced_qty": 3, "process_loss_qty": 1, "status": "In Process"}),
			frappe._dict({"qty": 10, "produced_qty": 10, "process_loss_qty": 0, "status": "Completed"}),
			frappe._dict({"qty": 5, "produced_qty": 0, "process_loss_qty": 0, "status": "Closed"}),
		]

		self.assertEqual(get_active_work_order_qty(work_orders), 6)

	def test_remaining_qty_floors_at_zero(self):
		self.assertEqual(remaining_qty(5, 4, 4), 0)

	@patch("process_simplification.api.actions.frappe.has_permission")
	@patch("process_simplification.api.actions._row_from_workbench")
	def test_create_work_order_rechecks_coverage_before_writing(self, row_from_workbench, has_permission):
		from process_simplification.api.actions import create_work_order

		has_permission.return_value = True
		row_from_workbench.return_value = frappe._dict({"uncovered_qty": 0})

		with self.assertRaises(SimplifiedFlowError):
			create_work_order("SO-TEST", "SO-ITEM-TEST")

		row_from_workbench.assert_called_once_with("SO-TEST", "SO-ITEM-TEST")

	@patch("process_simplification.api.actions.frappe.has_permission")
	@patch("process_simplification.api.actions._row_from_workbench")
	def test_create_delivery_note_requires_effective_reservation(self, row_from_workbench, has_permission):
		from process_simplification.api.actions import create_delivery_note

		has_permission.return_value = True
		row_from_workbench.return_value = frappe._dict({"reserved_qty": 0})

		with self.assertRaises(SimplifiedFlowError):
			create_delivery_note("SO-TEST", "SO-ITEM-TEST")

		row_from_workbench.assert_called_once_with("SO-TEST", "SO-ITEM-TEST")

	@patch("process_simplification.api.actions.get_available_qty_to_reserve")
	@patch("process_simplification.api.actions.frappe.get_doc")
	@patch("process_simplification.api.actions.frappe.has_permission")
	@patch("process_simplification.api.actions.get_sales_order_item")
	@patch("process_simplification.api.actions._row_from_workbench")
	def test_reserve_stock_rechecks_availability_before_writing(
		self,
		row_from_workbench,
		get_sales_order_item,
		has_permission,
		get_doc,
		get_available_qty_to_reserve,
	):
		from process_simplification.api.actions import reserve_stock

		has_permission.return_value = True
		get_doc.return_value = frappe._dict({"company": "_Test Company"})
		get_available_qty_to_reserve.return_value = 0
		row_from_workbench.return_value = frappe._dict({"pending_qty": 10, "reserved_qty": 10})
		get_sales_order_item.return_value = frappe._dict({"warehouse": "_Test Warehouse", "item_code": "_Test Item"})

		with self.assertRaises(SimplifiedFlowError):
			reserve_stock("SO-TEST", "SO-ITEM-TEST")

		row_from_workbench.assert_called_once_with("SO-TEST", "SO-ITEM-TEST")

	@patch("process_simplification.api.shortage.get_order_workbench")
	def test_shortage_selection_requires_demand(self, get_order_workbench):
		from process_simplification.api.shortage import check_shortage

		get_order_workbench.return_value = {
			"rows": [
				{
					"sales_order_item": "SO-ITEM-TEST",
					"unsupported": False,
					"uncovered_qty": 0,
					"active_work_order_qty": 0,
					"item_code": "_Test Item",
				}
			]
		}

		result = check_shortage([{"sales_order": "SO-TEST", "sales_order_item": "SO-ITEM-TEST"}], company="_Test Company")
		self.assertEqual(result["shortages"], [])

	@patch("process_simplification.api.shortage.calculate_material_shortages", return_value=[])
	@patch("process_simplification.api.shortage.get_default_bom", return_value="BOM-FG-001")
	@patch("process_simplification.api.shortage.get_order_workbench")
	def test_shortage_demand_carries_the_sales_order_item_warehouse_used_by_work_orders(
		self, get_order_workbench, get_default_bom, calculate_shortages
	):
		from process_simplification.api.shortage import check_shortage

		get_order_workbench.return_value = {
			"rows": [
				{
					"sales_order_item": "SO-ITEM-TEST",
					"unsupported": False,
					"uncovered_qty": 2,
					"active_work_order_qty": 0,
					"item_code": "FG-001",
					"warehouse": "Finished Goods - TC",
				}
			]
		}

		check_shortage(
			[{"sales_order": "SO-TEST", "sales_order_item": "SO-ITEM-TEST"}],
			company="_Test Company",
		)

		demand = calculate_shortages.call_args.args[0][0]
		self.assertEqual(demand["source"]["sales_order_item_warehouse"], "Finished Goods - TC")

	def test_workbench_row_serializes_actions(self):
		row = WorkbenchRow(
			sales_order="SO-TEST",
			sales_order_item="SO-ITEM-TEST",
			customer="_Test Customer",
			item_code="_Test Item",
			item_name="_Test Item",
			warehouse="_Test Warehouse",
			delivery_date=None,
			order_qty=1,
			delivered_qty=0,
			pending_qty=1,
		)

		self.assertEqual(row.as_dict()["sales_order_item"], "SO-ITEM-TEST")

	def test_quick_order_item_defaults_include_price_and_warehouse(self):
		from process_simplification.api.quick_order import get_quick_order_item_defaults

		item_code = "PS FG ITEM"
		warehouse = "Finished Goods - TC"
		with (
			patch("process_simplification.api.quick_order.frappe.has_permission"),
			patch(
				"process_simplification.api.quick_order.frappe.get_cached_value",
				return_value=frappe._dict(
					{
						"item_code": item_code,
						"item_name": item_code,
						"stock_uom": "Nos",
						"is_sales_item": 1,
						"is_stock_item": 1,
						"disabled": 0,
						"has_variants": 0,
						"has_serial_no": 0,
						"has_batch_no": 0,
					}
				),
			),
			patch("process_simplification.api.quick_order.frappe.db.exists", return_value=False),
			patch(
				"process_simplification.api.quick_order.get_company_defaults",
				return_value=frappe._dict({"company": "_Test Company", "fg_warehouse": warehouse}),
			),
			patch(
				"process_simplification.api.quick_order._item_default",
				return_value=frappe._dict(
					{"default_warehouse": warehouse, "default_price_list": "Standard Selling"}
				),
			),
			patch(
				"process_simplification.api.quick_order._item_price",
				return_value=frappe._dict(
					{"price_list": "Standard Selling", "price_list_rate": 99, "currency": "CNY"}
				),
			),
			patch("process_simplification.api.quick_order.get_available_qty_to_reserve", return_value=7),
			patch("process_simplification.api.quick_order.get_default_bom", return_value="BOM-PS-FG-001"),
		):
			defaults = get_quick_order_item_defaults(item_code, "_Test Company")

		self.assertEqual(defaults["warehouse"], warehouse)
		self.assertEqual(defaults["rate"], 99)
		self.assertEqual(defaults["available_to_reserve"], 7)
		self.assertEqual(defaults["stock_uom"], "Nos")
