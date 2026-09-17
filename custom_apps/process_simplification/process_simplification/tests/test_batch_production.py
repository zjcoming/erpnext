from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from process_simplification.api.production_readiness import (
	_attach_effective_batch_reservations,
	_loaded_work_order_stock_pool,
	allocate_work_order_readiness,
	attach_work_order_source_reservations,
	build_work_order_graph,
)
from process_simplification.api.shortage import get_material_stock_snapshot
from process_simplification.production_workflow.service import (
	_correct_batch_backflush_after_returns,
	_executable_available_qty,
	_new_work_order_reservation,
)


BATCH_FACTS = "process_simplification.batch_compat.get_batch_stock_facts"


class TestBatchProductionStock(UnitTestCase):
	def test_cancelled_batch_receipt_reconciles_return_usage_after_native_wo_update(self):
		from process_simplification.production_reporting.stock_entry import SubassemblyReservationStockEntryMixin

		events = []
		class NativeEntry:
			def update_work_order(self):
				events.append("native")
				return "updated"

		class Entry(SubassemblyReservationStockEntryMixin, NativeEntry):
			def get(self, key):
				return getattr(self, key, None)

		entry = Entry()
		entry.flags = frappe._dict(material_handling_verified=False)
		entry.docstatus, entry.purpose, entry.work_order = 2, "Manufacture", "WO"
		entry.custom_process_workflow_action = "Receipt Request"
		entry.items = [frappe._dict(item_code="RM", s_warehouse="WIP")]
		for batch, returned, expected in [(True, True, ["native", "reconcile"]),
			(False, True, ["native"]), (True, False, ["native"])]:
			with self.subTest(batch=batch, returned=returned):
				events.clear()
				with patch("process_simplification.batch_compat.is_batch_item", return_value=batch), \
					patch("process_simplification.production_reporting.stock_entry.frappe.db.exists", return_value=returned), \
					patch("process_simplification.production_reporting.stock_entry._reconcile_work_order_reservations",
						side_effect=lambda _doc: events.append("reconcile")):
					self.assertEqual(entry.update_work_order(), "updated")
				self.assertEqual(events, expected)

	def _backflush_fixture(self):
		entry = MagicMock()
		entry.get.side_effect = {"purpose": "Manufacture"}.get
		entry.get_backflush_based_on.return_value = "Material Transferred for Manufacture"
		entry.fg_completed_qty, entry.posting_date, entry.posting_time = 3, "2026-09-14", "12:00:00"
		entry.items = [frappe._dict(item_code="RM", s_warehouse="WIP", batch_no="A", qty=3)]
		order = frappe._dict(name="WO", qty=5, produced_qty=2,
			material_transferred_for_manufacturing=5,
			required_items=[frappe._dict(item_code="RM", required_qty=5)])
		route = frappe._dict(item_code="RM", original_item="RM", stock_uom="Nos",
			source_warehouse="WIP", return_warehouse="Stores", native_available_qty=3,
			lots=[frappe._dict(batch_no="A", serial_no=None, qty=2),
				frappe._dict(batch_no="B", serial_no=None, qty=1)])
		return entry, order, route

	def test_batch_return_backflush_uses_net_lots_and_current_manufacture_qty(self):
		entry, order, route = self._backflush_fixture()
		# The route is already net of two earlier consumed units and one returned
		# unit. Never rebuild the original five-unit issue as the next consumption.
		with patch("process_simplification.batch_compat.is_batch_item", return_value=True), \
			patch("process_simplification.production_workflow.service.frappe.db.exists", return_value=True), \
			patch("process_simplification.production_workflow.service.frappe.get_all", return_value=[]), \
			patch("process_simplification.production_workflow.service.frappe.get_cached_value", return_value=0), \
			patch("process_simplification.production_exceptions.material_routes.load_routes", return_value=[route]), \
			patch("process_simplification.batch_compat.get_stock_snapshot", return_value=frappe._dict(
				batch_available_qty={"A": 2, "B": 1, "UNRELATED": 5}, available_qty=8)):
			_correct_batch_backflush_after_returns(entry, order)
		rows = [call.args[1] for call in entry.append.call_args_list]
		self.assertEqual([(row["batch_no"], row["qty"], row["s_warehouse"]) for row in rows],
			[("A", 2, "WIP"), ("B", 1, "WIP")])
		self.assertTrue(all(not row["t_warehouse"] for row in rows))

	def test_batch_return_backflush_does_not_replace_a_moved_source_batch(self):
		entry, order, route = self._backflush_fixture()
		with patch("process_simplification.batch_compat.is_batch_item", return_value=True), \
			patch("process_simplification.production_workflow.service.frappe.db.exists", return_value=True), \
			patch("process_simplification.production_workflow.service.frappe.get_all", return_value=[]), \
			patch("process_simplification.production_workflow.service.frappe.get_cached_value", return_value=0), \
			patch("process_simplification.production_exceptions.material_routes.load_routes", return_value=[route]), \
			patch("process_simplification.batch_compat.get_stock_snapshot", return_value=frappe._dict(
				batch_available_qty={"A": 0, "B": 1, "UNRELATED": 5}, available_qty=6)):
			with self.assertRaises(frappe.ValidationError):
				_correct_batch_backflush_after_returns(entry, order)
		entry.set.assert_not_called()

	def test_batch_return_backflush_caps_extra_issued_lots_to_this_bom_quantity(self):
		entry, order, route = self._backflush_fixture()
		entry.fg_completed_qty = 1
		route.native_available_qty = 4
		route.lots[0].qty = 3
		with patch("process_simplification.batch_compat.is_batch_item", return_value=True), \
			patch("process_simplification.production_workflow.service.frappe.db.exists", return_value=True), \
			patch("process_simplification.production_workflow.service.frappe.get_all", return_value=[]), \
			patch("process_simplification.production_workflow.service.frappe.get_cached_value", return_value=0), \
			patch("process_simplification.production_exceptions.material_routes.load_routes", return_value=[route]), \
			patch("process_simplification.batch_compat.get_stock_snapshot", return_value=frappe._dict(
				batch_available_qty={"A": 3, "B": 1}, available_qty=4)):
			_correct_batch_backflush_after_returns(entry, order)
		rows = [call.args[1] for call in entry.append.call_args_list]
		self.assertEqual([(row["batch_no"], row["qty"]) for row in rows], [("A", 1)])

	def test_no_return_keeps_native_batch_draft_unchanged(self):
		entry, order, _route = self._backflush_fixture()
		with patch("process_simplification.batch_compat.is_batch_item", return_value=True), \
			patch("process_simplification.production_workflow.service.frappe.db.exists", return_value=False), \
			patch("process_simplification.production_exceptions.material_routes.load_routes") as load:
			_correct_batch_backflush_after_returns(entry, order)
		entry.set.assert_not_called()
		load.assert_not_called()

	def test_non_batch_return_keeps_native_draft_unchanged(self):
		entry, order, _route = self._backflush_fixture()
		with patch("process_simplification.batch_compat.is_batch_item", return_value=False), \
			patch("process_simplification.production_workflow.service.frappe.db.exists") as exists, \
			patch("process_simplification.production_exceptions.material_routes.load_routes") as load:
			_correct_batch_backflush_after_returns(entry, order)
		entry.set.assert_not_called()
		exists.assert_not_called()
		load.assert_not_called()

	def test_invalid_sales_batch_reservation_blocks_new_production_actions(self):
		from process_simplification.api.production import (
			attach_material_coverage, attach_production_plan_readiness, build_production_demand,
		)

		demand = build_production_demand(
			{"name": "SO", "company": "C", "delivery_date": "2026-09-20"},
			{"sales_order_item": "SOI", "item_code": "FG", "invalid_reserved_qty": 4,
				"production_required_qty": 4, "unplanned_production_qty": 4,
				"completed_unreserved_qty": 3}, today="2026-09-14",
		)
		self.assertEqual(demand["production_required_qty"], 4)
		self.assertEqual(demand["completed_unreserved_qty"], 3)
		demand = attach_material_coverage([demand], {"materials": [
			{"item_code": "RM", "warehouse": "Stores", "status": "new_purchase_required",
				"shortage_qty": 4, "current_gap_qty": 4,
				"sources": [{"demand_key": "SOI", "required_qty": 4}]},
		]})[0]
		demand = attach_production_plan_readiness([demand], {"SOI": [{
			"name": "PLAN", "work_orders": [{"name": "WO", "readiness_status": "ready_now",
				"flow_status": "ready_for_issue", "required_items": []}],
		}]})[0]
		self.assertEqual(demand["invalid_reserved_qty"], 4)
		self.assertEqual(demand["status_code"], "batch_reservation_blocked")
		self.assertEqual([row["action"] for row in demand["next_actions"]], ["view_sales_order"])
		self.assertEqual(demand["work_orders"][0]["flow_status"], "ready_for_issue")
		self.assertEqual(demand["work_orders"][0]["readiness_status"], "ready_now")

	def test_invalid_batch_reservation_stays_visible_without_new_production_gap(self):
		from process_simplification.api.production import build_production_demand

		demand = build_production_demand({"name": "SO"},
			{"sales_order_item": "SOI", "invalid_reserved_qty": 2}, today="2026-09-14")
		self.assertIsNotNone(demand)
		self.assertEqual(demand["production_required_qty"], 0)
		self.assertEqual(demand["status_code"], "batch_reservation_blocked")

	def test_batch_snapshot_keeps_ledger_and_soft_commitments_separate(self):
		with patch("process_simplification.api.shortage.frappe.db.get_value", return_value=frappe._dict(
			actual_qty=100, reserved_qty=20, reserved_qty_for_production=15,
			reserved_qty_for_sub_contract=0, reserved_qty_for_production_plan=5,
		)), patch(BATCH_FACTS, return_value={"effective_qty": 60, "free_qty": 20}):
			snapshot = get_material_stock_snapshot("BATCH-RM", "Stores")
		self.assertEqual(snapshot.actual_qty, 100)
		self.assertEqual(snapshot.available_qty, 40)
		self.assertEqual(snapshot.production_committed_qty, 20)
		self.assertEqual(snapshot.free_qty, 20)
		self.assertEqual(snapshot.batch_free_qty, 20)

	def test_loaded_plan_addback_does_not_double_subtract_owned_hard_stock(self):
		snapshot = dict(available_qty=60, production_committed_qty=50, batch_free_qty=20)
		# 40 loaded commitments leave 10 external; executable own locks are 15.
		self.assertEqual(_loaded_work_order_stock_pool(snapshot, 40, 0, 15), 35)
		# An unrelated soft commitment still constrains that executable ceiling.
		self.assertEqual(_loaded_work_order_stock_pool(snapshot, 10, 0, 15), 20)

	def test_non_batch_pool_keeps_existing_plan_reservation_addback(self):
		snapshot = dict(available_qty=10, production_committed_qty=30, free_qty=0)
		self.assertEqual(_loaded_work_order_stock_pool(snapshot, 10, 15, 4), 5)

	def test_expired_owned_batch_is_not_restored_to_readiness(self):
		entries = [
			frappe._dict(name="EXPIRED", voucher_detail_no="I1", item_code="RM", warehouse="Stores", reserved_qty=8),
			frappe._dict(name="VALID", voucher_detail_no="I2", item_code="RM", warehouse="Stores", reserved_qty=5),
		]
		items = [frappe._dict(name="I1", item_code="RM", source_warehouse="Stores"),
			frappe._dict(name="I2", item_code="RM", source_warehouse="Stores")]
		with patch(BATCH_FACTS, return_value={"effective_reserved_by_sre": {"EXPIRED": 0, "VALID": 3}}) as facts:
			_attach_effective_batch_reservations(entries)
			attach_work_order_source_reservations(items, entries)
		self.assertEqual([item.source_reserved_qty for item in items], [0, 3])
		self.assertEqual(facts.call_count, 1)

	def _plan(self, name, delivery, *, reserved=0, status="Not Started"):
		return build_work_order_graph(
			{"name": "PP-" + name, "planned_date": "2026-09-14", "creation": name},
			[dict(name="WO-" + name, production_item="FG", production_plan_item="ROOT-" + name,
				status=status, order_delivery_date=delivery, sales_order="SO-" + name)],
			[dict(parent="WO-" + name, item_code="RM", source_warehouse="Stores", required_qty=7,
				transferred_qty=0, source_reserved_qty=reserved, is_purchase_item=1)],
			[], active_bom_items={"FG"},
		)

	def test_batch_pool_still_shared_in_order_delivery_priority(self):
		early = self._plan("EARLY", "2026-09-15")
		late = self._plan("LATE", "2026-09-20", reserved=2)
		result = allocate_work_order_readiness([late, early], {
			("RM", "Stores"): dict(actual_qty=20, available_qty=10,
				production_committed_qty=14, batch_free_qty=8),
		})
		rows = {plan.name: plan.work_orders_by_name["WO-" + plan.name[3:]].required_items[0] for plan in result}
		self.assertEqual(rows["PP-EARLY"].available_qty, 7)
		self.assertEqual(rows["PP-LATE"].available_qty, 3)
		self.assertEqual(rows["PP-LATE"].effective_reserved_qty, 2)
		self.assertEqual(rows["PP-LATE"].current_gap_qty, 4)

	def test_terminal_plan_does_not_claim_shared_batch_surplus(self):
		done = self._plan("DONE", "2026-09-15", status="Completed")
		active = self._plan("ACTIVE", "2026-09-20")
		result = allocate_work_order_readiness([done, active], {
			("RM", "Stores"): dict(actual_qty=20, available_qty=7,
				production_committed_qty=7, batch_free_qty=7),
		})
		self.assertEqual(result[0].work_orders_by_name["WO-DONE"].required_items, [])
		self.assertEqual(result[1].work_orders_by_name["WO-ACTIVE"].required_items[0].available_qty, 7)

	def test_dispatch_only_adds_back_this_work_orders_effective_locks(self):
		with patch(BATCH_FACTS, return_value={
			"free_qty": 2, "effective_reserved_by_sre": {"OWN": 3, "EXPIRED": 0, "OTHER": 9},
		}), patch("process_simplification.production_workflow.service.frappe.get_all", return_value=["OWN", "EXPIRED"]):
			self.assertEqual(_executable_available_qty("RM", "Stores", work_order="WO", ordinary_available=100), 5)

	def test_plain_work_order_batch_reservation_does_not_require_lot_selection(self):
		sre = MagicMock()
		with patch("process_simplification.production_workflow.service._item_stock_details", return_value={"has_batch_no": 1}), \
			patch("process_simplification.production_workflow.stock_reservation.locked_available_qty", return_value=6), \
			patch("process_simplification.production_workflow.service.frappe.db.set_value"), \
			patch("process_simplification.production_workflow.service.frappe.new_doc", return_value=sre), \
			patch("process_simplification.batch_compat.source_batch_allocation") as source:
			_new_work_order_reservation(work_order=frappe._dict(name="WO", company="C"),
				work_order_item=frappe._dict(name="ROW", item_code="RM", source_warehouse="Stores", required_qty=6),
				qty=6, from_stock_entry="ISSUE-DRAFT", from_detail="ISSUE-ROW")
		self.assertEqual(sre.update.call_args.args[0]["reserved_qty"], 6)
		source.assert_not_called()
		sre.append.assert_not_called()
		sre.submit.assert_called_once()

	def test_directed_receipt_passes_only_capped_source_batches(self):
		sre = MagicMock()
		source_row = frappe._dict(name="SOURCE-ROW", parent="RECEIPT", item_code="SEMI")
		lots = [dict(batch_no="A", qty=2, warehouse="Stores"), dict(batch_no="B", qty=2, warehouse="Stores")]
		with patch("process_simplification.production_workflow.service._item_stock_details", return_value={"has_batch_no": 1}), \
			patch("process_simplification.production_workflow.stock_reservation.locked_available_qty", return_value=4), \
			patch("process_simplification.production_workflow.service.frappe.db.set_value"), \
			patch("process_simplification.production_workflow.service.frappe.new_doc", return_value=sre), \
			patch("process_simplification.batch_compat.source_batch_allocation", return_value=lots) as source:
			_new_work_order_reservation(work_order=frappe._dict(name="WO", company="C"),
				work_order_item=frappe._dict(name="ROW", item_code="SEMI", source_warehouse="Stores", required_qty=9),
				qty=6, from_stock_entry="RECEIPT", from_detail="SOURCE-ROW", source_detail_doc=source_row)
		source.assert_called_once_with(source_row, 4)
		self.assertEqual(sre.update.call_args.args[0]["reserved_qty"], 4)
		self.assertEqual([call.args for call in sre.append.call_args_list], [("sb_entries", row) for row in lots])

	def test_missing_source_lot_never_falls_back_to_other_batches(self):
		with patch("process_simplification.production_workflow.service._item_stock_details", return_value={"has_batch_no": 1}), \
			patch("process_simplification.production_workflow.stock_reservation.locked_available_qty", return_value=10), \
			patch("process_simplification.batch_compat.source_batch_allocation", return_value=[]), \
			patch("process_simplification.production_workflow.service.frappe.new_doc") as new_doc:
			result = _new_work_order_reservation(work_order=frappe._dict(name="WO", company="C"),
				work_order_item=frappe._dict(name="ROW", item_code="SEMI", source_warehouse="Stores"),
				qty=4, source_detail_doc=frappe._dict(name="SOURCE"))
		self.assertIsNone(result)
		new_doc.assert_not_called()
