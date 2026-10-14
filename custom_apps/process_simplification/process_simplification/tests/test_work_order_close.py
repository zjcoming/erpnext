from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from erpnext.manufacturing.doctype.work_order import work_order as native
from process_simplification import hooks
from process_simplification.management_access import OWNER_ROLE, PRODUCTION_MANAGER_ROLE
from process_simplification.production_reporting import work_order as reporting
from process_simplification.production_workflow import stock_reservation as reservation
from process_simplification.production_workflow import work_order_close as close


def throw(message, exception=frappe.ValidationError, **kwargs):
	raise exception(message)


class TestWorkOrderClose(TestCase):
	def setUp(self):
		self.enterContext(patch.object(frappe, "flags", frappe._dict()))
		self.enterContext(patch.object(frappe, "session", SimpleNamespace(user="manager@example.com")))
		self.enterContext(patch.object(frappe, "throw", side_effect=throw))
		self.enterContext(patch.object(close, "_", lambda message: message))
		self.roles = self.enterContext(patch.object(frappe, "get_roles", return_value=[PRODUCTION_MANAGER_ROLE]))
		self.scope = self.enterContext(patch.object(close, "user_company_scope", return_value={"Factory"}))
		self.guard = self.enterContext(patch.object(close, "assert_no_managed_draft_job_cards"))
		self.lock_cards = self.enterContext(patch.object(close, "managed_draft_job_cards", return_value=["JC-2"]))
		self.close_guard = self.enterContext(patch.object(close, "assert_work_order_close_allowed", return_value=["AS-2", "AS-3"]))
		self.retire = self.enterContext(patch.object(close, "cancel_unstarted_continuation_assignments"))
		self.doc = frappe._dict(name="WO-1", company="Factory", docstatus=1, status="Stopped", operations=[])
		self.doc.check_permission = Mock()
		self.get_doc = self.enterContext(patch.object(frappe, "get_doc", return_value=self.doc))
		self.native_close = self.enterContext(patch.object(close, "native_close_work_order", return_value="Closed"))

	def test_rpc_binding_accepts_only_post(self):
		self.assertEqual(hooks.override_whitelisted_methods[
			"erpnext.manufacturing.doctype.work_order.work_order.close_work_order"
		], "process_simplification.production_workflow.work_order_close.close_work_order")
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[close.close_work_order], ["POST"])

	def test_owner_and_manager_grant_only_the_authorized_work_order_during_native_close(self):
		for role in (OWNER_ROLE, PRODUCTION_MANAGER_ROLE):
			with self.subTest(role=role):
				self.roles.return_value = [role]
				def native_close(name, status):
					self.assertEqual((name, status), ("WO-1", "Closed"))
					self.assertEqual(frappe.flags[reservation._CLOSE_SRE_VOUCHER_FLAG],
						("WO-1", "Factory", "manager@example.com"))
					self.assertNotIn(reservation._GUIDED_SRE_VOUCHER_FLAG, frappe.flags)
					self.assertEqual(frappe.flags[reporting._CLOSE_CONTINUATION_FLAG].work_order, "WO-1")
					self.assertEqual(frappe.flags[reporting._CLOSE_CONTINUATION_FLAG].company, "Factory")
					self.assertEqual(frappe.flags[reporting._CLOSE_CONTINUATION_FLAG].user, "manager@example.com")
					return "Closed"
				self.native_close.side_effect = native_close
				self.assertEqual(close.close_work_order("WO-1", "Closed"), "Closed")
				self.assertNotIn(reservation._CLOSE_SRE_VOUCHER_FLAG, frappe.flags)
				self.assertNotIn(reporting._CLOSE_CONTINUATION_FLAG, frappe.flags)
		self.retire.assert_called_with("WO-1", ["AS-2", "AS-3"])
		self.doc.check_permission.assert_called_with("write")
		self.get_doc.assert_called_with("Work Order", "WO-1", for_update=True)

	def test_record_permission_denied_before_locks_or_native_writes(self):
		self.doc.check_permission.side_effect = frappe.PermissionError
		with self.assertRaises(frappe.PermissionError):
			close.close_work_order("WO-1", "Closed")
		self.guard.assert_not_called()
		self.lock_cards.assert_not_called()
		self.close_guard.assert_not_called()
		self.retire.assert_not_called()
		self.native_close.assert_not_called()

	def test_missing_or_other_company_is_rejected(self):
		for companies in (set(), {"Other Factory"}):
			with self.subTest(companies=companies):
				self.scope.return_value = companies
				with self.assertRaises(frappe.PermissionError):
					close.close_work_order("WO-1", "Closed")
		self.guard.assert_not_called()
		self.lock_cards.assert_not_called()
		self.close_guard.assert_not_called()
		self.retire.assert_not_called()
		self.native_close.assert_not_called()

	def test_submitted_state_is_rechecked_after_lock(self):
		for docstatus in (0, 2):
			with self.subTest(docstatus=docstatus):
				self.doc.docstatus = docstatus
				with self.assertRaises(frappe.ValidationError):
					close.close_work_order("WO-1", "Closed")
		self.native_close.assert_not_called()

	def test_other_status_cannot_use_close_authority(self):
		for status in ("Stopped", "In Process", "Cancelled", ""):
			with self.subTest(status=status), self.assertRaises(frappe.ValidationError):
				close.close_work_order("WO-1", status)
		self.get_doc.assert_not_called()
		self.native_close.assert_not_called()

	def test_job_cards_lock_before_work_order_and_close_guard(self):
		events = []
		self.doc.check_permission.side_effect = lambda *_args: events.append("permission")
		self.lock_cards.side_effect = lambda *_args, **_kwargs: events.append("job_cards") or ["JC-2"]
		def get_doc(*_args, **kwargs):
			events.append("work_order_lock" if kwargs.get("for_update") else "work_order_read")
			return self.doc
		self.get_doc.side_effect = get_doc
		def close_guard(name):
			events.append("close_guard")
			self.assertEqual(name, "WO-1")
			self.assertIn(reporting._CLOSE_CONTINUATION_FLAG, frappe.flags)
			raise frappe.ValidationError("Unfinished reporting Job Card")
		self.close_guard.side_effect = close_guard
		with self.assertRaisesRegex(frappe.ValidationError, "Unfinished"):
			close.close_work_order("WO-1", "Closed")
		self.assertEqual(events, ["work_order_read", "permission", "job_cards",
			"work_order_lock", "permission", "close_guard"])
		self.lock_cards.assert_called_once_with("WO-1", for_update=True)
		self.native_close.assert_not_called()
		self.retire.assert_not_called()
		self.assertNotIn(reporting._CLOSE_CONTINUATION_FLAG, frappe.flags)

	def test_permission_and_company_are_rechecked_after_locks(self):
		self.doc.check_permission.side_effect = [None, frappe.PermissionError("Changed permission")]
		with self.assertRaises(frappe.PermissionError):
			close.close_work_order("WO-1", "Closed")
		self.lock_cards.assert_called_once_with("WO-1", for_update=True)
		self.close_guard.assert_not_called()
		self.native_close.assert_not_called()
		self.retire.assert_not_called()
		self.doc.check_permission.side_effect = None
		self.scope.side_effect = [{"Factory"}, {"Other Factory"}]
		with self.assertRaises(frappe.PermissionError):
			close.close_work_order("WO-1", "Closed")
		self.close_guard.assert_not_called()
		self.native_close.assert_not_called()
		self.retire.assert_not_called()

	def test_native_active_job_card_guard_is_preserved(self):
		self.native_close.side_effect = native.close_work_order
		self.doc.operations = [{"name": "OP-1"}]
		with (
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(frappe, "get_list", return_value=["JC-1"]),
			patch.object(native, "_", lambda message: message),
			self.assertRaisesRegex(frappe.ValidationError, "JC-1"),
		):
			close.close_work_order("WO-1", "Closed")
		self.assertEqual(self.doc.status, "Stopped")
		self.assertNotIn(reservation._CLOSE_SRE_VOUCHER_FLAG, frappe.flags)
		self.retire.assert_not_called()

	def test_native_status_and_lifecycle_are_called_without_impersonation(self):
		self.native_close.side_effect = native.close_work_order
		def update_status(status):
			self.doc.status = status
		self.doc.update_status = Mock(side_effect=update_status)
		self.doc.on_close_or_cancel = Mock()
		self.doc.notify_update = Mock()
		with patch.object(frappe, "has_permission", return_value=True), patch.object(frappe, "msgprint"), \
			patch.object(native, "_", lambda message: message):
			self.assertEqual(close.close_work_order("WO-1", "Closed"), "Closed")
		self.doc.update_status.assert_called_once_with("Closed")
		self.doc.on_close_or_cancel.assert_called_once_with()
		self.doc.notify_update.assert_called_once_with()
		self.assertEqual(frappe.session.user, "manager@example.com")

	def test_repeat_closed_order_does_not_repeat_native_cancellation(self):
		self.doc.status = "Closed"
		self.assertEqual(close.close_work_order("WO-1", "Closed"), "Closed")
		self.native_close.assert_not_called()
		self.close_guard.assert_not_called()
		self.guard.assert_not_called()
		self.retire.assert_not_called()
		self.assertNotIn(reporting._CLOSE_CONTINUATION_FLAG, frappe.flags)

	def test_native_user_gets_no_extra_reservation_authority(self):
		self.roles.return_value = ["Manufacturing Manager"]
		def native_close(*_args):
			self.assertNotIn(reservation._CLOSE_SRE_VOUCHER_FLAG, frappe.flags)
			self.assertNotIn(reporting._CLOSE_CONTINUATION_FLAG, frappe.flags)
		self.native_close.side_effect = native_close
		close.close_work_order("WO-1", "Closed")
		self.scope.assert_not_called()
		self.guard.assert_called_once_with("WO-1")
		self.close_guard.assert_not_called()
		self.retire.assert_not_called()

	def test_native_user_keeps_strict_draft_guard(self):
		self.roles.return_value = ["Manufacturing Manager"]
		self.guard.side_effect = frappe.ValidationError("Draft continuation")
		with self.assertRaisesRegex(frappe.ValidationError, "Draft continuation"):
			close.close_work_order("WO-1", "Closed")
		self.close_guard.assert_not_called()
		self.native_close.assert_not_called()
		self.retire.assert_not_called()
		self.assertNotIn(reporting._CLOSE_CONTINUATION_FLAG, frappe.flags)

	def test_native_failure_propagates_and_clears_scope(self):
		self.native_close.side_effect = frappe.ValidationError("Native cancellation blocked")
		with self.assertRaisesRegex(frappe.ValidationError, "Native cancellation"):
			close.close_work_order("WO-1", "Closed")
		self.assertNotIn(reservation._CLOSE_SRE_VOUCHER_FLAG, frappe.flags)
		self.assertNotIn(reporting._CLOSE_CONTINUATION_FLAG, frappe.flags)
		self.retire.assert_not_called()

	def test_retirement_runs_after_native_close_and_outside_reservation_scope(self):
		events = []
		self.native_close.side_effect = lambda *_args: events.append("native_close") or "Closed"
		def retire(name, assignments):
			events.append("retire")
			self.assertEqual((name, assignments), ("WO-1", ["AS-2", "AS-3"]))
			self.assertIn(reporting._CLOSE_CONTINUATION_FLAG, frappe.flags)
			self.assertNotIn(reservation._CLOSE_SRE_VOUCHER_FLAG, frappe.flags)
		self.retire.side_effect = retire
		self.assertEqual(close.close_work_order("WO-1", "Closed"), "Closed")
		self.assertEqual(events, ["native_close", "retire"])

	def test_retirement_failure_propagates_and_restores_outer_scopes(self):
		outer = frappe._dict(work_order="OUTER-WO", company="Factory", user="manager@example.com",
			assignment_names=["OUTER-AS"])
		frappe.flags[reporting._CLOSE_CONTINUATION_FLAG] = outer
		self.retire.side_effect = frappe.ValidationError("Assignment save failed")
		with self.assertRaisesRegex(frappe.ValidationError, "Assignment save"):
			close.close_work_order("WO-1", "Closed")
		self.assertIs(frappe.flags[reporting._CLOSE_CONTINUATION_FLAG], outer)
		self.assertNotIn(reservation._CLOSE_SRE_VOUCHER_FLAG, frappe.flags)


class TestScopedContinuationClose(TestCase):
	def setUp(self):
		self.enterContext(patch.object(frappe, "flags", frappe._dict()))
		self.enterContext(patch.object(frappe, "session", SimpleNamespace(user="manager@example.com")))
		self.enterContext(patch.object(frappe, "throw", side_effect=throw))
		self.enterContext(patch.object(reporting, "_", lambda message: message))
		self.db = self.enterContext(patch.object(frappe, "db", Mock(), create=True))
		self.parent = frappe._dict(company="Factory", docstatus=1, status="Closed")
		self.db.get_value.side_effect = lambda _doctype, _name, fields, **_kwargs: (
			self.parent.status if fields == "status" else self.parent
		)
		self.doc = frappe._dict(name="AS-2", work_order="WO-1", company="Factory",
			status="Active", assigned_qty=2, flags=frappe._dict())
		self.doc.save = Mock()
		self.get_doc = self.enterContext(patch.object(frappe, "get_doc", return_value=self.doc))

	def close_scope(self):
		return reporting._allow_unstarted_continuation_close("WO-1", "Factory")

	def test_close_context_is_exact_and_restores_nested_scope_after_failure(self):
		with self.close_scope() as outer:
			self.assertEqual(dict(outer), dict(work_order="WO-1", company="Factory",
				user="manager@example.com", assignment_names=[]))
			with self.assertRaisesRegex(frappe.ValidationError, "Blocked"):
				with reporting._allow_unstarted_continuation_close("WO-2", "Other") as inner:
					self.assertIs(frappe.flags[reporting._CLOSE_CONTINUATION_FLAG], inner)
					raise frappe.ValidationError("Blocked")
			self.assertIs(frappe.flags[reporting._CLOSE_CONTINUATION_FLAG], outer)
		self.assertNotIn(reporting._CLOSE_CONTINUATION_FLAG, frappe.flags)

	def test_without_explicit_close_the_original_draft_guard_remains_strict(self):
		with patch.object(reporting, "assert_no_managed_draft_job_cards",
			side_effect=frappe.ValidationError("Draft continuation")) as strict:
			with self.assertRaisesRegex(frappe.ValidationError, "Draft continuation"):
				reporting.assert_work_order_close_allowed("WO-1")
			strict.assert_called_once_with("WO-1")
		self.db.get_value.assert_not_called()

	def test_scope_cannot_be_reused_for_another_work_order_actor_or_company(self):
		with self.close_scope():
			with self.assertRaises(frappe.PermissionError):
				reporting.assert_work_order_close_allowed("WO-2")
			frappe.session.user = "other@example.com"
			with self.assertRaises(frappe.PermissionError):
				reporting.assert_work_order_close_allowed("WO-1")
			frappe.session.user = "manager@example.com"
			self.parent.company = "Other Factory"
			with self.assertRaises(frappe.PermissionError):
				reporting.assert_work_order_close_allowed("WO-1")
		self.get_doc.assert_not_called()

	def test_only_registered_targets_of_a_closed_order_can_retire(self):
		with self.assertRaises(frappe.PermissionError):
			reporting.cancel_unstarted_continuation_assignments("WO-1", ["AS-2"])
		with self.close_scope() as scope:
			scope.assignment_names = ["AS-2"]
			with self.assertRaises(frappe.PermissionError):
				reporting.cancel_unstarted_continuation_assignments("WO-1", ["AS-3"])
			self.parent.status = "Stopped"
			with self.assertRaises(frappe.PermissionError):
				reporting.cancel_unstarted_continuation_assignments("WO-1", ["AS-2"])
		self.get_doc.assert_not_called()
		self.doc.save.assert_not_called()

	def test_retirement_preserves_quantity_and_uses_scoped_internal_save(self):
		def save(**kwargs):
			self.assertEqual(kwargs, {"ignore_permissions": True})
			self.assertTrue(self.doc.flags.worker_reporting_action)
			self.assertEqual(self.doc.status, "Cancelled")
			self.assertEqual(self.doc.assigned_qty, 2)
		self.doc.save.side_effect = save
		with self.close_scope() as scope:
			scope.assignment_names = ["AS-2"]
			reporting.cancel_unstarted_continuation_assignments("WO-1", ["AS-2"])
		self.get_doc.assert_called_once_with("Job Card Worker Assignment", "AS-2", for_update=True)
		self.doc.save.assert_called_once_with(ignore_permissions=True)
		self.assertEqual(self.doc.status, "Cancelled")
		self.assertEqual(self.doc.assigned_qty, 2)
		self.assertNotIn("worker_reporting_action", self.doc.flags)

	def test_save_failure_propagates_and_restores_existing_action_flag(self):
		self.doc.flags.worker_reporting_action = False
		self.doc.save.side_effect = frappe.ValidationError("Save failed")
		with self.close_scope() as scope:
			scope.assignment_names = ["AS-2"]
			with self.assertRaisesRegex(frappe.ValidationError, "Save failed"):
				reporting.cancel_unstarted_continuation_assignments("WO-1", ["AS-2"])
		self.assertIs(self.doc.flags.worker_reporting_action, False)
		self.assertNotIn(reporting._CLOSE_CONTINUATION_FLAG, frappe.flags)

	def test_changed_target_identity_or_completed_history_is_not_retired(self):
		for field, value in (("work_order", "WO-2"), ("company", "Other Factory"),
			("status", "Completed")):
			with self.subTest(field=field), self.close_scope() as scope:
				scope.assignment_names = ["AS-2"]
				original = self.doc[field]
				self.doc[field] = value
				with self.assertRaises(frappe.ValidationError):
					reporting.cancel_unstarted_continuation_assignments("WO-1", ["AS-2"])
				self.doc[field] = original
		self.doc.save.assert_not_called()


class NativeCancel:
	def cancel(self):
		self.native_cancel_calls += 1
		if not self.flags.get("ignore_permissions"):
			raise frappe.PermissionError("No generic SRE cancel permission")
		if self.native_failure:
			raise self.native_failure
		return "native cancellation"


class TestScopedReservationCancel(TestCase):
	def setUp(self):
		self.enterContext(patch.object(frappe, "flags", frappe._dict()))
		self.enterContext(patch.object(frappe, "session", SimpleNamespace(user="manager@example.com")))
		self.enterContext(patch.object(frappe, "throw", side_effect=throw))
		self.enterContext(patch.object(reservation, "_", lambda message: message))
		self.db = self.enterContext(patch.object(frappe, "db", Mock(), create=True))
		class SRE(reservation.GuidedStockReservationEntryMixin, NativeCancel, frappe._dict):
			pass
		self.doc = SRE(name="SRE-1", voucher_type="Work Order", voucher_no="WO-1", company="Factory",
			docstatus=1, flags=frappe._dict(), native_cancel_calls=0, native_failure=None)
		self.stored = frappe._dict(voucher_type="Work Order", voucher_no="WO-1", company="Factory", docstatus=1)
		self.parent = frappe._dict(company="Factory", docstatus=1, status="Closed")
		self.db.get_value.side_effect = lambda doctype, *_args, **_kwargs: (
			self.stored if doctype == "Stock Reservation Entry" else self.parent
		)

	def close_scope(self):
		return reservation._allow_work_order_close_reservations("WO-1", "Factory")

	def test_exact_reservation_uses_native_cancel_and_restores_document_flags(self):
		with self.close_scope():
			self.assertEqual(self.doc.cancel(), "native cancellation")
		self.assertEqual(self.doc.native_cancel_calls, 1)
		self.assertNotIn("ignore_permissions", self.doc.flags)
		self.assertNotIn(reservation._CLOSE_SRE_VOUCHER_FLAG, frappe.flags)

	def test_direct_or_creation_scope_cancel_keeps_native_permission_check(self):
		with self.assertRaises(frappe.PermissionError):
			self.doc.cancel()
		with reservation.allow_guided_stock_reservations_for("Work Order", "WO-1"):
			with self.assertRaises(frappe.PermissionError):
				self.doc.cancel()
		self.db.get_value.assert_not_called()

	def test_other_voucher_company_or_docstatus_is_not_a_derived_cancel(self):
		for field, value in (("voucher_no", "WO-2"), ("voucher_type", "Sales Order"),
			("company", "Other Factory"), ("docstatus", 0)):
			with self.subTest(field=field):
				original = self.doc[field]
				self.doc[field] = value
				with self.close_scope(), self.assertRaises(frappe.PermissionError):
					self.doc.cancel()
				self.doc[field] = original
		self.assertEqual(self.doc.native_cancel_calls, 0)

	def test_forged_in_memory_voucher_cannot_cancel_a_foreign_stored_row(self):
		self.stored.voucher_no = "OTHER-WO"
		with self.close_scope(), self.assertRaises(frappe.PermissionError):
			self.doc.cancel()
		self.assertEqual(self.doc.native_cancel_calls, 0)

	def test_stopped_unsubmitted_or_cross_company_parent_cannot_release(self):
		for changes in ({"status": "Stopped"}, {"docstatus": 0}, {"company": "Other Factory"}):
			with self.subTest(changes=changes):
				self.parent = frappe._dict(company="Factory", docstatus=1, status="Closed")
				self.parent.update(changes)
				with self.close_scope(), self.assertRaises(frappe.PermissionError):
					self.doc.cancel()
		self.assertEqual(self.doc.native_cancel_calls, 0)

	def test_actor_change_cannot_reuse_close_scope(self):
		with self.close_scope():
			frappe.session.user = "other@example.com"
			with self.assertRaises(frappe.PermissionError):
				self.doc.cancel()
		self.assertEqual(self.doc.native_cancel_calls, 0)

	def test_native_failure_restores_existing_false_flag_and_outer_scope(self):
		self.doc.flags.ignore_permissions = False
		self.doc.native_failure = frappe.ValidationError("Downstream reservation exists")
		outer = ("OUTER-WO", "Factory", "manager@example.com")
		frappe.flags[reservation._CLOSE_SRE_VOUCHER_FLAG] = outer
		with self.assertRaisesRegex(frappe.ValidationError, "Downstream"), self.close_scope():
			self.doc.cancel()
		self.assertIs(self.doc.flags.ignore_permissions, False)
		self.assertEqual(frappe.flags[reservation._CLOSE_SRE_VOUCHER_FLAG], outer)
