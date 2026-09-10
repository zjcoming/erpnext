from __future__ import annotations

from collections import Counter, defaultdict

import frappe
from frappe.utils import flt

from process_simplification.production_stock_facts import (
	get_work_order_stock_fact,
	load_work_order_stock_facts,
)

MANUFACTURING_STOCK_ENTRY_PURPOSES = {
	"Manufacture",
	"Material Consumption for Manufacture",
	"Material Transfer for Manufacture",
}
CONSUMPTION_STOCK_ENTRY_PURPOSES = {
	"Manufacture",
	"Material Consumption for Manufacture",
}
QTY_EPSILON = 1e-9


def before_submit(doc, method=None):
	from process_simplification.production_exceptions.service import validate_linked_stock_entry

	validate_linked_stock_entry(doc)
	from process_simplification.production_workflow.service import (
		mark_replenishment_receipt,
		validate_guided_stock_entry_before_submit,
	)

	mark_replenishment_receipt(doc)
	validate_guided_stock_entry_before_submit(doc)
	# Finished-goods posting is the only native path that can make a Work Order
	# Completed. Do not let it strand a Draft Job Card with immutable wage history.
	if doc.get("purpose") != "Manufacture" or not doc.get("work_order"):
		return
	from process_simplification.production_reporting.work_order import (
		assert_no_managed_draft_job_cards,
	)

	assert_no_managed_draft_job_cards(doc.work_order)


def on_trash(doc, method=None):
	from process_simplification.production_exceptions.service import (
		prevent_linked_stock_entry_delete,
	)

	prevent_linked_stock_entry_delete(doc)
	from process_simplification.production_workflow.service import (
		release_issue_request_reservations,
	)

	release_issue_request_reservations(doc)


def _submitted_sre_status(entry) -> str:
	"""Mirror ERPNext v16's submitted Stock Reservation Entry status rules."""
	entry = frappe._dict(entry or {})
	reserved_qty = flt(entry.get("reserved_qty"))
	transferred_qty = flt(entry.get("transferred_qty"))
	delivered_qty = flt(entry.get("delivered_qty"))
	consumed_qty = flt(entry.get("consumed_qty"))

	if transferred_qty > QTY_EPSILON:
		return "Closed" if transferred_qty + QTY_EPSILON >= reserved_qty else "Partially Used"
	if abs(reserved_qty - (delivered_qty or consumed_qty)) <= QTY_EPSILON:
		return "Delivered"
	if delivered_qty > QTY_EPSILON and delivered_qty < reserved_qty:
		return "Partially Delivered"
	if reserved_qty + QTY_EPSILON >= flt(entry.get("voucher_qty")):
		return "Reserved"
	return "Partially Reserved"


def _allocate_sre_group_quantity(entries, fieldname, target_qty):
	"""Distribute one item-level quantity once across its reservation entries."""
	remaining_qty = max(flt(target_qty), 0)
	other_fieldname = "consumed_qty" if fieldname == "transferred_qty" else "transferred_qty"
	ordered_entries = sorted(
		entries,
		key=lambda row: (str(row.get("creation") or ""), str(row.get("name") or "")),
	)
	serial_batch_entries = [
		entry
		for entry in ordered_entries
		if entry.get("reservation_based_on") == "Serial and Batch"
		and entry.get("serial_batch_used_qty") is not None
	]
	serial_batch_names = {entry.get("name") for entry in serial_batch_entries}
	qty_entries = [
		entry for entry in ordered_entries if entry.get("name") not in serial_batch_names
	]
	for entry in [*serial_batch_entries, *qty_entries]:
		capacity = max(
			flt(entry.get("reserved_qty"))
			- flt(entry.get("delivered_qty"))
			- flt(entry.get(other_fieldname)),
			0,
		)
		requested_qty = remaining_qty
		if entry.get("name") in serial_batch_names:
			requested_qty = min(requested_qty, flt(entry.get("serial_batch_used_qty")))
		entry[fieldname] = min(requested_qty, capacity)
		remaining_qty = max(remaining_qty - flt(entry.get(fieldname)), 0)


def _work_order_item_reservation_warehouse(work_order, item):
	if work_order.get("skip_transfer") and work_order.get("from_wip_warehouse"):
		return work_order.get("wip_warehouse")
	return item.get("source_warehouse")


def build_work_order_sre_reconciliation(
	work_order,
	required_items,
	stock_reservation_entries,
	stock_facts=None,
):
	"""Plan group-correct SRE values for Work Order material rows.

	ERPNext stores transferred and consumed quantities aggregated by item code on
	every matching Work Order Item. Applying that aggregate independently to each
	row's SRE overstates usage. Reallocate it once across the exact item/source
	group, including Closed entries so cancellation can reopen them.
	"""
	work_order = frappe._dict(work_order or {})
	facts_loaded = stock_facts is not None
	work_order_name = work_order.get("name")
	items_by_group = defaultdict(list)
	for source in required_items or []:
		item = frappe._dict(source)
		if not item.get("name") or not item.get("item_code"):
			continue
		reservation_warehouse = _work_order_item_reservation_warehouse(work_order, item)
		items_by_group[(item.get("item_code"), reservation_warehouse)].append(item)

	entries_by_name = {
		entry.get("name"): frappe._dict(dict(entry))
		for entry in stock_reservation_entries or []
		if entry.get("name")
	}
	touched_names = set()
	for (item_code, source_warehouse), items in items_by_group.items():
		detail_names = {item.get("name") for item in items}
		group_entries = [
			entry
			for entry in entries_by_name.values()
			if entry.get("voucher_detail_no") in detail_names
			and entry.get("item_code") == item_code
		]

		# Initial, request-created, and replenishment-created reservations can all
		# point back to different voucher types. Their role is determined by the
		# exact Work Order source/effective warehouse, not by provenance alone.
		transfer_entries = [
			entry
			for entry in group_entries
			if entry.get("warehouse") == source_warehouse
		]
		if work_order.get("skip_transfer"):
			for entry in transfer_entries:
				entry.transferred_qty = 0
		else:
			transfer_target = (
				flt(
					get_work_order_stock_fact(
						stock_facts,
						work_order_name,
						item_code,
						source_warehouse,
					).gross_issued_qty
				)
				if facts_loaded
				else max((flt(item.get("transferred_qty")) for item in items), default=0)
			)
			_allocate_sre_group_quantity(
				transfer_entries,
				"transferred_qty",
				transfer_target,
			)
		touched_names.update(entry.get("name") for entry in transfer_entries)

		consumption_warehouse = (
			work_order.get("wip_warehouse")
			if not work_order.get("skip_transfer") or work_order.get("from_wip_warehouse")
			else source_warehouse
		)
		consumption_entries = [
			entry
			for entry in group_entries
			if entry.get("warehouse") == consumption_warehouse
			and (
				work_order.get("skip_transfer")
				or entry.get("from_voucher_type") == "Stock Entry"
			)
		]
		consumption_target = (
			flt(
				get_work_order_stock_fact(
					stock_facts,
					work_order_name,
					item_code,
					consumption_warehouse,
				).consumed_release_qty
			)
			if facts_loaded
			else max(
				(flt(item.get("consumed_qty")) for item in items),
				default=0,
			)
		)
		_allocate_sre_group_quantity(
			consumption_entries,
			"consumed_qty",
			consumption_target,
		)
		touched_names.update(entry.get("name") for entry in consumption_entries)

	updates = []
	for name in sorted(touched_names):
		entry = entries_by_name[name]
		entry.status = _submitted_sre_status(entry)
		updates.append(
			frappe._dict(
				name=name,
				voucher_detail_no=entry.get("voucher_detail_no"),
				item_code=entry.get("item_code"),
				warehouse=entry.get("warehouse"),
				transferred_qty=flt(entry.get("transferred_qty")),
				consumed_qty=flt(entry.get("consumed_qty")),
				status=entry.status,
			)
		)
	return updates


def _refresh_serial_batch_reservation_usage(
	work_order,
	required_items,
	stock_reservation_entries,
):
	"""Rebuild SRE child usage from currently submitted manufacturing entries."""
	serial_entries = [
		entry
		for entry in stock_reservation_entries or []
		if entry.get("reservation_based_on") == "Serial and Batch"
	]
	if not serial_entries:
		return {}

	work_order = frappe._dict(work_order or {})
	item_by_name = {item.get("name"): frappe._dict(item) for item in required_items or []}
	entries_by_usage_type = defaultdict(list)
	documents = {}
	for entry in serial_entries:
		# A fresh query never contains this transient key, but callers can reuse
		# dictionaries in tests or hook retries. Empty child reservations must fall
		# back to quantity allocation rather than retaining an earlier zero.
		entry.pop("serial_batch_used_qty", None)
		item = item_by_name.get(entry.get("voucher_detail_no"))
		if not item:
			continue
		reservation_warehouse = _work_order_item_reservation_warehouse(work_order, item)
		if work_order.get("skip_transfer"):
			if entry.get("warehouse") != reservation_warehouse:
				continue
			usage_type = "consumption"
		elif entry.get("warehouse") == reservation_warehouse:
			usage_type = "transfer"
		elif (
			entry.get("warehouse") == work_order.get("wip_warehouse")
			and entry.get("from_voucher_type") == "Stock Entry"
		):
			usage_type = "consumption"
		else:
			continue

		doc = frappe.get_doc("Stock Reservation Entry", entry.get("name"))
		documents[entry.get("name")] = doc
		# Old Qty reservations can be marked Serial and Batch without any child
		# details. In that case the header quantity is the only usable fact.
		if doc.get("sb_entries"):
			entries_by_usage_type[usage_type].append((entry, doc))

	for usage_type, entry_documents in entries_by_usage_type.items():
		row_wise_usage = _get_work_order_serial_batch_usage(
			work_order.get("name"),
			usage_type,
		)
		used_qty_by_sre = _rebuild_serial_batch_child_usage(
			entry_documents,
			row_wise_usage,
		)
		for entry, doc in entry_documents:
			entry["serial_batch_used_qty"] = used_qty_by_sre.get(entry.get("name"), 0)
	return documents


def _get_work_order_serial_batch_usage(work_order, usage_type):
	"""Return current submitted outward serial/batch facts for one usage stage."""
	if usage_type == "consumption":
		stock_entries = set(
			frappe.get_all(
				"Stock Entry",
				filters={
					"work_order": work_order,
					"purpose": ["in", sorted(CONSUMPTION_STOCK_ENTRY_PURPOSES)],
					"docstatus": 1,
				},
				pluck="name",
			)
		)
		# ERPNext adds Material Transfer returns to Work Order Item.consumed_qty.
		# Their actual outward serial/batch rows leave WIP and therefore belong to
		# the consumption reservation child usage as well.
		stock_entries.update(
			frappe.get_all(
				"Stock Entry",
				filters={
					"work_order": work_order,
					"purpose": "Material Transfer for Manufacture",
					"is_return": 1,
					"docstatus": 1,
				},
				pluck="name",
			)
		)
	elif usage_type == "transfer":
		stock_entries = set(
			frappe.get_all(
				"Stock Entry",
				filters={
					"work_order": work_order,
					"purpose": "Material Transfer for Manufacture",
					"is_return": 0,
					"docstatus": 1,
				},
				pluck="name",
			)
		)
	else:
		raise ValueError(f"Unsupported serial/batch usage type: {usage_type}")

	if not stock_entries:
		return {}

	serial_batch_entries = frappe.get_all(
		"Serial and Batch Bundle",
		fields=[
			"`tabSerial and Batch Entry`.`serial_no`",
			"`tabSerial and Batch Entry`.`batch_no`",
			"`tabSerial and Batch Entry`.`qty`",
			"`tabSerial and Batch Bundle`.`warehouse`",
			"`tabSerial and Batch Bundle`.`item_code`",
			"`tabSerial and Batch Bundle`.`voucher_detail_no`",
		],
		filters=[
			["Serial and Batch Bundle", "voucher_type", "=", "Stock Entry"],
			[
				"Serial and Batch Bundle",
				"voucher_no",
				"in",
				sorted(stock_entries),
			],
			["Serial and Batch Bundle", "voucher_detail_no", "is", "set"],
			["Serial and Batch Bundle", "docstatus", "<", 2],
			["Serial and Batch Bundle", "is_cancelled", "=", 0],
			["Serial and Batch Entry", "qty", "<", 0],
		],
		limit=0,
	)
	stock_entry_details = frappe.get_all(
		"Stock Entry Detail",
		filters={"parent": ["in", sorted(stock_entries)]},
		fields=[
			"name",
			"item_code",
			"original_item",
			"s_warehouse",
			"transfer_qty",
		],
		limit=0,
	)
	if stock_entry_details:
		original_item_by_detail = {
			row.get("name"): row.get("original_item")
			for row in stock_entry_details
		}
		for entry in serial_batch_entries:
			entry["original_item"] = original_item_by_detail.get(
				entry.get("voucher_detail_no")
			)
	return _merge_alternative_fulfillment_usage(
		_build_row_wise_serial_batch_usage(serial_batch_entries),
		stock_entry_details,
	)


def _new_serial_batch_usage_row():
	return frappe._dict(
		serial_nos=[],
		batch_nos=defaultdict(float),
		alternative_fulfillment_qty=0,
	)


def _build_row_wise_serial_batch_usage(serial_batch_entries):
	"""Aggregate exact outward identities by required item and warehouse."""
	row_wise_usage = {}
	for source in serial_batch_entries or []:
		entry = frappe._dict(source)
		actual_item = entry.get("item_code")
		required_item = entry.get("original_item") or actual_item
		key = (
			required_item,
			entry.get("warehouse"),
		)
		if key not in row_wise_usage:
			row_wise_usage[key] = _new_serial_batch_usage_row()

		details = row_wise_usage[key]
		if required_item != actual_item:
			# Alternative material fulfills the original Work Order requirement, but
			# its serial/batch identities never belong to the original item's SRE.
			# Stock Entry Detail is the authoritative quantity source because ERPNext
			# permits an alternative without serial/batch tracking. Exact identity
			# matching here would leave the original stock falsely reserved.
			continue
		if entry.get("serial_no"):
			details.serial_nos.append(entry.get("serial_no"))
		if entry.get("batch_no"):
			details.batch_nos[entry.get("batch_no")] += abs(flt(entry.get("qty")))
	return row_wise_usage


def _merge_alternative_fulfillment_usage(row_wise_usage, stock_entry_details):
	"""Attach alternative fulfillment from submitted outward Stock Entry rows.

	Stock Entry Detail.transfer_qty is in stock UOM and exists even when an
	alternative item has no Serial and Batch Bundle. The caller already limits the
	parents to the exact submitted transfer or consumption stage.
	"""
	row_wise_usage = row_wise_usage or {}
	alternative_qty_by_group = defaultdict(float)
	for source in stock_entry_details or []:
		entry = frappe._dict(source)
		required_item = entry.get("original_item")
		actual_item = entry.get("item_code")
		source_warehouse = entry.get("s_warehouse")
		if (
			not required_item
			or required_item == actual_item
			or not source_warehouse
		):
			continue
		alternative_qty_by_group[(required_item, source_warehouse)] += abs(
			flt(entry.get("transfer_qty"))
		)

	for key, alternative_qty in alternative_qty_by_group.items():
		if key not in row_wise_usage:
			row_wise_usage[key] = _new_serial_batch_usage_row()
		# Replace, rather than add to, any bundle-derived placeholder. One Stock
		# Entry Detail can own multiple serial rows but represents the quantity once.
		row_wise_usage[key].alternative_fulfillment_qty = alternative_qty
	return row_wise_usage


def _rebuild_serial_batch_child_usage(entry_documents, row_wise_usage):
	"""Allocate submitted serial/batch usage once across a group of SRE children."""
	serial_remaining = {}
	batch_remaining = {}
	alternative_remaining = {}
	for key, source in (row_wise_usage or {}).items():
		data = frappe._dict(source or {})
		serial_remaining[key] = Counter(data.get("serial_nos") or [])
		batch_remaining[key] = defaultdict(
			float,
			{
				batch_no: max(flt(qty), 0)
				for batch_no, qty in (data.get("batch_nos") or {}).items()
			},
		)
		alternative_remaining[key] = max(
			flt(data.get("alternative_fulfillment_qty")),
			0,
		)

	used_qty_by_sre = defaultdict(float)
	for entry, doc in sorted(
		entry_documents or [],
		key=lambda pair: (
			str(pair[0].get("creation") or ""),
			str(pair[0].get("name") or ""),
		),
	):
		key = (entry.get("item_code"), entry.get("warehouse"))
		serials = serial_remaining.get(key, Counter())
		batches = batch_remaining.get(key, {})
		for row in sorted(
			doc.get("sb_entries") or [],
			key=lambda child: (int(child.get("idx") or 0), str(child.get("name") or "")),
		):
			used_qty = 0
			if row.get("serial_no") and serials.get(row.get("serial_no"), 0) > 0:
				used_qty = 1
				serials[row.get("serial_no")] -= 1
			elif row.get("batch_no") and flt(batches.get(row.get("batch_no"))) > 0:
				used_qty = min(
					abs(flt(row.get("qty"))),
					flt(batches.get(row.get("batch_no"))),
				)
				batches[row.get("batch_no")] = max(
					flt(batches.get(row.get("batch_no"))) - used_qty,
					0,
				)

			# An issued alternative item satisfies this original requirement without
			# sharing its serial/batch identity. Consume the remaining requirement
			# against original SRE children in stable SRE/child order. In ERPNext,
			# child delivered_qty is the amount no longer hard-reserved; rebuilding it
			# from submitted entries also restores the original reservation on cancel.
			child_capacity = 1 if row.get("serial_no") else abs(flt(row.get("qty")))
			alternative_used_qty = min(
				max(child_capacity - used_qty, 0),
				flt(alternative_remaining.get(key)),
			)
			used_qty += alternative_used_qty
			alternative_remaining[key] = max(
				flt(alternative_remaining.get(key)) - alternative_used_qty,
				0,
			)
			row.delivered_qty = used_qty
			row.db_update()
			used_qty_by_sre[entry.get("name")] += used_qty
	return used_qty_by_sre


def _reconcile_work_order_reservations(stock_entry):
	"""Persist group-correct SRE quantities after the native Work Order refresh."""
	if (
		not stock_entry.get("work_order")
		or stock_entry.get("purpose") not in MANUFACTURING_STOCK_ENTRY_PURPOSES
	):
		return []

	work_order = frappe.db.get_value(
		"Work Order",
		stock_entry.work_order,
		["name", "reserve_stock", "skip_transfer", "from_wip_warehouse", "wip_warehouse"],
		as_dict=True,
		for_update=True,
	)
	if not work_order or not work_order.get("reserve_stock"):
		return []

	required_items = frappe.get_all(
		"Work Order Item",
		filters={"parent": stock_entry.work_order},
		fields=[
			"name",
			"item_code",
			"source_warehouse",
			"transferred_qty",
			"consumed_qty",
			"idx",
		],
		order_by="idx, name",
		limit=0,
	)
	work_order_item_names = [item.get("name") for item in required_items if item.get("name")]
	if not work_order_item_names:
		return []

	stock_reservation_entries = frappe.get_all(
		"Stock Reservation Entry",
		filters={
			"docstatus": 1,
			"voucher_type": "Work Order",
			"voucher_no": stock_entry.work_order,
			"voucher_detail_no": ["in", work_order_item_names],
		},
		fields=[
			"name",
			"voucher_detail_no",
			"item_code",
			"warehouse",
			"reserved_qty",
			"voucher_qty",
			"delivered_qty",
			"transferred_qty",
			"consumed_qty",
			"status",
			"from_voucher_type",
			"from_voucher_no",
			"from_voucher_detail_no",
			"reservation_based_on",
			"creation",
		],
		order_by="creation, name",
		limit=0,
	)
	documents = _refresh_serial_batch_reservation_usage(
		work_order,
		required_items,
		stock_reservation_entries,
	)
	stock_facts = load_work_order_stock_facts([stock_entry.work_order])
	updates = build_work_order_sre_reconciliation(
		work_order,
		required_items,
		stock_reservation_entries,
		stock_facts,
	)
	if not updates:
		return []

	for update in updates:
		frappe.db.set_value(
			"Stock Reservation Entry",
			update.name,
			{
				"transferred_qty": update.transferred_qty,
				"consumed_qty": update.consumed_qty,
				"status": update.status,
			},
			update_modified=False,
		)

	for update in updates:
		if update.name not in documents:
			documents[update.name] = frappe.get_doc("Stock Reservation Entry", update.name)
	for detail_name in {update.voucher_detail_no for update in updates}:
		doc = next(
			doc
			for doc in documents.values()
			if doc.voucher_detail_no == detail_name
		)
		doc.update_reserved_qty_in_voucher(update_modified=False)
	for item_code, warehouse in {
		(update.item_code, update.warehouse) for update in updates
	}:
		doc = next(
			doc
			for doc in documents.values()
			if doc.item_code == item_code and doc.warehouse == warehouse
		)
		doc.update_reserved_stock_in_bin()
	return updates


class SubassemblyReservationStockEntryMixin:
	"""Refresh subassembly reservations after ERPNext updates the Work Order.

	A ``doc_events`` on-submit handler runs before the native Stock Entry
	controller's ``on_submit`` method.  At that point the Production Plan child
	row still contains the previous ``wo_produced_qty``.  Wrapping the controller
	method guarantees that the derived Bin is recalculated only after the native
	Work Order and Production Plan updates have finished.
	"""

	def validate(self):
		from process_simplification.production_workflow.service import (
			validate_guided_stock_entry_identity,
		)

		validate_guided_stock_entry_identity(self)
		return super().validate()

	def validate_component_and_quantities(self):
		# ERPNext calculates a Work Order's pending component quantity from gross
		# transferred_qty and therefore cannot represent a net gap reopened by a
		# submitted material return. The guided additional-transfer draft rebuilds
		# those rows from net coverage and validates the exact rows plus hard SRE in
		# before_submit, so skip only the incompatible gross-BOM comparison here.
		if (
			self.get("custom_process_workflow_action") == "Material Issue Request"
			and self.get("is_additional_transfer_entry")
		):
			return
		return super().validate_component_and_quantities()

	def make_stock_reserve_for_wip_and_fg(self):
		"""Leave guided finished goods for the sales handoff to reserve.

		ERPNext uses the Work Order ``reserve_stock`` flag for both raw-material
		reservation and automatic finished-goods reservation. The simplified flow
		needs the first behavior, but its receipt request deliberately hands the
		completed stock back to sales for an explicit reservation before delivery.
		Limit the exception to guided receipt requests so native Work Orders and
		material-transfer reservations keep their standard behavior.
		"""
		if (
			self.get("purpose") == "Manufacture"
			and self.get("work_order")
		):
			if self.get("custom_process_workflow_action") == "Receipt Request":
				return None
			# Internal output is reserved once, by its exact BOM edge after stock
			# posting. Native item-level matching can select another branch.
			order = frappe.db.get_value("Work Order", self.get("work_order"),
				["production_plan_sub_assembly_item", "custom_replenishes_work_order"], as_dict=True)
			if order and (order.production_plan_sub_assembly_item or order.custom_replenishes_work_order):
				return None
		return super().make_stock_reserve_for_wip_and_fg()

	def on_submit(self):
		result = super().on_submit()
		_reconcile_work_order_reservations(self)
		_refresh_subassembly_bin(self)
		from process_simplification.production_workflow.service import (
			reserve_replenishment_output,
		)

		replenishment_result = reserve_replenishment_output(self)
		from process_simplification.production_exceptions.service import complete_linked_stock_entry

		complete_linked_stock_entry(self)
		from process_simplification.notifications import notify_production_stock_completed

		notify_production_stock_completed(self, replenishment_result=replenishment_result)
		return result

	def on_cancel(self):
		from process_simplification.production_workflow.service import (
			cancel_replenishment_output_reservations,
		)

		cancel_replenishment_output_reservations(self)
		result = super().on_cancel()
		_reconcile_work_order_reservations(self)
		_refresh_subassembly_bin(self)
		from process_simplification.production_exceptions.service import reopen_cancelled_stock_entry

		reopen_cancelled_stock_entry(self)
		from process_simplification.production_workflow.service import (
			release_issue_request_reservations,
		)

		release_issue_request_reservations(self)
		from process_simplification.notifications import notify_production_stock_cancelled

		notify_production_stock_cancelled(self)
		return result


def _refresh_subassembly_bin(doc):
	"""Refresh v16's derived Production Plan reservation after manufacture.

	ERPNext updates the Work Order and Production Plan subassembly row when a
	Manufacture Stock Entry is submitted or cancelled, but the corresponding
	Bin can retain the earlier ``reserved_qty_for_production_plan`` value.  This
	helper must therefore run after the native Stock Entry controller method.
	The stale value otherwise makes the next parent Work Order look short even
	though the completed subassembly is physically in its source warehouse.
	"""
	if doc.get("purpose") != "Manufacture" or not doc.get("work_order"):
		return

	work_order = frappe.db.get_value(
		"Work Order",
		doc.work_order,
		[
			"production_plan",
			"production_plan_sub_assembly_item",
			"production_item",
			"fg_warehouse",
		],
		as_dict=True,
	)
	if not work_order or not work_order.get("production_plan_sub_assembly_item"):
		return
	if not work_order.get("production_item") or not work_order.get("fg_warehouse"):
		return

	bin_name = frappe.db.get_value(
		"Bin",
		{
			"item_code": work_order.production_item,
			"warehouse": work_order.fg_warehouse,
		},
		"name",
	)
	if not bin_name:
		return

	frappe.get_doc("Bin", bin_name).update_reserved_qty_for_for_sub_assembly()
