from contextlib import ExitStack
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

import frappe

from process_simplification.api import quick_order_impact as impact
from process_simplification.api import shortage
from process_simplification.api.production import build_production_demand
from process_simplification.api.workbench import allocate_finished_stock


class TestQuickOrderImpact(TestCase):
	def evaluate(
		self,
		*,
		raw=10,
		existing_qty=10,
		new_qty=4,
		fg=0,
		reserved=0,
		existing_date="2099-09-22",
		new_date="2099-09-15",
		po=None,
		planned=0,
		production_committed=0,
	):
		order = dict(name="SO-LATER", company="COMPANY-A", creation="2099-01-01", delivery_date=existing_date)
		row = dict(
			sales_order=order["name"],
			company=order["company"],
			sales_order_item="SOI-LATER",
			item_code="FG",
			item_name="成品",
			warehouse="FG-WH",
			pending_qty=existing_qty,
			reserved_qty=reserved,
			available_stock_snapshot_qty=fg,
			active_work_order_qty=planned,
			material_status="未检查",
			order_creation=order["creation"],
			delivery_date=existing_date,
		)
		row = allocate_finished_stock([row])[0]
		order["rows"] = [row]
		demand = build_production_demand(order, row)
		if demand and planned:
			demand["production_plans"] = [{"name": "PP", "company": "COMPANY-A", "work_orders": []}]
		fulfillment = {"orders": [order]}
		baseline = [demand] if demand else []
		preview = [
			dict(
				row=1,
				item_code="FG",
				item_name="成品",
				warehouse="FG-WH",
				qty=new_qty,
				bom_no="BOM-FG",
				available_stock_snapshot_qty=fg,
			)
		]
		original = deepcopy((fulfillment, baseline))
		with ExitStack() as stack:
			stack.enter_context(
				patch(
					"process_simplification.api.workbench.get_fulfillment_overview", return_value=fulfillment
				)
			)
			stack.enter_context(
				patch(
					"process_simplification.api.production.get_production_overview",
					return_value={"demands": baseline},
				)
			)
			stack.enter_context(
				patch.object(
					shortage, "get_default_bom", side_effect=lambda code: "BOM-FG" if code == "FG" else None
				)
			)
			stack.enter_context(
				patch.object(
					shortage,
					"get_company_defaults",
					return_value=frappe._dict(company="COMPANY-A", source_warehouse="RAW-WH"),
				)
			)
			stack.enter_context(
				patch.object(
					shortage,
					"resolve_production_source_warehouse",
					return_value=frappe._dict(warehouse="RAW-WH", can_use=True),
				)
			)
			stack.enter_context(
				patch.object(
					shortage,
					"get_bom_items_as_dict",
					return_value={
						"RAW": frappe._dict(item_code="RAW", item_name="原料", stock_uom="Nos", qty=1)
					},
				)
			)
			stack.enter_context(
				patch.object(
					shortage,
					"get_material_stock_snapshot",
					return_value=frappe._dict(
						actual_qty=raw,
						available_qty=raw,
						free_qty=max(raw - production_committed, 0),
						committed_qty=production_committed,
						production_committed_qty=production_committed,
						can_calculate=True,
					),
				)
			)
			stack.enter_context(patch.object(shortage, "_mr_documents", return_value=[]))
			stack.enter_context(patch.object(shortage, "_po_documents", return_value=po or []))
			result = impact.evaluate_order_material_risk("COMPANY-A", new_date, preview)
		self.assertEqual(
			(fulfillment, baseline), original, "Simulation must not mutate the original plan/stock facts"
		)
		return result

	def test_own_ready_but_later_shortage_is_reported(self):
		result = self.evaluate()
		self.assertEqual(result["coverage"]["shortages"], [])
		material = result["downstream_impacts"][0]["materials"][0]
		self.assertEqual(
			(material["before_shortage_qty"], material["after_shortage_qty"], material["added_shortage_qty"]),
			(0, 4, 4),
		)

	def test_existing_shortage_is_not_notified_again(self):
		material = self.evaluate(raw=5)["downstream_impacts"][0]["materials"][0]
		self.assertEqual(
			(material["before_shortage_qty"], material["after_shortage_qty"], material["added_shortage_qty"]),
			(5, 9, 4),
		)

	def test_own_and_later_shortage_can_both_exist(self):
		result = self.evaluate(raw=2)
		self.assertEqual(result["coverage"]["shortages"][0]["shortage_qty"], 2)
		self.assertEqual(result["downstream_impacts"][0]["materials"][0]["added_shortage_qty"], 2)

	def test_no_stock_means_existing_gap_is_unchanged(self):
		result = self.evaluate(raw=0)
		self.assertEqual(result["coverage"]["shortages"][0]["shortage_qty"], 4)
		self.assertEqual(result["downstream_impacts"], [])

	def test_equal_delivery_date_keeps_existing_order_first(self):
		result = self.evaluate(new_date="2099-09-22")
		self.assertEqual(result["coverage"]["shortages"][0]["shortage_qty"], 4)
		self.assertEqual(result["downstream_impacts"], [])

	def test_later_new_order_does_not_displace_earlier_order(self):
		self.assertEqual(self.evaluate(new_date="2099-09-25")["downstream_impacts"], [])

	def test_free_finished_stock_loss_is_reported_even_when_new_order_needs_no_production(self):
		result = self.evaluate(fg=10, raw=0)
		self.assertEqual(result["coverage"]["requirements"], [])
		self.assertEqual(result["downstream_impacts"][0]["lost_finished_stock_qty"], 4)
		self.assertEqual(result["downstream_impacts"][0]["materials"][0]["added_shortage_qty"], 4)

	def test_reserved_finished_stock_is_not_displaced(self):
		result = self.evaluate(reserved=10, fg=0, raw=0)
		self.assertEqual(result["downstream_impacts"], [])
		self.assertEqual(result["coverage"]["shortages"][0]["shortage_qty"], 4)

	def test_existing_plan_commitment_is_not_silently_borrowed(self):
		result = self.evaluate(planned=10, production_committed=10)
		self.assertEqual(result["coverage"]["shortages"][0]["shortage_qty"], 4)
		self.assertEqual(result["downstream_impacts"], [])

	def test_inbound_supply_allocates_once_and_respects_delivery_date(self):
		po = [
			dict(
				name="PO",
				detail_name="POI",
				outstanding_qty=4,
				schedule_date="2099-09-20",
				doctype="Purchase Order",
			)
		]
		result = self.evaluate(raw=10, po=po)
		self.assertEqual(result["coverage"]["shortages"], [])
		self.assertEqual(result["downstream_impacts"], [])
		late = self.evaluate(raw=10, po=[dict(po[0], schedule_date="2099-09-30")])
		self.assertEqual(late["downstream_impacts"][0]["materials"][0]["added_shortage_qty"], 4)
		shared_po = self.evaluate(raw=0, po=[dict(po[0], schedule_date="2099-09-10", outstanding_qty=10)])
		self.assertEqual(shared_po["coverage"]["shortages"], [])
		self.assertEqual(shared_po["downstream_impacts"][0]["materials"][0]["added_shortage_qty"], 4)

	def test_decimal_rounding_does_not_invent_an_added_gap(self):
		material = dict(
			item_code="RAW", warehouse="WH", sources=[dict(sales_order_item="SOI", shortage_qty=0.1)]
		)
		after = dict(material, sources=[dict(sales_order_item="SOI", shortage_qty=0.100000000000001)])
		rows = {"SOI": dict(sales_order="SO", sales_order_item="SOI", available_to_reserve=0)}
		self.assertEqual(impact.compare_order_impacts([material], [after], rows, rows), [])

	def test_simulation_does_not_include_another_company(self):
		rows = {"orders": [dict(name="OTHER-SO", company="COMPANY-B", rows=[dict(sales_order_item="OTHER")])]}
		demands, before, after = impact.simulate_order_demands("COMPANY-A", "2099-09-15", [], rows, [])
		self.assertEqual((demands, before, after), ([], {}, {}))

	def test_review_fingerprint_changes_when_only_later_impact_changes(self):
		from process_simplification.api.quick_order import quick_order_review_fingerprint

		before = {
			"downstream_impacts": [{"sales_order": "SO-LATER", "materials": [{"added_shortage_qty": 1}]}]
		}
		after = deepcopy(before)
		after["downstream_impacts"][0]["materials"][0]["added_shortage_qty"] = 2
		self.assertNotEqual(quick_order_review_fingerprint(before), quick_order_review_fingerprint(after))

	def test_new_shortage_notifies_warehouse_and_production_once_each(self):
		from process_simplification import notifications

		impacts = self.evaluate()["downstream_impacts"]
		recipients = {
			notifications.WAREHOUSE_RESPONSIBILITY: ["warehouse", "both"],
			notifications.PROCUREMENT_RESPONSIBILITY: ["warehouse", "buyer", "both"],
			notifications.PRODUCTION_DISPATCH_RESPONSIBILITY: ["production", "both"],
		}
		with (
			patch.object(
				notifications,
				"responsibility_recipients",
				side_effect=lambda company, responsibility: recipients[responsibility],
			),
			patch.object(notifications, "notify_users", side_effect=lambda users, **kwargs: users) as notify,
		):
			result = notifications.notify_quick_order_submitted(
				"SO-NEW", "COMPANY-A", [], production_required=4, downstream_impacts=impacts
			)
		self.assertEqual(result, ["both", "buyer", "production", "warehouse"])
		calls = {user: call.kwargs for call in notify.call_args_list for user in call.args[0]}
		self.assertEqual(sum(len(call.args[0]) for call in notify.call_args_list), 4)
		self.assertIn("安排补料", calls["warehouse"]["description"])
		self.assertIn("后续排期", calls["production"]["description"])
		self.assertIn("安排补料", calls["both"]["description"])
		self.assertIn("后续排期", calls["both"]["description"])
		self.assertEqual(calls["warehouse"]["link"], notifications.SHORTAGE_ROUTE)
		self.assertEqual(calls["production"]["link"], notifications.PRODUCTION_WORKBENCH_ROUTE)
		for call in calls.values():
			self.assertIn("本单缺料 0 项；影响后续 1 单", call["description"])
			self.assertNotIn("added_shortage_qty", call["description"])
			self.assertLessEqual(len(call["description"]), 100)

	def test_every_new_order_notifies_both_teams_with_role_specific_content(self):
		from process_simplification import notifications

		stock_impact = [{"sales_order": "SO-LATER", "lost_finished_stock_qty": 4, "materials": []}]
		material_impact = self.evaluate()["downstream_impacts"]
		for production_qty, shortages, impacts in (
			(0, [], []),  # Direct shipment still informs production, without asking it to produce.
			(4, [], []),  # Enough materials must not suppress the new production demand.
			(4, [{"item_code": "RAW"}], []),
			(0, [], stock_impact),  # Replanning is relevant even without an added purchase gap.
			(0, [], material_impact),
		):
			with (
				self.subTest(production_qty=production_qty, shortages=shortages, impacts=impacts),
				patch.object(notifications, "responsibility_recipients", side_effect=lambda company, role:
					["production"] if role == notifications.PRODUCTION_DISPATCH_RESPONSIBILITY else ["warehouse"]
				) as recipients,
				patch.object(notifications, "notify_users", return_value=[]) as notify,
			):
				notifications.notify_quick_order_submitted(
					"SO-NEW", "COMPANY-A", shortages,
					production_required=production_qty, downstream_impacts=impacts,
				)
				self.assertEqual(notify.call_count, 2)
				calls = {call.args[0][0]: call.kwargs for call in notify.call_args_list}
				warehouse, production = calls["warehouse"], calls["production"]
				self.assertNotEqual(warehouse["description"], production["description"])
				self.assertIn("新销售订单", warehouse["subject"])
				self.assertEqual("无需安排生产" in production["description"], production_qty == 0)
				self.assertEqual("后续订单排期" in production["description"], bool(impacts))
				self.assertEqual(production["link"], notifications.PRODUCTION_WORKBENCH_ROUTE
					if production_qty or impacts else "/app/order-workbench?sales_order=SO-NEW")
				has_gap = bool(shortages or any(row["materials"] for row in impacts))
				self.assertEqual(warehouse["link"], notifications.SHORTAGE_ROUTE
					if has_gap else "/app/order-workbench?sales_order=SO-NEW")
				self.assertEqual(any(call.args[1] == notifications.PROCUREMENT_RESPONSIBILITY
					for call in recipients.call_args_list), has_gap)
