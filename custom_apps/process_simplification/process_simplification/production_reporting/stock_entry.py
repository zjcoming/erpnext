from __future__ import annotations

import frappe


def before_submit(doc, method=None):
	# Finished-goods posting is the only native path that can make a Work Order
	# Completed. Do not let it strand a Draft Job Card with immutable wage history.
	if doc.get("purpose") != "Manufacture" or not doc.get("work_order"):
		return
	from process_simplification.production_reporting.work_order import (
		assert_no_managed_draft_job_cards,
	)

	assert_no_managed_draft_job_cards(doc.work_order)


class SubassemblyReservationStockEntryMixin:
	"""Refresh subassembly reservations after ERPNext updates the Work Order.

	A ``doc_events`` on-submit handler runs before the native Stock Entry
	controller's ``on_submit`` method.  At that point the Production Plan child
	row still contains the previous ``wo_produced_qty``.  Wrapping the controller
	method guarantees that the derived Bin is recalculated only after the native
	Work Order and Production Plan updates have finished.
	"""

	def on_submit(self):
		result = super().on_submit()
		_refresh_subassembly_bin(self)
		return result

	def on_cancel(self):
		result = super().on_cancel()
		_refresh_subassembly_bin(self)
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
