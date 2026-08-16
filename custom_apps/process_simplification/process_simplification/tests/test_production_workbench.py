from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase


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

	def _priority_demand(self, demand_key, delivery_date, creation):
		return {
			"demand_key": demand_key,
			"sales_order": f"SO-{demand_key}",
			"sales_order_item": demand_key,
			"item_code": f"FG-{demand_key}",
			"warehouse": "Finished Goods - TC",
			"delivery_date": delivery_date,
			"creation": creation,
			"production_required_qty": 1,
			"status_code": "ready_to_start",
			"delivery_timing": "later",
			"next_actions": [],
		}

	def _attach_priority_material_coverage(
		self,
		demands,
		*,
		stock_qty=10,
		po_docs=None,
		mr_docs=None,
		warehouse_can_use=True,
		stock_can_calculate=True,
	):
		from process_simplification.api import production
		po_docs = po_docs or []
		mr_docs = mr_docs or []

		def bom_items(_bom_no, _company, qty, fetch_exploded):
			return {
				"RM-SHARED": frappe._dict(
					{
						"item_code": "RM-SHARED",
						"item_name": "共享原料",
						"stock_uom": "Nos",
						"qty": 10 * qty,
					}
				)
			}

		with (
			patch.object(production, "get_default_bom", return_value="BOM-FG"),
			patch(
				"process_simplification.api.shortage.resolve_production_source_warehouse",
				return_value=frappe._dict(
					{"warehouse": "Stores - TC", "can_use": warehouse_can_use, "reason": None}
				),
			),
			patch("process_simplification.api.shortage.get_bom_items_as_dict", side_effect=bom_items),
			patch(
				"process_simplification.api.shortage.get_material_stock_snapshot",
				return_value=frappe._dict(
					{
						"can_calculate": stock_can_calculate,
						"actual_qty": stock_qty,
						"committed_qty": 0,
						"available_qty": stock_qty,
					}
				),
			),
			patch(
				"process_simplification.api.shortage._mr_documents",
				side_effect=lambda _item, _warehouse, _company, need_by_date: [
					{**document, "is_late": bool(need_by_date and document.get("schedule_date") > need_by_date)}
					for document in mr_docs
				],
			),
			patch(
				"process_simplification.api.shortage._po_documents",
				side_effect=lambda _item, _warehouse, _company, need_by_date: [
					{**document, "is_late": bool(need_by_date and document.get("schedule_date") > need_by_date)}
					for document in po_docs
				],
			),
		):
			return production.attach_priority_material_coverage(demands, "_Test Company")

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

	def test_material_demands_merge_unplanned_bom_with_pending_work_order_items(self):
		production = self._module()
		demand = {
			"demand_key": "SOI-001",
			"sales_order": "SO-001",
			"sales_order_item": "SOI-001",
			"item_code": "FG-001",
			"warehouse": "Finished Goods - TC",
			"delivery_date": "2026-08-08",
			"production_required_qty": 10,
			"unplanned_production_qty": 3,
			"work_order_materials_loaded": True,
			"work_orders": [
				{
					"name": "WO-001",
					"pending_materials": [
						{
							"item_code": "RM-WO",
							"item_name": "工单原料",
							"stock_uom": "Nos",
							"warehouse": "WO Stores - TC",
							"qty": 7,
						}
					],
				}
			],
		}

		with patch.object(production, "get_default_bom", return_value="BOM-FG-001"):
			rows = production._material_demands([demand])

		self.assertEqual(len(rows), 2)
		self.assertEqual(rows[0]["bom_no"], "BOM-FG-001")
		self.assertEqual(rows[0]["qty"], 3)
		self.assertEqual(rows[1]["materials"][0]["item_code"], "RM-WO")
		self.assertEqual(rows[1]["materials"][0]["warehouse"], "WO Stores - TC")
		self.assertEqual(rows[1]["materials"][0]["qty"], 7)
		self.assertEqual(rows[1]["source"]["work_order"], "WO-001")

	def test_pending_work_order_materials_use_required_minus_transferred(self):
		production = self._module()
		demands = [
			{
				"active_work_order_qty": 8,
				"work_orders": [{"name": "WO-001", "status": "Not Started"}],
			}
		]
		required_items = [
			frappe._dict(
				parent="WO-001",
				item_code="RM-001",
				item_name="原料 001",
				stock_uom="Nos",
				source_warehouse="Execution Stores - TC",
				required_qty=10,
				transferred_qty=4,
			),
			frappe._dict(
				parent="WO-001",
				item_code="RM-DONE",
				item_name="已转原料",
				stock_uom="Nos",
				source_warehouse="Execution Stores - TC",
				required_qty=5,
				transferred_qty=5,
			),
		]

		with patch.object(production.frappe, "get_all", return_value=required_items):
			production._attach_pending_work_order_materials(demands)

		self.assertTrue(demands[0]["work_order_materials_loaded"])
		self.assertEqual(
			demands[0]["work_orders"][0]["pending_materials"],
			[
				{
					"item_code": "RM-001",
					"item_name": "原料 001",
					"stock_uom": "Nos",
					"warehouse": "Execution Stores - TC",
					"qty": 6.0,
				}
			],
		)

	def test_direct_work_order_material_coverage_uses_exact_source_warehouse(self):
		from process_simplification.api import shortage

		with (
			patch.object(shortage, "get_company_defaults", return_value=frappe._dict({"company": "_Test Company"})),
			patch.object(shortage, "get_bom_items_as_dict") as bom_query,
			patch.object(
				shortage,
				"get_material_stock_snapshot",
				return_value=frappe._dict(
					{"can_calculate": True, "actual_qty": 2, "committed_qty": 0, "available_qty": 2}
				),
			) as stock_query,
			patch.object(shortage, "_mr_documents", return_value=[]),
			patch.object(shortage, "_po_documents", return_value=[]),
		):
			coverage = shortage.calculate_material_coverage(
				[
					{
						"materials": [
							{
								"item_code": "RM-WO",
								"item_name": "工单原料",
								"stock_uom": "Nos",
								"warehouse": "Execution Stores - TC",
								"qty": 5,
							}
						],
						"source": {"demand_key": "SOI-001", "work_order": "WO-001"},
					}
				],
				"_Test Company",
			)

		bom_query.assert_not_called()
		stock_query.assert_called_once_with("RM-WO", "Execution Stores - TC")
		self.assertEqual(coverage["materials"][0]["required_qty"], 5)
		self.assertEqual(coverage["materials"][0]["current_gap_qty"], 3)
		self.assertEqual(coverage["materials"][0]["status"], "new_purchase_required")

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
		self.assertEqual(first["status_code"], "in_production")
		self.assertEqual(second["status_code"], "unplanned")

	def test_overview_allocates_shared_material_by_delivery_priority_while_purchasing_stays_aggregate(self):
		from process_simplification.api.shortage import calculate_material_shortages

		production = self._module()
		early_row = self._row(
			name="SOI-EARLY",
			sales_order="SO-EARLY",
			item_code="FG-EARLY",
			delivery_date="2099-08-04",
			pending_qty=1,
			reserved_qty=0,
			available_to_reserve=0,
			active_work_order_qty=1,
		)
		late_row = self._row(
			name="SOI-LATE",
			sales_order="SO-LATE",
			item_code="FG-LATE",
			delivery_date="2099-08-10",
			pending_qty=1,
			reserved_qty=0,
			available_to_reserve=0,
			active_work_order_qty=1,
		)
		fulfillment = {
			"orders": [
				{**self._order("SO-EARLY", "2099-08-04", "2099-08-01"), "rows": [early_row]},
				{**self._order("SO-LATE", "2099-08-10", "2099-08-02"), "rows": [late_row]},
			]
		}

		def bom_items(_bom_no, _company, qty, fetch_exploded):
			return {
				"RM-SHARED": frappe._dict(
					{
						"item_code": "RM-SHARED",
						"item_name": "共享原料",
						"stock_uom": "Nos",
						"qty": 10 * qty,
					}
				)
			}

		with (
			patch.object(production, "get_fulfillment_overview", return_value=fulfillment),
			patch.object(production, "get_work_orders", return_value=[]),
			patch.object(production, "get_default_bom", return_value="BOM-FG"),
			patch.object(production, "_other_work_orders", return_value=[]),
			patch(
				"process_simplification.api.shortage.get_company_defaults",
				return_value=frappe._dict({"company": "_Test Company"}),
			),
			patch(
				"process_simplification.api.shortage.resolve_production_source_warehouse",
				return_value=frappe._dict(
					{"warehouse": "Stores - TC", "can_use": True, "reason": None}
				),
			),
			patch("process_simplification.api.shortage.get_bom_items_as_dict", side_effect=bom_items),
			patch(
				"process_simplification.api.shortage.get_material_stock_snapshot",
				return_value=frappe._dict(
					{"can_calculate": True, "actual_qty": 10, "committed_qty": 0, "available_qty": 10}
				),
			),
			patch("process_simplification.api.shortage._mr_documents", return_value=[]),
			patch("process_simplification.api.shortage._po_documents", return_value=[]),
		):
			overview = production.get_production_overview()
			shortages = calculate_material_shortages(
				production._material_demands(overview["demands"]), "_Test Company"
			)

		by_key = {demand["demand_key"]: demand for demand in overview["demands"]}
		self.assertEqual(by_key["SOI-EARLY"]["materials"][0]["shortage_qty"], 0)
		self.assertEqual(by_key["SOI-LATE"]["materials"][0]["shortage_qty"], 10)
		self.assertEqual(by_key["SOI-EARLY"]["materials"][0]["total_required_qty"], 20)
		self.assertEqual(by_key["SOI-LATE"]["materials"][0]["total_required_qty"], 20)
		self.assertEqual([(row["required_qty"], row["shortage_qty"]) for row in shortages], [(20, 10)])

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

	def test_material_priority_allocates_shared_stock_to_the_earliest_delivery(self):
		late = self._priority_demand("LATE", "2026-08-10", "2026-08-02 09:00:00")
		early = self._priority_demand("EARLY", "2026-08-04", "2026-08-03 09:00:00")

		result = self._attach_priority_material_coverage([late, early])

		by_key = {row["demand_key"]: row for row in result}
		self.assertEqual(by_key["EARLY"]["materials"][0]["current_gap_qty"], 0)
		self.assertEqual(by_key["LATE"]["materials"][0]["current_gap_qty"], 10)
		self.assertEqual(by_key["EARLY"]["materials"][0]["total_required_qty"], 20)
		self.assertEqual(by_key["LATE"]["materials"][0]["source_count"], 2)
		self.assertTrue(by_key["EARLY"]["materials"][0]["is_shared"])

	def test_unusable_source_warehouse_blocks_pre_start_demand(self):
		demand = self._priority_demand(
			"WAREHOUSE-BLOCKED", "2026-08-04", "2026-08-01 09:00:00"
		)

		result = self._attach_priority_material_coverage([demand], warehouse_can_use=False)

		self.assertEqual(result[0]["material_summary"]["status_code"], "blocked")
		self.assertEqual(result[0]["status_code"], "master_data_blocked")
		self.assertEqual(result[0]["status_label"], "基础资料异常")

	def test_uncalculable_stock_snapshot_blocks_pre_start_demand(self):
		demand = self._priority_demand("STOCK-BLOCKED", "2026-08-04", "2026-08-01 09:00:00")

		result = self._attach_priority_material_coverage([demand], stock_can_calculate=False)

		self.assertEqual(result[0]["material_summary"]["status_code"], "blocked")
		self.assertEqual(result[0]["status_code"], "master_data_blocked")

	def test_blocked_material_preserves_active_production_workflow_state(self):
		demand = self._priority_demand("ACTIVE-BLOCKED", "2026-08-04", "2026-08-01 09:00:00")
		demand["status_code"] = "in_production"

		result = self._attach_priority_material_coverage([demand], warehouse_can_use=False)

		self.assertEqual(result[0]["material_summary"]["status_code"], "blocked")
		self.assertEqual(result[0]["status_code"], "in_production")

	def test_priority_allocation_expands_once_and_caches_shared_material_facts(self):
		from process_simplification.api import production

		demands = [
			self._priority_demand(f"DEMAND-{index}", f"2026-08-{index + 1:02d}", f"2026-07-{index + 1:02d}")
			for index in range(1, 5)
		]
		po_docs = [
			{
				"doctype": "Purchase Order",
				"name": "PO-CACHED",
				"detail_name": "PO-CACHED-1",
				"status": "To Receive",
				"outstanding_qty": 10,
				"schedule_date": "2026-08-01",
				"is_late": False,
			}
		]

		def bom_items(_bom_no, _company, qty, fetch_exploded):
			return {
				"RM-SHARED": frappe._dict(
					{
						"item_code": "RM-SHARED",
						"item_name": "共享原料",
						"stock_uom": "Nos",
						"qty": 10 * qty,
					}
				)
			}

		with (
			patch.object(production, "get_default_bom", return_value="BOM-FG"),
			patch(
				"process_simplification.api.shortage.get_company_defaults",
				return_value=frappe._dict({"company": "_Test Company"}),
			) as defaults_query,
			patch(
				"process_simplification.api.shortage.resolve_production_source_warehouse",
				return_value=frappe._dict({"warehouse": "Stores - TC", "can_use": True, "reason": None}),
			),
			patch(
				"process_simplification.api.shortage.get_bom_items_as_dict", side_effect=bom_items
			) as bom_query,
			patch(
				"process_simplification.api.shortage.get_material_stock_snapshot",
				return_value=frappe._dict(
					{"can_calculate": True, "actual_qty": 10, "committed_qty": 0, "available_qty": 10}
				),
			) as stock_query,
			patch("process_simplification.api.shortage._mr_documents", return_value=[]) as mr_query,
			patch(
				"process_simplification.api.shortage._po_documents", return_value=po_docs
			) as po_query,
		):
			result = production.attach_priority_material_coverage(demands, "_Test Company")

		self.assertEqual([row["materials"][0]["current_gap_qty"] for row in result], [0, 10, 10, 10])
		self.assertEqual(defaults_query.call_count, 1)
		self.assertEqual(bom_query.call_count, len(demands))
		self.assertEqual(stock_query.call_count, 1)
		self.assertEqual(mr_query.call_count, 1)
		self.assertEqual(po_query.call_count, 1)

	def test_material_priority_uses_older_creation_when_delivery_dates_match(self):
		newer = self._priority_demand("NEWER", "2026-08-04", "2026-08-03 09:00:00")
		older = self._priority_demand("OLDER", "2026-08-04", "2026-08-01 09:00:00")

		result = self._attach_priority_material_coverage([newer, older])

		by_key = {row["demand_key"]: row for row in result}
		self.assertEqual(by_key["OLDER"]["materials"][0]["current_gap_qty"], 0)
		self.assertEqual(by_key["NEWER"]["materials"][0]["current_gap_qty"], 10)

	def test_material_priority_allocates_stock_to_dated_demand_before_undated_data_risk(self):
		undated = self._priority_demand("UNDATED", None, "2026-08-01 09:00:00")
		dated = self._priority_demand("DATED", "2026-08-04", "2026-08-03 09:00:00")

		result = self._attach_priority_material_coverage([undated, dated])

		by_key = {row["demand_key"]: row for row in result}
		self.assertEqual(by_key["DATED"]["materials"][0]["current_gap_qty"], 0)
		self.assertEqual(by_key["UNDATED"]["materials"][0]["current_gap_qty"], 10)

	def test_material_priority_allocates_shared_purchase_order_only_once(self):
		early = self._priority_demand("EARLY", "2026-08-04", "2026-08-01 09:00:00")
		late = self._priority_demand("LATE", "2026-08-10", "2026-08-02 09:00:00")

		result = self._attach_priority_material_coverage(
			[late, early],
			stock_qty=0,
			po_docs=[
				{
					"doctype": "Purchase Order",
					"name": "PO-SHARED",
					"status": "To Receive",
					"outstanding_qty": 10,
					"schedule_date": "2026-08-03",
				}
			],
		)

		by_key = {row["demand_key"]: row for row in result}
		early_material = by_key["EARLY"]["materials"][0]
		late_material = by_key["LATE"]["materials"][0]
		self.assertEqual(early_material["open_purchase_order_qty"], 10)
		self.assertEqual(early_material["status"], "awaiting_purchase_receipt")
		self.assertEqual(early_material["supply_documents"][0]["allocated_qty"], 10)
		self.assertEqual(late_material["open_purchase_order_qty"], 0)
		self.assertEqual(late_material["shortage_qty"], 10)
		self.assertEqual(late_material["supply_documents"][0]["allocated_qty"], 0)

	def test_material_priority_allocates_purchase_order_only_after_its_schedule_date(self):
		early = self._priority_demand("EARLY", "2026-08-04", "2026-08-01 09:00:00")
		late = self._priority_demand("LATE", "2026-08-10", "2026-08-02 09:00:00")

		result = self._attach_priority_material_coverage(
			[early, late],
			stock_qty=0,
			po_docs=[
				{
					"doctype": "Purchase Order",
					"name": "PO-BETWEEN-DATES",
					"status": "To Receive",
					"outstanding_qty": 10,
					"schedule_date": "2026-08-07",
				}
			],
		)

		by_key = {row["demand_key"]: row for row in result}
		early_document = by_key["EARLY"]["materials"][0]["supply_documents"][0]
		late_document = by_key["LATE"]["materials"][0]["supply_documents"][0]
		self.assertTrue(early_document["is_late"])
		self.assertEqual(early_document["allocated_qty"], 0)
		self.assertFalse(late_document["is_late"])
		self.assertEqual(late_document["allocated_qty"], 10)

	def test_material_coverage_blocks_unstarted_work_order_when_supply_is_inbound(self):
		stock_covered = self._priority_demand("STOCK", "2026-08-04", "2026-08-01 09:00:00")
		supply_covered = self._priority_demand("SUPPLY", "2026-08-10", "2026-08-02 09:00:00")

		result = self._attach_priority_material_coverage(
			[stock_covered, supply_covered],
			po_docs=[
				{
					"doctype": "Purchase Order",
					"name": "PO-INBOUND",
					"status": "To Receive",
					"outstanding_qty": 10,
					"schedule_date": "2026-08-09",
				}
			],
		)

		by_key = {row["demand_key"]: row for row in result}
		self.assertEqual(by_key["STOCK"]["status_code"], "ready_to_start")
		self.assertEqual(by_key["SUPPLY"]["material_summary"]["status_code"], "awaiting_supply")
		self.assertEqual(by_key["SUPPLY"]["status_code"], "material_shortage")
		self.assertFalse(
			any(action["action"] == "handle_shortage" for action in by_key["SUPPLY"]["next_actions"])
		)

	def test_material_priority_allocates_each_purchase_order_item_row(self):
		demand = self._priority_demand("COMBINED-PO", "2026-08-10", "2026-08-01 09:00:00")

		result = self._attach_priority_material_coverage(
			[demand],
			stock_qty=0,
			po_docs=[
				{
					"doctype": "Purchase Order",
					"name": "PO-TWO-ITEM-ROWS",
					"status": "To Receive",
					"outstanding_qty": 5,
					"schedule_date": "2026-08-03",
					"detail_name": "PO-TWO-ITEM-ROWS-1",
				},
				{
					"doctype": "Purchase Order",
					"name": "PO-TWO-ITEM-ROWS",
					"status": "To Receive",
					"outstanding_qty": 5,
					"schedule_date": "2026-08-03",
					"detail_name": "PO-TWO-ITEM-ROWS-2",
				},
			],
		)

		material = result[0]["materials"][0]
		self.assertEqual(material["open_purchase_order_qty"], 10)
		self.assertEqual(material["shortage_qty"], 0)
		self.assertEqual(
			[(document["outstanding_qty"], document["allocated_qty"]) for document in material["supply_documents"]],
			[(5, 5), (5, 5)],
		)
		self.assertTrue(all("detail_name" not in document for document in material["supply_documents"]))

	def test_material_priority_allocates_shared_material_request_only_once(self):
		early = self._priority_demand("EARLY", "2026-08-04", "2026-08-01 09:00:00")
		late = self._priority_demand("LATE", "2026-08-10", "2026-08-02 09:00:00")

		result = self._attach_priority_material_coverage(
			[late, early],
			stock_qty=0,
			mr_docs=[
				{
					"doctype": "Material Request",
					"name": "MR-SHARED",
					"status": "Pending",
					"outstanding_qty": 5,
					"schedule_date": "2026-08-03",
					"detail_name": "MR-SHARED-1",
				},
				{
					"doctype": "Material Request",
					"name": "MR-SHARED",
					"status": "Pending",
					"outstanding_qty": 5,
					"schedule_date": "2026-08-03",
					"detail_name": "MR-SHARED-2",
				}
			],
		)

		by_key = {row["demand_key"]: row for row in result}
		early_material = by_key["EARLY"]["materials"][0]
		late_material = by_key["LATE"]["materials"][0]
		self.assertEqual(early_material["open_material_request_qty"], 10)
		self.assertEqual(early_material["status"], "purchase_request_pending")
		self.assertEqual(sum(document["allocated_qty"] for document in early_material["supply_documents"]), 10)
		self.assertEqual(late_material["open_material_request_qty"], 0)
		self.assertEqual(late_material["shortage_qty"], 10)
		self.assertEqual(late_material["supply_documents"][0]["allocated_qty"], 0)

	def test_material_priority_consumes_purchase_orders_before_material_requests(self):
		demand = self._priority_demand("PO-FIRST", "2026-08-10", "2026-08-01 09:00:00")

		result = self._attach_priority_material_coverage(
			[demand],
			stock_qty=0,
			po_docs=[
				{
					"doctype": "Purchase Order",
					"name": "PO-SPLIT",
					"status": "To Receive",
					"outstanding_qty": 3,
					"schedule_date": "2026-08-03",
					"detail_name": "PO-SPLIT-1",
				},
				{
					"doctype": "Purchase Order",
					"name": "PO-SPLIT",
					"status": "To Receive",
					"outstanding_qty": 3,
					"schedule_date": "2026-08-03",
					"detail_name": "PO-SPLIT-2",
				},
			],
			mr_docs=[
				{
					"doctype": "Material Request",
					"name": "MR-FALLBACK",
					"status": "Pending",
					"outstanding_qty": 10,
					"schedule_date": "2026-08-03",
				}
			],
		)

		material = result[0]["materials"][0]
		self.assertEqual(material["open_purchase_order_qty"], 6)
		self.assertEqual(material["open_material_request_qty"], 4)
		self.assertEqual(
			[(document["name"], document["allocated_qty"]) for document in material["supply_documents"]],
			[("MR-FALLBACK", 4), ("PO-SPLIT", 3), ("PO-SPLIT", 3)],
		)
