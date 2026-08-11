import frappe
from frappe.tests import UnitTestCase
from unittest.mock import patch


class TestProductionWorkbench(UnitTestCase):
	def _module(self):
		from process_simplification.api import production

		return production

	def _order(self, name="SO-001", delivery_date="2026-08-08", creation="2026-08-01"):
		return frappe._dict(
			name=name,
			customer="CUST-001",
			customer_name="测试客户",
			company="_Test Company",
			delivery_date=delivery_date,
			creation=creation,
		)

	def _row(self, name="SOI-001", **overrides):
		row = {
			"sales_order": "SO-001",
			"sales_order_item": name,
			"item_code": "FG-001",
			"item_name": "成品 001",
			"warehouse": "Finished Goods - TC",
			"delivery_date": "2026-08-08",
			"pending_qty": 100,
			"reserved_qty": 20,
			"available_to_reserve": 30,
			"finished_stock_coverage_qty": 50,
			"production_required_qty": 50,
			"active_work_order_qty": 40,
			"unplanned_production_qty": 10,
			"overplanned_qty": 0,
			"completed_qty": 0,
			"completed_unreserved_qty": 0,
			"material_status": "未检查",
			"unsupported": False,
		}
		row.update(overrides)
		return frappe._dict(row)

	def test_unplanned_demand_is_visible_without_a_work_order(self):
		production = self._module()
		self.assertTrue(hasattr(production, "build_production_demand"))

		demand = production.build_production_demand(
			self._order(),
			self._row(active_work_order_qty=0, unplanned_production_qty=50),
			work_orders=[],
			today="2026-08-02",
		)

		self.assertEqual(demand["status_code"], "unplanned")
		self.assertEqual(demand["unplanned_production_qty"], 50)
		self.assertEqual(demand["sales_order_item"], "SOI-001")

	def test_stock_only_row_is_excluded_from_production_overview(self):
		production = self._module()
		self.assertTrue(hasattr(production, "build_production_demand"))

		demand = production.build_production_demand(
			self._order(),
			self._row(
				finished_stock_coverage_qty=100,
				production_required_qty=0,
				active_work_order_qty=0,
				unplanned_production_qty=0,
			),
			work_orders=[],
			today="2026-08-02",
		)

		self.assertIsNone(demand)

	def test_finished_stock_is_allocated_once_in_delivery_priority_order(self):
		production = self._module()
		self.assertTrue(hasattr(production, "allocate_finished_stock"))
		rows = [
			self._row(
				name="SOI-EARLY",
				sales_order="SO-EARLY",
				delivery_date="2026-08-04",
				pending_qty=40,
				reserved_qty=0,
				available_to_reserve=50,
				active_work_order_qty=0,
			),
			self._row(
				name="SOI-LATE",
				sales_order="SO-LATE",
				delivery_date="2026-08-10",
				pending_qty=40,
				reserved_qty=0,
				available_to_reserve=50,
				active_work_order_qty=0,
			),
		]

		allocated = production.allocate_finished_stock(rows)

		self.assertEqual(allocated[0]["available_to_reserve"], 40)
		self.assertEqual(allocated[0]["production_required_qty"], 0)
		self.assertEqual(allocated[1]["available_to_reserve"], 10)
		self.assertEqual(allocated[1]["production_required_qty"], 30)
		self.assertEqual(allocated[1]["unplanned_production_qty"], 30)

	def test_material_coverage_is_projected_to_each_contributing_demand(self):
		production = self._module()
		self.assertTrue(hasattr(production, "attach_material_coverage"))
		demands = [
			{
				"demand_key": "SOI-1",
				"status_code": "in_production",
				"delivery_timing": "later",
				"materials": [],
				"next_actions": [],
			},
			{
				"demand_key": "SOI-2",
				"status_code": "unplanned",
				"delivery_timing": "later",
				"materials": [],
				"next_actions": [],
			},
		]
		coverage = {
			"materials": [
				{
					"item_code": "RM-001",
					"item_name": "原料 001",
					"stock_uom": "Nos",
					"warehouse": "Stores - TC",
					"required_qty": 30,
					"actual_qty": 10,
					"committed_qty": 0,
					"available_qty": 10,
					"open_material_request_qty": 5,
					"open_purchase_order_qty": 5,
					"current_gap_qty": 20,
					"shortage_qty": 10,
					"status": "new_purchase_required",
					"blocked": False,
					"sources": [
						{"demand_key": "SOI-1", "required_qty": 20},
						{"demand_key": "SOI-2", "required_qty": 10},
					],
				}
			]
		}

		result = production.attach_material_coverage(demands, coverage)

		first = result[0]
		second = result[1]
		self.assertEqual(first["materials"][0]["source_required_qty"], 20)
		self.assertEqual(second["materials"][0]["source_required_qty"], 10)
		self.assertEqual(first["materials"][0]["total_required_qty"], 30)
		self.assertTrue(first["materials"][0]["is_shared"])
		self.assertEqual(first["material_summary"]["shortage_item_count"], 1)
		self.assertEqual(first["status_code"], "material_shortage")
		self.assertEqual(second["status_code"], "unplanned")

	def test_missing_delivery_date_sorts_before_dated_demands_as_a_data_risk(self):
		production = self._module()
		self.assertTrue(hasattr(production, "production_sort_key"))
		demands = [
			{
				"demand_key": "DATED",
				"delivery_date": "2026-08-03",
				"risk_score": 100,
				"unplanned_production_qty": 10,
				"creation": "2026-08-01",
			},
			{
				"demand_key": "MISSING",
				"delivery_date": None,
				"risk_score": 90,
				"unplanned_production_qty": 1,
				"creation": "2026-08-02",
			},
		]

		ordered = sorted(demands, key=production.production_sort_key)

		self.assertEqual([row["demand_key"] for row in ordered], ["MISSING", "DATED"])

	@patch("process_simplification.api.production._other_work_orders")
	@patch("process_simplification.api.production.get_default_bom")
	@patch("process_simplification.api.production.get_work_orders")
	@patch("process_simplification.api.production.get_fulfillment_overview")
	@patch("process_simplification.api.production.frappe.has_permission")
	@patch("process_simplification.api.production.now_datetime")
	def test_production_overview_returns_requested_page_with_global_summary(
		self, now_datetime, has_permission, get_fulfillment_overview, get_work_orders, get_default_bom, other_work_orders
	):
		production = self._module()
		has_permission.return_value = True
		now_datetime.return_value = frappe.utils.get_datetime("2026-08-02 09:00:00")
		get_work_orders.return_value = []
		get_default_bom.return_value = None
		other_work_orders.return_value = []
		get_fulfillment_overview.return_value = {
			"orders": [
				{
					"name": f"SO-{index}",
					"customer": "CUST-001",
					"customer_name": "测试客户",
					"company": "_Test Company",
					"creation": f"2026-08-0{index}",
					"rows": [
						self._row(
							name=f"SOI-{index}",
							sales_order=f"SO-{index}",
							item_code=f"FG-{index}",
							delivery_date=f"2026-08-0{index}",
							production_required_qty=10,
							unplanned_production_qty=10,
						)
					],
				}
				for index in range(1, 6)
			]
		}

		result = production.get_production_overview(page=2, page_size=2)

		self.assertEqual([demand["demand_key"] for demand in result["demands"]], ["SOI-3", "SOI-4"])
		self.assertEqual(result["summary"]["total_demands"], 5)
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
