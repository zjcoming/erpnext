from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from process_simplification.api.quick_order import validate_no_duplicate_finished_goods
from process_simplification.api.utils import SimplifiedFlowError, WorkbenchRow, remaining_qty
from process_simplification.api.workbench import _remaining_reserved_qty, get_active_work_order_qty


class TestSimplifiedFlow(UnitTestCase):
	def test_production_quantities_do_not_duplicate_available_finished_stock(self):
		from process_simplification.api import workbench

		self.assertTrue(
			hasattr(workbench, "calculate_production_quantities"),
			"production quantity calculator must exist",
		)
		result = workbench.calculate_production_quantities(
			pending_qty=100,
			reserved_qty=20,
			available_to_reserve=30,
			active_work_order_qty=40,
		)

		self.assertEqual(result.finished_stock_coverage_qty, 50)
		self.assertEqual(result.production_required_qty, 50)
		self.assertEqual(result.unplanned_production_qty, 10)
		self.assertEqual(result.overplanned_qty, 0)

	def test_overplanned_work_order_is_reported_without_negative_unplanned_qty(self):
		from process_simplification.api import workbench

		self.assertTrue(
			hasattr(workbench, "calculate_production_quantities"),
			"production quantity calculator must exist",
		)
		result = workbench.calculate_production_quantities(
			pending_qty=10,
			reserved_qty=0,
			available_to_reserve=0,
			active_work_order_qty=15,
		)

		self.assertEqual(result.finished_stock_coverage_qty, 0)
		self.assertEqual(result.production_required_qty, 10)
		self.assertEqual(result.unplanned_production_qty, 0)
		self.assertEqual(result.overplanned_qty, 5)

	def test_direct_stock_order_is_included_and_marked_ready_to_ship(self):
		from process_simplification.api.workbench import build_fulfillment_order

		order = frappe._dict(name="SO-READY", customer="C1", customer_name="C1", creation="2026-08-01")
		rows = [
			{
				"pending_qty": 10,
				"reserved_qty": 10,
				"uncovered_qty": 0,
				"active_work_order_qty": 0,
				"delivered_qty": 0,
				"order_qty": 10,
				"delivery_date": "2026-08-06",
				"next_actions": [],
			}
		]

		result = build_fulfillment_order(order, rows, today="2026-08-02")

		self.assertEqual(result["status_code"], "ready_to_ship")
		self.assertTrue(result["direct_ship"])
		self.assertEqual(result["risk_level"], "green")

	def test_fulfillment_order_uses_only_pending_rows_for_delivery_and_coverage(self):
		from process_simplification.api.workbench import build_fulfillment_order

		order = frappe._dict(name="SO-MIXED", customer="C1", customer_name="Customer 1", creation="2026-08-01")
		rows = [
			{
				"pending_qty": 0,
				"reserved_qty": 99,
				"active_work_order_qty": 0,
				"completed_qty": 9,
				"delivered_qty": 9,
				"order_qty": 9,
				"delivery_date": "2026-08-03",
				"next_actions": [],
			},
			{
				"pending_qty": 5,
				"reserved_qty": 8,
				"active_work_order_qty": 0,
				"completed_qty": 4,
				"delivered_qty": 0,
				"order_qty": 5,
				"delivery_date": "2026-08-08",
				"next_actions": [],
			},
			{
				"pending_qty": 2,
				"reserved_qty": 0,
				"active_work_order_qty": 0,
				"completed_qty": 2,
				"delivered_qty": 0,
				"order_qty": 2,
				"delivery_date": "2026-08-04",
				"next_actions": [{"action": "create_work_order"}],
			},
		]

		result = build_fulfillment_order(order, rows, today="2026-08-02")

		self.assertEqual(result["delivery_date"], "2026-08-04")
		self.assertTrue(result["has_multiple_delivery_dates"])
		self.assertEqual(result["item_count"], 3)
		self.assertEqual(result["order_qty"], 16)
		self.assertEqual(result["delivered_qty"], 9)
		self.assertEqual(result["pending_qty"], 7)
		self.assertEqual(result["reserved_qty"], 5)
		self.assertEqual(result["uncovered_qty"], 2)
		self.assertTrue(result["needs_production"])
		self.assertFalse(result["direct_ship"])

	def test_fulfillment_order_marks_missing_pending_delivery_date(self):
		from process_simplification.api.workbench import build_fulfillment_order

		result = build_fulfillment_order(
			frappe._dict(name="SO-NO-DATE", creation="2026-08-01"),
			[
				{
					"pending_qty": 1,
					"reserved_qty": 1,
					"active_work_order_qty": 0,
					"delivery_date": None,
					"next_actions": [],
				}
			],
			today="2026-08-02",
		)

		self.assertEqual(result["delivery_timing"], "missing")
		self.assertIsNone(result["days_to_delivery"])
		self.assertEqual(result["risk_level"], "orange")
		self.assertGreater(result["risk_score"], 0)

	def test_fulfillment_risk_follows_the_approved_priority_hierarchy(self):
		from process_simplification.api.workbench import build_fulfillment_order

		base_row = {
			"pending_qty": 10,
			"reserved_qty": 0,
			"active_work_order_qty": 0,
			"delivery_date": "2026-08-20",
			"material_status": "未检查",
			"next_actions": [],
		}
		cases = [
			(
				"overdue overrides unsupported",
				{**base_row, "delivery_date": "2026-08-01", "unsupported": True},
				("red", 100, "已逾期"),
			),
			(
				"unsupported simplified action",
				{**base_row, "reserved_qty": 10, "unsupported": True},
				("orange", 90, "简化操作不支持"),
			),
			(
				"missing production base data",
				{**base_row, "material_status": "不涉及生产"},
				("orange", 90, "缺少生产基础资料"),
			),
			(
				"missing delivery date",
				{**base_row, "delivery_date": None, "reserved_qty": 10},
				("orange", 85, "缺少交期"),
			),
			(
				"due soon uncovered production",
				{
					**base_row,
					"delivery_date": "2026-08-07",
					"next_actions": [{"action": "create_work_order"}],
				},
				("orange", 80, "临期生产未覆盖"),
			),
			(
				"later uncovered production",
				{**base_row, "next_actions": [{"action": "create_work_order"}]},
				("orange", 70, "生产未覆盖"),
			),
			(
				"active production",
				{**base_row, "active_work_order_qty": 10},
				("blue", 60, "生产中"),
			),
			(
				"partial stock with no work order remains production uncovered",
				{**base_row, "reserved_qty": 5},
				("orange", 70, "生产未覆盖"),
			),
			(
				"ready to ship",
				{**base_row, "reserved_qty": 10},
				("green", 20, "可发货"),
			),
		]

		for name, row, expected in cases:
			with self.subTest(name=name):
				result = build_fulfillment_order(
					frappe._dict(name="SO-RISK", creation="2026-08-01"),
					[row],
					today="2026-08-02",
				)

				self.assertEqual(
					(result["risk_level"], result["risk_score"], result["risk_label"]), expected
				)

	@patch("process_simplification.api.workbench.now_datetime")
	@patch("process_simplification.api.workbench.get_order_workbench")
	@patch("process_simplification.api.workbench.frappe.get_list")
	@patch("process_simplification.api.workbench.frappe.has_permission")
	def test_fulfillment_overview_sorts_orders_and_counts_orders_not_items(
		self, has_permission, get_list, get_order_workbench, now_datetime
	):
		from process_simplification.api.workbench import get_fulfillment_overview

		has_permission.return_value = True
		now_datetime.return_value = frappe.utils.get_datetime("2026-08-02 09:00:00")
		get_list.return_value = [
			frappe._dict(name="SO-LATER", customer="C1", customer_name="C1", creation="2026-08-01"),
			frappe._dict(name="SO-GREEN", customer="C1", customer_name="C1", creation="2026-08-02"),
			frappe._dict(name="SO-BLOCKED", customer="C1", customer_name="C1", creation="2026-08-03"),
			frappe._dict(name="SO-OVERDUE", customer="C1", customer_name="C1", creation="2026-08-04"),
			frappe._dict(name="SO-TODAY", customer="C1", customer_name="C1", creation="2026-08-05"),
			frappe._dict(name="SO-MISSING", customer="C1", customer_name="C1", creation="2026-08-05"),
		]
		get_order_workbench.side_effect = lambda name: {
			"rows": {
				"SO-LATER": [
					{"pending_qty": 2, "reserved_qty": 2, "delivery_date": "2026-08-08", "next_actions": []}
				],
				"SO-GREEN": [
					{"pending_qty": 3, "reserved_qty": 3, "delivery_date": "2026-08-04", "next_actions": []}
				],
				"SO-BLOCKED": [
					{
						"pending_qty": 1,
						"reserved_qty": 0,
						"delivery_date": "2026-08-04",
						"unsupported": True,
						"next_actions": [],
					},
					{
						"pending_qty": 4,
						"reserved_qty": 0,
						"delivery_date": "2026-08-04",
						"unsupported": True,
						"next_actions": [],
					},
				],
				"SO-OVERDUE": [
					{"pending_qty": 1, "reserved_qty": 1, "delivery_date": "2026-08-01", "next_actions": []}
				],
				"SO-TODAY": [
					{"pending_qty": 1, "reserved_qty": 1, "delivery_date": "2026-08-02", "next_actions": []}
				],
				"SO-MISSING": [{"pending_qty": 1, "reserved_qty": 1, "delivery_date": None, "next_actions": []}],
			}[name]
		}

		result = get_fulfillment_overview(page_size=0)

		self.assertEqual(
			[order["name"] for order in result["orders"]],
			["SO-OVERDUE", "SO-TODAY", "SO-BLOCKED", "SO-GREEN", "SO-LATER", "SO-MISSING"],
		)
		self.assertEqual(
			result["summary"],
			{
				"total_orders": 6,
				"overdue_orders": 1,
				"due_within_7_days": 4,
				"needs_production_orders": 1,
				"direct_ship_orders": 5,
			},
		)
		self.assertEqual(get_list.call_args.kwargs["limit_start"], 0)
		self.assertEqual(get_list.call_args.kwargs["limit_page_length"], 500)

	@patch("process_simplification.api.workbench.now_datetime")
	@patch("process_simplification.api.workbench.get_order_workbench")
	@patch("process_simplification.api.workbench.frappe.get_list")
	@patch("process_simplification.api.workbench.frappe.has_permission")
	def test_fulfillment_overview_fetches_a_second_deterministically_ordered_page(
		self, has_permission, get_list, get_order_workbench, now_datetime
	):
		from process_simplification.api.workbench import get_fulfillment_overview

		has_permission.return_value = True
		now_datetime.return_value = frappe.utils.get_datetime("2026-08-02 09:00:00")
		first_page = [
			frappe._dict(name=f"SO-{index:04d}", creation=f"2026-08-01 00:{index % 60:02d}:00")
			for index in range(500)
		]
		get_list.side_effect = [first_page, [frappe._dict(name="SO-0500", creation="2026-08-02")]]
		get_order_workbench.return_value = {
			"rows": [{"pending_qty": 1, "reserved_qty": 1, "delivery_date": "2026-08-08", "next_actions": []}]
		}

		result = get_fulfillment_overview(page_size=0)

		self.assertEqual(len(result["orders"]), 501)
		self.assertEqual(get_list.call_count, 2)
		self.assertEqual(
			[(call.kwargs["limit_start"], call.kwargs["limit_page_length"]) for call in get_list.call_args_list],
			[(0, 500), (500, 500)],
		)
		self.assertTrue(
			all(call.kwargs.get("order_by") == "creation asc, name asc" for call in get_list.call_args_list)
		)

	@patch("process_simplification.api.workbench.now_datetime")
	@patch("process_simplification.api.workbench.get_order_workbench")
	@patch("process_simplification.api.workbench.frappe.get_list")
	@patch("process_simplification.api.workbench.frappe.has_permission")
	def test_fulfillment_overview_returns_requested_page_with_global_summary(
		self, has_permission, get_list, get_order_workbench, now_datetime
	):
		from process_simplification.api.workbench import get_fulfillment_overview

		has_permission.return_value = True
		now_datetime.return_value = frappe.utils.get_datetime("2026-08-02 09:00:00")
		get_list.return_value = [
			frappe._dict(name=f"SO-{index}", customer="C1", customer_name="C1", creation=f"2026-08-0{index}")
			for index in range(1, 6)
		]
		get_order_workbench.return_value = {
			"rows": [{"pending_qty": 1, "reserved_qty": 1, "delivery_date": "2026-08-08", "next_actions": []}]
		}

		result = get_fulfillment_overview(page=2, page_size=2)

		self.assertEqual([order["name"] for order in result["orders"]], ["SO-3", "SO-4"])
		self.assertEqual(result["summary"]["total_orders"], 5)
		self.assertEqual(
			result["pagination"],
			{
				"page": 2,
				"page_size": 2,
				"total_count": 5,
				"total_pages": 3,
				"has_next": True,
				"has_prev": True,
			},
		)

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

	@patch("process_simplification.api.actions.get_allocated_production_row")
	@patch("process_simplification.api.actions.frappe.has_permission")
	@patch("process_simplification.api.actions._row_from_workbench")
	def test_create_work_order_rechecks_cross_order_finished_stock_allocation(
		self,
		row_from_workbench,
		has_permission,
		get_allocated_production_row,
	):
		from process_simplification.api.actions import create_work_order

		has_permission.return_value = True
		row_from_workbench.return_value = frappe._dict({"unplanned_production_qty": 5})
		get_allocated_production_row.return_value = None

		with self.assertRaises(SimplifiedFlowError):
			create_work_order("SO-LATE", "SO-LATE-ITEM")

		get_allocated_production_row.assert_called_once_with("SO-LATE", "SO-LATE-ITEM")

	@patch("process_simplification.api.actions.frappe.has_permission")
	@patch("process_simplification.api.actions._row_from_workbench")
	def test_create_delivery_note_requires_effective_reservation(self, row_from_workbench, has_permission):
		from process_simplification.api.actions import create_delivery_note

		has_permission.return_value = True
		row_from_workbench.return_value = frappe._dict({"reserved_qty": 0})

		with self.assertRaises(SimplifiedFlowError):
			create_delivery_note("SO-TEST", "SO-ITEM-TEST")

		row_from_workbench.assert_called_once_with("SO-TEST", "SO-ITEM-TEST")

	@patch("process_simplification.api.actions.make_delivery_note")
	@patch("process_simplification.api.actions.get_sales_order_item")
	@patch("process_simplification.api.actions.frappe.get_doc")
	@patch("process_simplification.api.actions.frappe.get_all")
	@patch("process_simplification.api.actions.frappe.db.get_value")
	@patch("process_simplification.api.actions.frappe.has_permission")
	@patch("process_simplification.api.actions._row_from_workbench")
	def test_create_delivery_note_reuses_existing_draft_for_the_same_order_item(
		self,
		row_from_workbench,
		has_permission,
		get_value,
		get_all,
		get_doc,
		get_sales_order_item,
		make_delivery_note,
	):
		from process_simplification.api.actions import create_delivery_note

		has_permission.return_value = True
		get_value.return_value = "SO-ITEM-TEST"
		# A draft Delivery Note can consume the effective reservation shown by the workbench.
		# Retrying the action must still reopen that draft instead of reporting no reservation.
		row_from_workbench.return_value = frappe._dict({"reserved_qty": 0})
		get_all.return_value = [frappe._dict({"parent": "DN-DRAFT-001"})]
		existing_draft = MagicMock(name="existing_delivery_note")
		existing_draft.name = "DN-DRAFT-001"
		existing_draft.docstatus = 0
		get_doc.return_value = existing_draft
		get_sales_order_item.return_value = frappe._dict({"conversion_factor": 1})
		new_delivery_note = MagicMock(name="new_delivery_note")
		new_delivery_note.name = "DN-NEW-001"
		new_delivery_note.docstatus = 0
		new_delivery_note.items = [frappe._dict({"so_detail": "SO-ITEM-TEST", "qty": 5})]
		make_delivery_note.return_value = new_delivery_note

		result = create_delivery_note("SO-TEST", "SO-ITEM-TEST")

		self.assertEqual(
			result,
			{"delivery_note": "DN-DRAFT-001", "docstatus": 0, "reused": True},
		)

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
