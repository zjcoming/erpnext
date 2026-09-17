from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from process_simplification.request_transaction import retry_request_transaction


class TestRequestTransaction(TestCase):
	def setUp(self):
		self.enterContext(patch("process_simplification.request_transaction._", lambda text: text))
		self.db = self.enterContext(patch.object(frappe, "db", Mock(), create=True))
		self.local = self.enterContext(patch.object(frappe, "local", SimpleNamespace(
			request=object(), form_dict={}, message_log=["before"],
			document_cache={"old": object()},
		)))

	def endpoint(self, body):
		def action():
			return body()
		self.local.form_dict["cmd"] = f"{action.__module__}.{action.__name__}"
		return retry_request_transaction(action)

	def test_success_leaves_commit_to_request_owner(self):
		action = self.endpoint(lambda: {"stock_entry": "DRAFT-1"})
		self.assertEqual(action(), {"stock_entry": "DRAFT-1"})
		self.db.commit.assert_not_called()
		self.db.rollback.assert_not_called()

	def test_conflict_reloads_after_rollback_and_removes_failed_attempt_messages(self):
		snapshot = {"available": 80}
		def body():
			if snapshot["available"] == 80:
				self.local.message_log.append("abandoned draft")
				raise frappe.QueryDeadlockError(1020, "Bin changed")
			self.assertEqual(self.local.message_log, ["before"])
			self.assertEqual(self.local.document_cache, {})
			return {"allowed_qty": snapshot["available"]}
		self.db.rollback.side_effect = lambda: snapshot.update(available=40)
		self.assertEqual(self.endpoint(body)(), {"allowed_qty": 40})
		self.db.rollback.assert_called_once_with()
		self.db.commit.assert_not_called()

	def test_retry_rechecks_business_rejection_instead_of_writing_old_quantity(self):
		body = Mock(side_effect=[frappe.QueryDeadlockError(1020), frappe.ValidationError("only 40")])
		with self.assertRaisesRegex(frappe.ValidationError, "only 40"):
			self.endpoint(body)()
		self.assertEqual(body.call_count, 2)
		self.db.rollback.assert_called_once_with()

	def test_internal_call_does_not_rollback_its_callers_work(self):
		body = Mock(side_effect=frappe.QueryDeadlockError(1020))
		action = self.endpoint(body)
		self.local.form_dict["cmd"] = "another.transaction.owner"
		with self.assertRaises(frappe.QueryDeadlockError):
			action()
		self.assertEqual(body.call_count, 1)
		self.db.rollback.assert_not_called()

	def test_non_http_call_does_not_rollback_its_callers_work(self):
		action = self.endpoint(Mock(side_effect=frappe.QueryDeadlockError(1020)))
		self.local.request = None
		with self.assertRaises(frappe.QueryDeadlockError):
			action()
		self.db.rollback.assert_not_called()

	def test_permission_rejection_is_not_retried(self):
		body = Mock(side_effect=frappe.PermissionError("denied"))
		with self.assertRaises(frappe.PermissionError):
			self.endpoint(body)()
		self.assertEqual(body.call_count, 1)
		self.db.rollback.assert_not_called()

	def test_repeated_conflicts_stop_after_three_complete_rollbacks(self):
		for error in (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			with self.subTest(error=error.__name__):
				self.db.reset_mock()
				body = Mock(side_effect=error(1020))
				with patch.object(frappe, "throw", side_effect=frappe.ValidationError("retry later")) as fail:
					with self.assertRaises(frappe.ValidationError):
						self.endpoint(body)()
					self.assertIn("本次操作未保存", fail.call_args.args[0])
				self.assertEqual(body.call_count, 3)
				self.assertEqual(self.db.rollback.call_count, 3)
				self.db.commit.assert_not_called()

	def test_monthly_wage_endpoints_retry_their_own_rpc_after_conflict(self):
		from process_simplification.api import production_reporting

		for name, kwargs, result in (
			("build_monthly_summaries", {"company": "Factory", "month_start": "2026-08-01"}, {"summaries": ["WAGE-1"]}),
			("confirm_monthly_summary", {"summary_name": "WAGE-1"}, {"name": "WAGE-1", "docstatus": 1, "total_amount": 50}),
		):
			with self.subTest(endpoint=name):
				self.db.reset_mock()
				self.local.form_dict["cmd"] = f"{production_reporting.__name__}.{name}"
				with patch.object(
					production_reporting.summary, name,
					side_effect=[frappe.QueryDeadlockError(1020), result],
				) as action:
					self.assertEqual(getattr(production_reporting, name)(**kwargs), result)
				self.assertEqual(action.call_count, 2)
				self.db.rollback.assert_called_once_with()
				self.db.commit.assert_not_called()

	def test_completed_stock_reservation_rechecks_quantity_after_rpc_deadlock(self):
		from process_simplification.api import actions

		self.local.form_dict["cmd"] = f"{actions.__name__}.reserve_completed_stock"
		with (
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(frappe, "get_doc", return_value=SimpleNamespace(company="Factory")),
			patch.object(actions, "_row_from_workbench", side_effect=[
				frappe._dict(completed_unreserved_qty=5), frappe._dict(completed_unreserved_qty=2)
			]) as rows,
			patch.object(actions, "_locked_row_from_workbench", side_effect=[
				frappe.QueryDeadlockError(1213), frappe._dict(completed_unreserved_qty=2)
			]),
			patch.object(actions, "get_sales_order_item", return_value=SimpleNamespace(item_code="FG")),
			patch.object(actions, "item_stock_qty", return_value=10),
			patch.object(actions, "_manufactured_finished_rows", return_value=[
				SimpleNamespace(t_warehouse="FG-WH", transfer_qty=8, parent="MFG-8", name="MFG-ROW")
			]),
			patch.object(actions, "is_batch_item", return_value=False),
			patch.object(actions, "get_available_qty_to_reserve", return_value=2),
			patch.object(actions, "normalize_qty", side_effect=float),
			patch.object(actions, "_new_sre", return_value=SimpleNamespace(name="SRE-2", reserved_qty=2)) as reserve,
		):
			self.assertEqual(actions.reserve_completed_stock("SO-10", "SO-ROW"),
				{"stock_reservation_entries": ["SRE-2"]})
		self.assertEqual(rows.call_count, 2)
		self.db.rollback.assert_called_once_with()
		self.db.commit.assert_not_called()
		reserve.assert_called_once()
		self.assertEqual(reserve.call_args.kwargs["qty"], 2)
		self.assertEqual(reserve.call_args.kwargs["from_voucher_detail_no"], "MFG-ROW")
