from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

import frappe

from process_simplification.api.workbench import allocate_finished_stock, apply_reservation_action_permissions
from process_simplification.management_access import (
	OWNER_ROLE,
	PRODUCTION_MANAGER_ROLE,
	MANAGED_DOCUMENT_PERMISSIONS,
	WAREHOUSE_OPERATOR_ROLE,
)


class TestReservationActionPermissions(TestCase):
	def rows(self):
		return [{
			"sales_order": "SO-1", "sales_order_item": "SOI-1",
			"status": "完工待预留", "status_code": "awaiting_order_reservation",
			"status_label": "完工待预留", "completed_unreserved_qty": 30,
			"next_actions": [
				{"action": "reserve_completed_stock", "label": "预留完工成品", "enabled": True, "reason": None},
				{"action": "reserve_stock", "label": "预留库存", "enabled": True, "reason": None},
				{"action": "view_sales_order", "label": "查看销售订单", "enabled": True, "reason": None},
			],
		}]

	def test_current_supervisor_role_gets_handoff_while_warehouse_and_owner_keep_actions(self):
		for role in (PRODUCTION_MANAGER_ROLE, WAREHOUSE_OPERATOR_ROLE, OWNER_ROLE):
			with self.subTest(role=role):
				permissions = MANAGED_DOCUMENT_PERMISSIONS[role]["Stock Reservation Entry"]
				rows = self.rows()
				before = deepcopy(rows)
				with patch.object(frappe, "has_permission", side_effect=lambda _doctype, permission: permission in permissions):
					apply_reservation_action_permissions(rows)
				if role != PRODUCTION_MANAGER_ROLE:
					self.assertEqual(rows, before)
					continue
				row = rows[0]
				self.assertFalse(row["next_actions"][0]["enabled"])
				self.assertFalse(row["next_actions"][1]["enabled"])
				self.assertTrue(row["next_actions"][2]["enabled"])
				self.assertEqual(row["status"], "待库房预留完工成品")
				self.assertEqual(row["status_label"], row["reservation_handoff"]["label"])
				self.assertIn("订单工作台", row["reservation_handoff"]["reason"])
				self.assertEqual(row["completed_unreserved_qty"], 30)
				self.assertEqual(row["sales_order_item"], "SOI-1")

	def test_create_without_submit_cannot_advertise_an_executable_reservation(self):
		rows = self.rows()
		with patch.object(frappe, "has_permission", side_effect=lambda _doctype, permission: permission == "create") as permission:
			apply_reservation_action_permissions(rows)
		self.assertFalse(rows[0]["next_actions"][0]["enabled"])
		self.assertEqual([call.args for call in permission.call_args_list], [
			("Stock Reservation Entry", "create"), ("Stock Reservation Entry", "submit")])

	def test_existing_block_and_production_status_are_not_replaced_by_handoff(self):
		rows = self.rows()
		rows[0].update(status="待安排生产", status_code="planning_required", status_label="待创建生产计划")
		rows[0]["next_actions"][0].update(enabled=False, reason="来源批次库存不足")
		with patch.object(frappe, "has_permission", return_value=False):
			apply_reservation_action_permissions(rows)
		self.assertEqual(rows[0]["next_actions"][0]["reason"], "来源批次库存不足")
		self.assertEqual(rows[0]["status"], "待安排生产")
		self.assertEqual(rows[0]["status_label"], "待创建生产计划")

	def test_no_reservation_action_does_not_query_unrelated_permissions(self):
		rows = [{"next_actions": [{"action": "view_sales_order", "enabled": True}]}]
		with patch.object(frappe, "has_permission") as permission:
			self.assertIs(apply_reservation_action_permissions(rows), rows)
		permission.assert_not_called()

	def test_shared_stock_reallocation_removes_an_obsolete_reservation_handoff(self):
		rows = []
		for index, date in enumerate(("2026-10-10", "2026-10-11"), 1):
			rows.append({
				"sales_order": f"SO-{index}", "sales_order_item": f"SOI-{index}",
				"company": "Factory", "item_code": "FG", "warehouse": "Finished Goods",
				"delivery_date": date, "pending_qty": 10, "available_to_reserve": 10,
				"available_stock_snapshot_qty": 10, "completed_unreserved_qty": 0,
				"reservation_handoff": {"label": "待库房预留库存", "reason": "旧快照"},
			})
		with patch.object(frappe, "has_permission", return_value=False):
			result = apply_reservation_action_permissions(allocate_finished_stock(rows))
		self.assertEqual(result[0]["reservation_handoff"]["label"], "待库房预留库存")
		self.assertEqual(result[1]["available_to_reserve"], 0)
		self.assertEqual(result[1]["status"], "待安排生产")
		self.assertNotIn("reservation_handoff", result[1])
		self.assertNotIn("reserve_stock", {a["action"] for a in result[1]["next_actions"]})

	def test_backend_denies_unauthorized_completed_reservation_before_reading_the_order(self):
		from process_simplification.api import actions

		with (
			patch.object(actions.frappe, "has_permission", side_effect=frappe.PermissionError),
			patch.object(actions, "_row_from_workbench") as read_order,
		):
			with self.assertRaises(frappe.PermissionError):
				actions._reserve_completed_stock("SO-1", "SOI-1")
		read_order.assert_not_called()
