from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase


class TestProductionStockFacts(UnitTestCase):
	def test_scrap_and_quarantine_reduce_original_issue_coverage(self):
		from process_simplification.production_stock_facts import aggregate_work_order_stock_facts
		from process_simplification.production_workflow.service import _guided_component_issue_quantities
		for target in ("Stores", "Scrap", "Quarantine"):
			with self.subTest(target=target):
				rows = [
					frappe._dict(work_order="WO", purpose="Material Transfer for Manufacture", is_return=0,
						item_code="RM", s_warehouse="Stores", t_warehouse="WIP", transfer_qty=30, docstatus=1),
					frappe._dict(work_order="WO", purpose="Material Transfer for Manufacture", is_return=1,
						item_code="RM", s_warehouse="WIP", t_warehouse=target, transfer_qty=10, docstatus=1),
				]
				facts = aggregate_work_order_stock_facts(rows)
				self.assertEqual(facts[("WO", "RM", "Stores")].net_issued_qty, 20)
				wo = frappe._dict(name="WO", qty=30, required_items=[frappe._dict(
					item_code="RM", source_warehouse="Stores", required_qty=30, include_item_in_manufacturing=1)])
				self.assertEqual(_guided_component_issue_quantities(wo, 10, facts)[("RM", "Stores")].qty, 10)

	def test_cross_warehouse_returns_use_frozen_source_and_do_not_guess(self):
		from process_simplification.production_stock_facts import aggregate_work_order_stock_facts
		rows = [frappe._dict(work_order="WO", purpose="Material Transfer for Manufacture", is_return=0,
			item_code="RM", s_warehouse=source, t_warehouse="WIP", transfer_qty=10, docstatus=1)
			for source in ("Stores-A", "Stores-B")]
		returned = frappe._dict(work_order="WO", purpose="Material Transfer for Manufacture", is_return=1,
			item_code="RM", s_warehouse="WIP", t_warehouse="Scrap", transfer_qty=6, docstatus=1)
		with self.assertRaises(frappe.ValidationError):
			aggregate_work_order_stock_facts([returned, *rows])
		returned.custom_return_source_warehouse = "Stores-B"
		facts = aggregate_work_order_stock_facts([returned, *rows])
		self.assertEqual(facts[("WO", "RM", "Stores-A")].net_issued_qty, 10)
		self.assertEqual(facts[("WO", "RM", "Stores-B")].net_issued_qty, 4)

	@patch("process_simplification.production_stock_facts.frappe.get_all")
	def test_loader_reads_only_submitted_parent_and_child_rows(self, get_all):
		from process_simplification.production_stock_facts import (
			load_work_order_stock_facts,
		)

		get_all.side_effect = [
			[
				frappe._dict(
					name="STE-001", work_order="WO-A",
					purpose="Material Transfer for Manufacture", is_return=0, docstatus=1,
				)
			],
			[
				frappe._dict(
					parent="STE-001", docstatus=1, item_code="RM", original_item=None,
					s_warehouse="Stores-A", t_warehouse="WIP", transfer_qty=5,
				)
			],
		]

		facts = load_work_order_stock_facts(["WO-B", "WO-A", "WO-A"])

		self.assertEqual(facts[("WO-A", "RM", "Stores-A")].net_issued_qty, 5)
		parent_call, child_call = get_all.call_args_list
		self.assertEqual(parent_call.args[0], "Stock Entry")
		self.assertEqual(parent_call.kwargs["filters"]["docstatus"], 1)
		self.assertEqual(
			parent_call.kwargs["filters"]["work_order"], ["in", ["WO-A", "WO-B"]]
		)
		self.assertEqual(child_call.args[0], "Stock Entry Detail")
		self.assertEqual(
			child_call.kwargs["filters"],
			{"parent": ["in", ["STE-001"]], "docstatus": 1},
		)
		self.assertTrue(
			{
				"item_code", "original_item", "s_warehouse", "t_warehouse",
				"transfer_qty",
			}.issubset(child_call.kwargs["fields"])
		)

	def test_submitted_movements_keep_item_warehouse_and_return_direction(self):
		from process_simplification.production_stock_facts import (
			aggregate_work_order_stock_facts,
		)

		entries = [
			frappe._dict(
				name="STE-ISSUE", work_order="WO-001",
				purpose="Material Transfer for Manufacture", is_return=0, docstatus=1,
			),
			frappe._dict(
				name="STE-RETURN", work_order="WO-001",
				purpose="Material Transfer for Manufacture", is_return=1, docstatus=1,
			),
			frappe._dict(
				name="STE-CONSUME", work_order="WO-001",
				purpose="Manufacture", is_return=0, docstatus=1,
			),
			frappe._dict(
				name="STE-DRAFT", work_order="WO-001",
				purpose="Material Transfer for Manufacture", is_return=0, docstatus=0,
			),
		]
		details = [
			frappe._dict(
				parent="STE-ISSUE", docstatus=1, item_code="ALT-RM", original_item="RM",
				s_warehouse="Stores-A", t_warehouse="WIP", transfer_qty=6,
			),
			frappe._dict(
				parent="STE-RETURN", docstatus=1, item_code="ALT-RM", original_item="RM",
				s_warehouse="WIP", t_warehouse="Stores-A", transfer_qty=2,
			),
			frappe._dict(
				parent="STE-CONSUME", docstatus=1, item_code="RM",
				s_warehouse="WIP", transfer_qty=3,
			),
			frappe._dict(
				parent="STE-DRAFT", docstatus=0, item_code="RM",
				s_warehouse="Stores-B", t_warehouse="WIP", transfer_qty=99,
			),
		]

		facts = aggregate_work_order_stock_facts(details, entries)

		self.assertEqual(facts[("WO-001", "RM", "Stores-A")].gross_issued_qty, 6)
		self.assertEqual(facts[("WO-001", "RM", "Stores-A")].returned_qty, 2)
		self.assertEqual(facts[("WO-001", "RM", "Stores-A")].net_issued_qty, 4)
		self.assertEqual(facts[("WO-001", "RM", "WIP")].returned_out_qty, 2)
		self.assertEqual(facts[("WO-001", "RM", "WIP")].consumed_qty, 3)
		self.assertEqual(facts[("WO-001", "RM", "WIP")].consumed_release_qty, 5)
		self.assertNotIn(("WO-001", "RM", "Stores-B"), facts)


class TestProductionWorkflowQuantities(UnitTestCase):
	def test_receipt_remaining_qty_keeps_process_loss_out_of_finished_output(self):
		from process_simplification.production_workflow.service import receipt_remaining_qty

		self.assertEqual(
			receipt_remaining_qty(
				frappe._dict(qty=100, produced_qty=60, process_loss_qty=5, skip_transfer=1)
			),
			35,
		)

	def test_receipt_is_capped_by_formally_transferred_material(self):
		from process_simplification.production_workflow.service import receipt_remaining_qty

		self.assertEqual(
			receipt_remaining_qty(
				frappe._dict(
					qty=100,
					material_transferred_for_manufacturing=80,
					produced_qty=60,
					process_loss_qty=5,
					skip_transfer=0,
					required_items=[
						frappe._dict(
							item_code="RM-A",
							source_warehouse="Stores",
							required_qty=100,
							transferred_qty=80,
							returned_qty=0,
							include_item_in_manufacturing=1,
						)
					],
				)
			),
			15,
		)

	def test_issueable_qty_uses_the_limiting_direct_material(self):
		from process_simplification.production_workflow.service import issueable_finished_qty

		work_order = frappe._dict(
			qty=100,
			material_transferred_for_manufacturing=0,
			skip_transfer=0,
			required_items=[
				frappe._dict(
					item_code="RM-A",
					source_warehouse="Stores",
					required_qty=200,
					transferred_qty=0,
					returned_qty=0,
					include_item_in_manufacturing=1,
				),
				frappe._dict(
					item_code="RM-B",
					source_warehouse="Stores",
					required_qty=50,
					transferred_qty=0,
					returned_qty=0,
					include_item_in_manufacturing=1,
				),
			],
		)

		self.assertEqual(
			issueable_finished_qty(
				work_order,
				{("RM-A", "Stores"): 100, ("RM-B", "Stores"): 40},
			),
			50,
		)

	def test_submitted_issue_fact_does_not_cross_source_warehouses(self):
		from process_simplification.production_workflow.service import (
			_material_issue_coverage,
		)

		work_order = frappe._dict(
			name="WO-001",
			qty=10,
			required_items=[
				frappe._dict(
					item_code="RM", source_warehouse="Stores-A", required_qty=5,
					transferred_qty=5, include_item_in_manufacturing=1,
				),
				frappe._dict(
					item_code="RM", source_warehouse="Stores-B", required_qty=5,
					transferred_qty=5, include_item_in_manufacturing=1,
				),
			],
		)
		stock_facts = {
			("WO-001", "RM", "Stores-A"): frappe._dict(
				gross_issued_qty=5, returned_qty=0, net_issued_qty=5,
			),
		}

		current, potential = _material_issue_coverage(
			work_order,
			{("RM", "Stores-B"): 5},
			stock_facts,
		)

		self.assertEqual(current, 0)
		self.assertEqual(potential, 1)

	def test_returned_material_reopens_the_issue_gap(self):
		from process_simplification.production_workflow.service import (
			issueable_finished_qty,
			remaining_material_issue_qty,
		)

		work_order = frappe._dict(
			qty=100,
			material_transferred_for_manufacturing=90,
			skip_transfer=0,
			required_items=[
				frappe._dict(
					item_code="RM-A",
					source_warehouse="Stores",
					required_qty=100,
					transferred_qty=90,
					returned_qty=10,
					include_item_in_manufacturing=1,
				)
			],
		)

		# Net issued is 80, so 10 available units restore another 10 FG-equivalent units.
		self.assertAlmostEqual(
			issueable_finished_qty(work_order, {("RM-A", "Stores"): 10}),
			10,
		)
		self.assertAlmostEqual(remaining_material_issue_qty(work_order), 20)

	def test_returned_component_rows_are_rebuilt_from_net_coverage(self):
		from process_simplification.production_workflow.service import (
			_guided_component_issue_quantities,
		)

		work_order = frappe._dict(
			qty=100,
			required_items=[
				frappe._dict(
					item_code="RM-A",
					source_warehouse="Stores",
					required_qty=100,
					transferred_qty=100,
					returned_qty=20,
					include_item_in_manufacturing=1,
				),
				frappe._dict(
					item_code="RM-B",
					source_warehouse="Stores",
					required_qty=50,
					transferred_qty=50,
					returned_qty=0,
					include_item_in_manufacturing=1,
				),
			],
		)

		rows = _guided_component_issue_quantities(work_order, 20)
		self.assertEqual(rows[("RM-A", "Stores")].qty, 20)
		self.assertNotIn(("RM-B", "Stores"), rows)

	def test_guided_issue_rows_must_target_the_work_order_wip(self):
		from process_simplification.production_workflow.service import (
			_validate_guided_issue_targets,
		)

		with self.assertRaises(frappe.ValidationError):
			_validate_guided_issue_targets(
				frappe._dict(items=[frappe._dict(t_warehouse="Other")]),
				frappe._dict(wip_warehouse="WIP"),
			)

	def test_guided_receipt_main_output_must_match_item_qty_and_fg_warehouse(self):
		from process_simplification.production_workflow.service import (
			_validate_guided_receipt_rows,
		)

		work_order = frappe._dict(production_item="FG-001", fg_warehouse="FG")
		valid = frappe._dict(
			fg_completed_qty=10,
			process_loss_qty=1,
			items=[
				frappe._dict(
					item_code="FG-001",
					is_finished_item=1,
					transfer_qty=9,
					t_warehouse="FG",
					s_warehouse=None,
				)
			],
		)
		self.assertIsNone(_validate_guided_receipt_rows(valid, work_order))

		invalid_warehouse = frappe._dict(valid)
		invalid_warehouse["items"] = [frappe._dict(dict(valid["items"][0]), t_warehouse="Other")]
		with self.assertRaises(frappe.ValidationError):
			_validate_guided_receipt_rows(invalid_warehouse, work_order)

		invalid_qty = frappe._dict(valid)
		invalid_qty["items"] = [frappe._dict(dict(valid["items"][0]), transfer_qty=8)]
		with self.assertRaises(frappe.ValidationError):
			_validate_guided_receipt_rows(invalid_qty, work_order)

	@patch("process_simplification.production_workflow.service.make_stock_entry")
	@patch("process_simplification.production_workflow.service.frappe.get_doc")
	def test_return_reissue_uses_zero_native_header_and_keeps_business_coverage(
		self, get_doc, make_entry
	):
		from process_simplification.production_workflow.service import (
			_insert_guided_issue_stock_entry,
		)

		class FakeEntry(frappe._dict):
			def set(self, fieldname, value):
				self[fieldname] = value

			def add_to_stock_entry_detail(self, rows):
				for details in rows.values():
					self["items"].append(frappe._dict(details))

			def insert(self, **kwargs):
				self.insert_kwargs = kwargs

		entry = FakeEntry(items=[], fg_completed_qty=20)
		get_doc.return_value = entry
		make_entry.return_value = frappe._dict(doctype="Stock Entry")
		work_order = frappe._dict(
			name="WO-001",
			qty=100,
			wip_warehouse="WIP",
			required_items=[
				frappe._dict(
					item_code="RM-A",
					source_warehouse="Stores",
					required_qty=100,
					transferred_qty=100,
					returned_qty=20,
					include_item_in_manufacturing=1,
					stock_uom="Nos",
				)
			],
		)

		result = _insert_guided_issue_stock_entry(
			work_order,
			20,
			{
				("WO-001", "RM-A", "Stores"): frappe._dict(
					gross_issued_qty=100,
					returned_qty=20,
					net_issued_qty=80,
				)
			},
		)

		self.assertIs(result, entry)
		self.assertEqual(entry.fg_completed_qty, 0)
		self.assertEqual(entry.custom_process_coverage_qty, 20)
		self.assertEqual(entry["items"][0].qty, 20)
		self.assertTrue(make_entry.call_args.kwargs["is_additional_transfer_entry"])

	@patch("process_simplification.production_workflow.service.frappe.get_doc")
	def test_worker_dispatch_is_blocked_after_material_is_returned(self, get_doc):
		from process_simplification.production_workflow.service import (
			assert_work_order_dispatch_ready,
			get_work_order_dispatch_state,
		)

		get_doc.return_value = frappe._dict(
			skip_transfer=0,
			required_items=[
				frappe._dict(
					item_code="RM-A",
					required_qty=10,
					transferred_qty=10,
					returned_qty=2,
					include_item_in_manufacturing=1,
				)
			],
		)
		state = get_work_order_dispatch_state("WO-001")
		self.assertFalse(state.can_dispatch)
		self.assertEqual(state.block_code, "MATERIAL_NOT_FULLY_ISSUED")
		with self.assertRaises(frappe.ValidationError):
			assert_work_order_dispatch_ready("WO-001")

	@patch("process_simplification.production_workflow.service.frappe.get_doc")
	def test_worker_dispatch_is_allowed_after_full_net_issue(self, get_doc):
		from process_simplification.production_workflow.service import (
			assert_work_order_dispatch_ready,
			get_work_order_dispatch_state,
		)

		get_doc.return_value = frappe._dict(
			skip_transfer=0,
			required_items=[
				frappe._dict(
					required_qty=10,
					transferred_qty=12,
					returned_qty=2,
					include_item_in_manufacturing=1,
				)
			],
		)
		self.assertTrue(get_work_order_dispatch_state("WO-001").can_dispatch)
		self.assertIsNone(assert_work_order_dispatch_ready("WO-001"))

	@patch("process_simplification.production_workflow.service.frappe.get_doc")
	def test_worker_dispatch_groups_operation_split_component_rows(self, get_doc):
		from process_simplification.production_workflow.service import (
			assert_work_order_dispatch_ready,
		)

		get_doc.return_value = frappe._dict(
			skip_transfer=0,
			required_items=[
				frappe._dict(
					item_code="RM-A",
					source_warehouse="Stores",
					required_qty=5,
					transferred_qty=5,
					returned_qty=0,
					include_item_in_manufacturing=1,
				),
				frappe._dict(
					item_code="RM-A",
					source_warehouse="Stores",
					required_qty=5,
					transferred_qty=5,
					returned_qty=0,
					include_item_in_manufacturing=1,
				),
			],
		)
		with self.assertRaises(frappe.ValidationError):
			assert_work_order_dispatch_ready("WO-001")

	@patch(
		"process_simplification.production_workflow.service._active_work_order_item_reserved_qty",
		return_value=0,
	)
	@patch(
		"process_simplification.production_workflow.service.get_available_qty_to_reserve",
		return_value=4,
	)
	@patch("process_simplification.production_workflow.service.frappe.get_doc")
	def test_direct_consumption_dispatch_is_blocked_when_source_stock_was_taken(
		self, get_doc, get_available, active_reserved_qty
	):
		from process_simplification.production_workflow.service import (
			assert_work_order_dispatch_ready,
		)

		get_doc.return_value = frappe._dict(
			skip_transfer=1,
			required_items=[
				frappe._dict(
					name="WOI-001",
					parent="WO-001",
					item_code="RM-A",
					source_warehouse="Stores",
					required_qty=10,
					consumed_qty=2,
					include_item_in_manufacturing=1,
				)
			],
		)
		with self.assertRaises(frappe.ValidationError):
			assert_work_order_dispatch_ready("WO-001")
		get_available.assert_called_once_with("RM-A", "Stores")
		active_reserved_qty.assert_called_once()

	@patch(
		"process_simplification.production_workflow.service._active_work_order_item_reserved_qty",
		return_value=3,
	)
	@patch("process_simplification.production_workflow.service._require_stock_reservation_enabled")
	@patch("process_simplification.production_workflow.service._new_work_order_reservation")
	@patch(
		"process_simplification.production_workflow.service.get_available_qty_to_reserve",
		return_value=5,
	)
	@patch("process_simplification.production_workflow.service.frappe.get_doc")
	def test_direct_consumption_dispatch_counts_its_own_hard_reservation(
		self,
		get_doc,
		get_available,
		new_reservation,
		require_reservation,
		active_reserved_qty,
	):
		from process_simplification.production_workflow.service import (
			assert_work_order_dispatch_ready,
		)

		get_doc.return_value = frappe._dict(
			skip_transfer=1,
			required_items=[
				frappe._dict(
					name="WOI-001",
					parent="WO-001",
					item_code="RM-A",
					source_warehouse="Stores",
					required_qty=10,
					consumed_qty=2,
					include_item_in_manufacturing=1,
				)
			],
		)
		new_reservation.return_value = frappe._dict(reserved_qty=5)
		self.assertIsNone(assert_work_order_dispatch_ready("WO-001"))
		require_reservation.assert_called_once()
		self.assertEqual(new_reservation.call_args.kwargs["qty"], 5)
		self.assertEqual(new_reservation.call_args.kwargs["voucher_qty"], 10)

	@patch(
		"process_simplification.production_workflow.service._active_work_order_item_reserved_qty",
		return_value=0,
	)
	@patch(
		"process_simplification.production_workflow.service.load_work_order_stock_facts",
		return_value={
			("WO-001", "RM-A", "WIP"): frappe._dict(consumed_qty=2),
		},
	)
	@patch("process_simplification.production_workflow.service._require_stock_reservation_enabled")
	@patch("process_simplification.production_workflow.service._new_work_order_reservation")
	@patch(
		"process_simplification.production_workflow.service.get_available_qty_to_reserve",
		return_value=8,
	)
	@patch("process_simplification.production_workflow.service.frappe.get_doc")
	def test_direct_consumption_from_wip_checks_and_locks_the_wip_warehouse(
		self,
		get_doc,
		get_available,
		new_reservation,
		require_reservation,
		load_stock_facts,
		active_reserved_qty,
	):
		from process_simplification.production_workflow.service import (
			assert_work_order_dispatch_ready,
		)

		item = frappe._dict(
			name="WOI-001",
			parent="WO-001",
			item_code="RM-A",
			source_warehouse="Stores",
			required_qty=10,
			consumed_qty=2,
			include_item_in_manufacturing=1,
		)
		get_doc.return_value = frappe._dict(
			name="WO-001",
			company="Factory",
			skip_transfer=1,
			from_wip_warehouse=1,
			wip_warehouse="WIP",
			required_items=[item],
		)
		new_reservation.return_value = frappe._dict(reserved_qty=8)

		self.assertIsNone(assert_work_order_dispatch_ready("WO-001"))
		get_available.assert_called_once_with("RM-A", "WIP")
		active_reserved_qty.assert_called_once_with(item, "WIP")
		self.assertEqual(new_reservation.call_args.kwargs["warehouse"], "WIP")
		require_reservation.assert_called_once()
		load_stock_facts.assert_called_once_with(["WO-001"])


class TestProductionWorkflowIdempotency(UnitTestCase):
	def test_guided_sre_permission_is_exact_voucher_and_request_scoped(self):
		from process_simplification.production_workflow.stock_reservation import (
			GuidedStockReservationEntryMixin,
			allow_guided_stock_reservations_for,
		)

		class Parent:
			def save(self, *args, **kwargs):
				return kwargs

			def submit(self):
				return bool(self.flags.get("ignore_permissions"))

		class FakeSRE(GuidedStockReservationEntryMixin, Parent):
			def __init__(self, voucher_type, voucher_no):
				self.voucher_type = voucher_type
				self.voucher_no = voucher_no
				self.flags = frappe._dict()

			def get(self, fieldname):
				return getattr(self, fieldname, None)

		allowed = FakeSRE("Production Plan", "PP-001")
		other = FakeSRE("Production Plan", "PP-OTHER")
		with allow_guided_stock_reservations_for("Production Plan", "PP-001"):
			self.assertTrue(allowed.save()["ignore_permissions"])
			self.assertTrue(allowed.submit())
			self.assertNotIn("ignore_permissions", other.save())
			self.assertFalse(other.submit())

		allowed.flags.pop("ignore_permissions", None)
		self.assertNotIn("ignore_permissions", allowed.save())
		self.assertFalse(allowed.submit())

	@patch("process_simplification.production_workflow.service.frappe.get_doc")
	@patch("process_simplification.production_workflow.service.frappe.get_all")
	def test_request_reservation_release_uses_narrow_internal_cancel(self, get_all, get_doc):
		from process_simplification.production_workflow.service import (
			release_issue_request_reservations,
		)

		reservation = frappe._dict(flags=frappe._dict())
		reservation.cancel = MagicMock()
		get_all.return_value = ["SRE-001"]
		get_doc.return_value = reservation

		release_issue_request_reservations(
			frappe._dict(
				name="STE-001",
				custom_process_workflow_action="Material Issue Request",
			)
		)

		self.assertTrue(reservation.flags.ignore_permissions)
		reservation.cancel.assert_called_once_with()

	@patch("process_simplification.production_workflow.service.frappe.get_all")
	def test_unmanaged_stock_draft_is_never_adopted_as_a_guided_request(self, get_all):
		from process_simplification.production_workflow.service import (
			_existing_draft_stock_entry,
		)

		get_all.return_value = [
			frappe._dict(name="STE-MANUAL", custom_process_workflow_action=None)
		]
		with self.assertRaises(frappe.ValidationError):
			_existing_draft_stock_entry(
				"WO-001", "Material Transfer for Manufacture", "Material Issue Request"
			)

	@patch("process_simplification.production_workflow.service._new_work_order_reservation")
	@patch(
		"process_simplification.production_workflow.service._active_work_order_item_reserved_qty",
		return_value=5,
	)
	def test_issue_request_only_reserves_the_uncovered_part(
		self, active_reserved_qty, new_reservation
	):
		from process_simplification.production_workflow.service import (
			_reserve_issue_request_rows,
		)

		new_reservation.return_value = frappe._dict(reserved_qty=5)
		work_order_item = frappe._dict(
			name="WOI-001",
			idx=1,
			parent="WO-001",
			item_code="SEMI",
			source_warehouse="Stores",
			required_qty=10,
			include_item_in_manufacturing=1,
		)
		work_order = frappe._dict(
			name="WO-001",
			required_items=[work_order_item],
		)
		stock_entry = frappe._dict(
			name="STE-001",
			items=[
				frappe._dict(
					name="SED-001",
					item_code="SEMI",
					s_warehouse="Stores",
					transfer_qty=10,
				)
			],
		)

		_reserve_issue_request_rows(stock_entry, work_order)
		self.assertEqual(new_reservation.call_args.kwargs["qty"], 5)

	@patch(
		"process_simplification.production_workflow.service._existing_draft_stock_entry",
		return_value=frappe._dict(name="STE-EXISTING"),
	)
	@patch(
		"process_simplification.production_workflow.service.validate_guided_stock_entry_before_submit"
	)
	@patch("process_simplification.production_workflow.service.frappe.get_doc")
	@patch("process_simplification.production_workflow.service._work_order_for_action")
	def test_receipt_request_reuses_an_existing_draft(
		self, get_work_order, get_stock_entry, validate_request, get_draft
	):
		from process_simplification.production_workflow.service import request_manufacture

		get_work_order.return_value = frappe._dict(name="WO-001", status="In Process")
		get_stock_entry.return_value = frappe._dict(name="STE-EXISTING")
		self.assertEqual(
			request_manufacture("WO-001"),
			{"stock_entry": "STE-EXISTING", "reused": True},
		)
		get_draft.assert_called_once_with("WO-001", "Manufacture", "Receipt Request")
		validate_request.assert_called_once_with(get_stock_entry.return_value)

	@patch(
		"process_simplification.api.production_readiness.get_production_plan_readiness"
	)
	def test_priority_cap_comes_from_server_readiness_provenance(self, get_readiness):
		from process_simplification.production_workflow.service import (
			_production_priority_issue_cap,
		)

		get_readiness.return_value = {
			"SOI-1": [
				{
					"work_orders": [
						{
							"name": "WO-LOW",
							"issue_state": {"additional_issueable_qty": 4},
							"required_items": [
								{
									"item_code": "RM-A",
									"source_warehouse": "Stores",
									"allocation_conflict": {
										"sources": [
											{
												"work_order": "WO-URGENT",
												"impact_qty": 6,
											}
										]
									},
								}
							],
						}
					]
				}
			]
		}
		result = _production_priority_issue_cap(
			frappe._dict(name="WO-LOW", production_plan="PP-1", company="Factory")
		)
		self.assertEqual(result.cap, 4)
		self.assertEqual(result.conflicts[0]["work_order"], "WO-URGENT")

	@patch("process_simplification.production_workflow.service.frappe.db.get_value")
	def test_guided_request_cannot_be_relinked_to_another_work_order(self, get_value):
		from process_simplification.production_workflow.service import (
			validate_guided_stock_entry_identity,
		)

		get_value.return_value = frappe._dict(
			custom_process_workflow_action="Receipt Request",
			custom_process_requested_by="manager@example.com",
			work_order="WO-A",
			company="Factory",
			purpose="Manufacture",
			is_return=0,
		)
		draft = frappe._dict(
			name="STE-1",
			custom_process_workflow_action="Receipt Request",
			custom_process_requested_by="manager@example.com",
			work_order="WO-B",
			company="Factory",
			purpose="Manufacture",
			is_return=0,
			is_new=lambda: False,
		)
		with self.assertRaises(frappe.ValidationError):
			validate_guided_stock_entry_identity(draft)


class TestReplenishmentReservation(UnitTestCase):
	@patch("process_simplification.production_workflow.service._new_work_order_reservation")
	@patch("process_simplification.production_workflow.service.frappe.db.get_value")
	def test_terminal_target_receives_stock_without_a_stale_reservation(
		self, get_value, new_reservation
	):
		from process_simplification.production_workflow.service import (
			reserve_replenishment_output,
		)

		get_value.side_effect = [
			frappe._dict(
				name="WO-SUPPLY",
				company="Factory",
				production_item="SEMI",
				custom_replenishes_work_order="WO-TARGET",
				custom_replenishes_work_order_item="WOI-TARGET",
			),
			frappe._dict(
				name="WO-TARGET", docstatus=1, status="Completed", company="Factory"
			),
		]
		stock_entry = frappe._dict(
			name="STE-SUPPLY",
			purpose="Manufacture",
			work_order="WO-SUPPLY",
			items=[
				frappe._dict(
					name="SED-FG",
					item_code="SEMI",
					is_finished_item=1,
					transfer_qty=5,
					t_warehouse="Stores",
				)
			],
		)

		result = reserve_replenishment_output(stock_entry)
		self.assertEqual(result.reason, "target_terminal")
		self.assertEqual(result.reserved_qty, 0)
		self.assertEqual(result.surplus_qty, 5)
		new_reservation.assert_not_called()

	@patch(
		"process_simplification.production_workflow.service._target_group_gap",
		return_value=(10, 10),
	)
	@patch("process_simplification.production_workflow.service._target_work_order_item_group")
	@patch("process_simplification.production_workflow.service._target_work_order_item")
	@patch("process_simplification.production_workflow.service._new_work_order_reservation")
	@patch("process_simplification.production_workflow.service.frappe.get_doc")
	@patch("process_simplification.production_workflow.service.frappe.db.get_value")
	def test_replenishment_reserves_all_matching_finished_rows(
		self,
		get_value,
		get_doc,
		new_reservation,
		get_target_item,
		get_target_items,
		_target_gap,
	):
		from process_simplification.production_workflow.service import (
			reserve_replenishment_output,
		)

		get_value.side_effect = [
			frappe._dict(
				name="WO-SUPPLY",
				company="Factory",
				production_item="SEMI",
				custom_replenishes_work_order="WO-TARGET",
				custom_replenishes_work_order_item="WOI-TARGET",
			),
			frappe._dict(
				name="WO-TARGET", docstatus=1, status="In Process", company="Factory"
			),
		]
		target_work_order = frappe._dict(name="WO-TARGET")
		target_item = frappe._dict(
			name="WOI-TARGET",
			item_code="SEMI",
			source_warehouse="Stores",
		)
		get_doc.return_value = target_work_order
		get_target_item.return_value = target_item
		get_target_items.return_value = [target_item]
		new_reservation.side_effect = [
			frappe._dict(reserved_qty=3),
			frappe._dict(reserved_qty=4),
		]
		stock_entry = frappe._dict(
			name="STE-SUPPLY",
			purpose="Manufacture",
			work_order="WO-SUPPLY",
			items=[
				frappe._dict(
					name="SED-FG-1", item_code="SEMI", is_finished_item=1,
					transfer_qty=3, t_warehouse="Stores",
				),
				frappe._dict(
					name="SED-FG-2", item_code="SEMI", is_finished_item=1,
					transfer_qty=4, t_warehouse="Stores",
				),
			],
		)

		result = reserve_replenishment_output(stock_entry)

		self.assertEqual(result.received_qty, 7)
		self.assertEqual(result.reserved_qty, 7)
		self.assertEqual(result.surplus_qty, 0)
		self.assertEqual(new_reservation.call_count, 2)
		self.assertEqual(
			[call.kwargs["qty"] for call in new_reservation.call_args_list],
			[3, 4],
		)
		self.assertEqual(
			[call.kwargs["from_detail"] for call in new_reservation.call_args_list],
			["SED-FG-1", "SED-FG-2"],
		)

	@patch(
		"process_simplification.production_workflow.service._existing_draft_stock_entry",
		return_value=frappe._dict(name="STE-ISSUE"),
	)
	@patch(
		"process_simplification.production_workflow.service.validate_guided_stock_entry_before_submit"
	)
	@patch("process_simplification.production_workflow.service.frappe.get_doc")
	@patch("process_simplification.production_workflow.service._work_order_for_action")
	def test_issue_request_reuses_an_existing_draft(
		self, get_work_order, get_stock_entry, validate_request, get_draft
	):
		from process_simplification.production_workflow.service import request_material_issue

		get_work_order.return_value = frappe._dict(name="WO-001", status="Not Started")
		get_stock_entry.return_value = frappe._dict(name="STE-ISSUE")
		self.assertEqual(
			request_material_issue("WO-001"),
			{"stock_entry": "STE-ISSUE", "reused": True},
		)
		get_draft.assert_called_once_with(
			"WO-001", "Material Transfer for Manufacture", "Material Issue Request"
		)
		validate_request.assert_called_once_with(get_stock_entry.return_value)
