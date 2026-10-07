from __future__ import annotations

from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import patch

import frappe

from process_simplification.production_reporting import dispatch_pool as pool


def _throw(message, exc=frappe.ValidationError, *args, **kwargs):
	raise exc(message)


class TestDispatchPool(TestCase):
	def setUp(self):
		self.stack = ExitStack()
		self.addCleanup(self.stack.close)
		self.stack.enter_context(patch.object(pool.frappe, "throw", side_effect=_throw))

	def test_gate_is_strict_and_default_off(self):
		for value, expected in ((None, False), (False, False), ("false", False), ("0", False), (True, True), (1, True), ("1", True)):
			with self.subTest(value=value), patch.object(pool.frappe, "conf", frappe._dict(enable_operation_dispatch_pool=value)):
				self.assertEqual(pool.is_enabled(), expected)

	def test_disabled_read_and_write_do_not_touch_business_services(self):
		with patch.object(pool, "is_enabled", return_value=False), patch.object(pool.service, "require_reviewer") as reviewer, patch.object(pool.service, "assign_workers") as assign:
			with self.assertRaises(frappe.PermissionError):
				pool.get_operation_dispatch_pool()
			with self.assertRaises(frappe.PermissionError):
				pool.assign_workers_first("JC", [], 10)
			reviewer.assert_not_called()
			assign.assert_not_called()

	def test_gate_does_not_replace_reviewer_permission(self):
		with patch.object(pool, "is_enabled", return_value=True), patch.object(pool.service, "require_reviewer", side_effect=frappe.PermissionError), patch.object(pool.frappe.db, "sql") as sql:
			with self.assertRaises(frappe.PermissionError):
				pool.get_operation_dispatch_pool()
			sql.assert_not_called()

	def _write_fixture(self, *, company="C", qty=10, plan_status=1):
		self.stack.enter_context(patch.object(pool, "_require_access", return_value={"C"}))
		# flt(..., precision) consults System Settings. Keep that lookup out of
		# the two-row get_value stub below: flt otherwise swallows StopIteration
		# from the exhausted mock and turns both compared quantities into zero.
		self.stack.enter_context(patch.object(pool.frappe, "get_system_settings", return_value="Banker's Rounding (legacy)"))
		jc = frappe._dict(name="JC", company=company, work_order="WO", operation_id="OP-ROW", for_quantity=qty)
		job_card = self.stack.enter_context(patch.object(pool.service, "job_card_values", return_value=jc))
		work_order = frappe._dict(name="WO", company=company, status="Not Started", docstatus=1, production_plan="PP")
		get_value = self.stack.enter_context(patch.object(pool.frappe.db, "get_value", side_effect=[work_order, frappe._dict(company=company, docstatus=plan_status)]))
		self.stack.enter_context(patch.object(pool.service, "job_card_qty_precision", return_value=3))
		assign = self.stack.enter_context(patch.object(pool.service, "assign_workers", return_value=[{"name": "ASSIGN"}]))
		return job_card, get_value, assign

	def test_first_write_delegates_with_guard_and_preserves_lock_order(self):
		job_card, get_value, assign = self._write_fixture()
		allocation = [{"employee": "E", "assigned_qty": 10}]
		result = pool.assign_workers_first("JC", allocation, 10, "reviewer", expected_work_order="WO", expected_operation_id="OP-ROW")
		job_card.assert_called_once_with("JC", for_update=True)
		self.assertTrue(get_value.call_args_list[0].kwargs["for_update"])
		assign.assert_called_once_with("JC", allocation, "reviewer", first_only=True)
		self.assertEqual(result, {"status": "assigned", "job_card": "JC", "assignments": [{"name": "ASSIGN"}]})

	def test_changed_quantity_never_reaches_reservations_or_assignment(self):
		_, _, assign = self._write_fixture(qty=11)
		with self.assertRaisesRegex(frappe.ValidationError, "任务量已变化"):
			pool.assign_workers_first("JC", [], 10)
		assign.assert_not_called()

	def test_invalid_or_nonfinite_expected_quantity_is_rejected(self):
		for value in (None, "bad", "nan", "inf", 0, -1):
			with self.subTest(value=value):
				with ExitStack() as local:
					old_stack, self.stack = self.stack, local
					try:
						_, _, assign = self._write_fixture()
						with self.assertRaises(frappe.ValidationError):
							pool.assign_workers_first("JC", [], value)
						assign.assert_not_called()
					finally:
						self.stack = old_stack

	def test_changed_operation_identity_never_dispatches(self):
		_, _, assign = self._write_fixture()
		with self.assertRaisesRegex(frappe.ValidationError, "工序已变化"):
			pool.assign_workers_first("JC", [], 10, expected_operation_id="OLD-OP")
		assign.assert_not_called()

	def test_cross_company_write_is_rejected_before_work_order_read(self):
		_, get_value, assign = self._write_fixture(company="OTHER")
		with self.assertRaises(frappe.PermissionError):
			pool.assign_workers_first("JC", [], 10)
		get_value.assert_not_called()
		assign.assert_not_called()

	def test_cancelled_plan_never_dispatches(self):
		_, _, assign = self._write_fixture(plan_status=2)
		with self.assertRaisesRegex(frappe.ValidationError, "生产计划已失效"):
			pool.assign_workers_first("JC", [], 10)
		assign.assert_not_called()

	def test_existing_history_error_is_not_caught_or_reported_as_success(self):
		_, _, assign = self._write_fixture()
		assign.side_effect = frappe.ValidationError("已存在派工或生产历史")
		with self.assertRaisesRegex(frappe.ValidationError, "已存在"):
			pool.assign_workers_first("JC", [], 10)
		self.assertTrue(assign.call_args.kwargs["first_only"])

	def test_history_blocks_first_only_even_when_original_card_is_replaceable(self):
		card = {"can_assign": True, "assignments": [], "remaining_qty": 10}
		for field in (
			"has_assignment_history", "has_work_history", "has_movement_history",
			"completed_qty", "process_loss_qty",
		):
			with self.subTest(field=field):
				state = pool._row_dispatch_state({field: True}, card)
				self.assertFalse(state["can_assign_first"])
				self.assertEqual(state["block_code"], "EXISTING_HISTORY")
		# A concurrent assignment can appear between list and context reads.
		self.assertFalse(pool._row_dispatch_state({}, {**card, "assignments": [{"name": "A"}]})["can_assign_first"])

	def test_pool_keeps_domain_blockers_instead_of_guessing_readiness(self):
		for code in ("PREVIOUS_OPERATION_PENDING", "MATERIAL_NOT_FULLY_ISSUED", "RATE_MISSING", "WORK_ORDER_UNAVAILABLE"):
			with self.subTest(code=code):
				state = pool._row_dispatch_state({}, {"can_assign": False, "block_code": code, "block_message": "reason"})
				self.assertFalse(state["can_assign_first"])
				self.assertEqual(state["block_code"], code)

	def test_assignment_filter_and_company_scope_apply_to_count_and_page(self):
		filters = pool._normalize_filters({"company": "C", "operation": "Cut", "assignment_state": "unassigned", "search": "SO"})
		scope, values = pool._pool_query_scope(filters, {"C"})
		self.assertIn("jc.company in %(companies)s", scope)
		self.assertIn("not exists (select 1 from `tabJob Card Worker Assignment`", scope)
		self.assertIn("pp.docstatus = 1", scope)
		self.assertEqual(values["companies"], ("C",))
		self.assertEqual(values["search"], "%SO%")
		with self.assertRaises(frappe.ValidationError):
			pool._normalize_filters({"ready_only": True})

	def test_read_is_bounded_and_reuses_one_context_for_same_work_order(self):
		rows = [frappe._dict(job_card=name, work_order="WO", has_assignment_history=0) for name in ("JC-1", "JC-2")]
		with patch.object(pool, "_require_access", return_value={"C"}), patch.object(pool.frappe.db, "sql", side_effect=[[(21,)], rows]) as sql, patch.object(pool.frappe, "get_list", return_value=["C"]), patch.object(pool.service, "get_work_order_assignment_context", return_value={"job_cards": [{"name": name, "can_assign": True} for name in ("JC-1", "JC-2")]}) as context, patch.object(pool.service, "assign_workers") as assign, patch.object(pool.frappe.db, "set_value") as write:
			result = pool.get_operation_dispatch_pool(page=2, page_length=1000)
			self.assertEqual(result["pagination"], {"page": 2, "page_length": 20, "total_count": 21, "total_pages": 2, "has_more": False})
			self.assertEqual(sql.call_args.args[1]["offset"], 20)
			context.assert_called_once_with("WO")
			assign.assert_not_called()
			write.assert_not_called()

	def test_cross_company_filter_is_rejected_without_query(self):
		with patch.object(pool, "_require_access", return_value={"C"}), patch.object(pool.frappe.db, "sql") as sql:
			with self.assertRaises(frappe.PermissionError):
				pool.get_operation_dispatch_pool(filters={"company": "OTHER"})
			sql.assert_not_called()
