from __future__ import annotations

from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import flt


def locked_available_qty(item_code, warehouse, *, ignore_sre=None):
	"""Serialize physical stock promises, including the first SRE in an empty pool."""
	if not item_code or not warehouse:
		return 0.0
	from erpnext.stock.utils import get_bin

	bin_name = get_bin(item_code, warehouse).name
	actual = frappe.db.get_value("Bin", bin_name, "actual_qty", for_update=True)
	sre = frappe.qb.DocType("Stock Reservation Entry")
	query = frappe.qb.from_(sre).select(
		sre.name, sre.reserved_qty, sre.delivered_qty, sre.transferred_qty, sre.consumed_qty
	).where((sre.docstatus == 1) & (sre.item_code == item_code) & (sre.warehouse == warehouse))
	if ignore_sre:
		query = query.where(sre.name != ignore_sre)
	reserved = sum(max(flt(row.reserved_qty) - flt(row.delivered_qty)
		- flt(row.transferred_qty) - flt(row.consumed_qty), 0)
		for row in query.for_update().run(as_dict=True))
	available = max(flt(actual) - reserved, 0)
	# Keep the same physical pool and lock order. Batch eligibility only narrows
	# what this pool can promise; it never changes plan priority or ownership.
	from process_simplification.batch_compat import get_batch_stock_facts

	batch = get_batch_stock_facts(item_code, warehouse, ignore_sre=ignore_sre)
	if batch is not None:
		available = min(available, max(flt(batch.get("free_qty")), 0))
	return available


_GUIDED_SRE_VOUCHER_FLAG = "process_simplification_guided_sre_voucher"
_CLOSE_SRE_VOUCHER_FLAG = "process_simplification_closed_work_order_reservations"


def _is_guided_stock_reservation_side_effect(doc) -> bool:
	allowed = getattr(frappe.flags, _GUIDED_SRE_VOUCHER_FLAG, None)
	if not allowed:
		return False
	return tuple(allowed) == (doc.get("voucher_type"), doc.get("voucher_no"))


@contextmanager
def allow_guided_stock_reservations_for(voucher_type: str, voucher_no: str):
	"""Authorize only native SRE side effects for one exact guided voucher."""
	previous = getattr(frappe.flags, _GUIDED_SRE_VOUCHER_FLAG, None)
	setattr(frappe.flags, _GUIDED_SRE_VOUCHER_FLAG, (voucher_type, voucher_no))
	try:
		yield
	finally:
		if previous is None:
			frappe.flags.pop(_GUIDED_SRE_VOUCHER_FLAG, None)
		else:
			setattr(frappe.flags, _GUIDED_SRE_VOUCHER_FLAG, previous)


@contextmanager
def _allow_work_order_close_reservations(work_order: str, company: str):
	"""Called only after the close RPC has authorized and locked its Work Order."""
	previous = getattr(frappe.flags, _CLOSE_SRE_VOUCHER_FLAG, None)
	setattr(frappe.flags, _CLOSE_SRE_VOUCHER_FLAG, (work_order, company, frappe.session.user))
	try:
		yield
	finally:
		if previous is None:
			frappe.flags.pop(_CLOSE_SRE_VOUCHER_FLAG, None)
		else:
			setattr(frappe.flags, _CLOSE_SRE_VOUCHER_FLAG, previous)


def _check_work_order_close_reservation(doc, scope):
	work_order, company, user = scope
	expected = ("Work Order", work_order, company, 1)
	fields = ("voucher_type", "voucher_no", "company", "docstatus")
	# Match both the loaded document and the stored row: a crafted in-memory
	# voucher/company must never turn another reservation into a derived cancel.
	stored = frappe.db.get_value("Stock Reservation Entry", doc.name, fields, as_dict=True)
	parent = frappe.db.get_value("Work Order", work_order, ["company", "docstatus", "status"], as_dict=True)
	if (
		frappe.session.user != user
		or tuple(doc.get(field) for field in fields) != expected
		or not stored
		or tuple(stored.get(field) for field in fields) != expected
		or not parent
		or (parent.company, parent.docstatus, parent.status) != (company, 1, "Closed")
	):
		frappe.throw(_("只能在关闭已授权工单时释放该工单的库存预留。"), frappe.PermissionError)


class GuidedStockReservationEntryMixin:
	"""Keep SRE permissions narrow while allowing ERPNext's derived records.

	Production managers submit the simplified Production Plan/Work Order action,
	but must not receive generic create or submit permission on Stock Reservation
	Entry. The adapter sets a request-local exact voucher marker while native
	ERPNext builds or transfers those derived reservations.
	"""

	def save(self, *args, **kwargs):
		if _is_guided_stock_reservation_side_effect(self):
			kwargs["ignore_permissions"] = True
		return super().save(*args, **kwargs)

	def insert(self, *args, **kwargs):
		if _is_guided_stock_reservation_side_effect(self):
			kwargs["ignore_permissions"] = True
		return super().insert(*args, **kwargs)

	def validate_with_allowed_qty(self, qty_to_be_reserved):
		available = locked_available_qty(self.item_code, self.warehouse, ignore_sre=self.name)
		if flt(qty_to_be_reserved, self.precision("reserved_qty")) > flt(available, self.precision("reserved_qty")):
			frappe.throw("库存发生变化，当前实际可预留数量不足，请刷新后重试。")
		return super().validate_with_allowed_qty(qty_to_be_reserved)

	def submit(self):
		if _is_guided_stock_reservation_side_effect(self):
			self.flags.ignore_permissions = True
		return super().submit()

	def cancel(self):
		scope = getattr(frappe.flags, _CLOSE_SRE_VOUCHER_FLAG, None)
		if not scope:
			return super().cancel()
		_check_work_order_close_reservation(self, scope)
		had_override = "ignore_permissions" in self.flags
		previous = self.flags.get("ignore_permissions")
		self.flags.ignore_permissions = True
		try:
			# Keep native before_cancel/on_cancel, Bin updates, Version and user audit.
			return super().cancel()
		finally:
			if had_override:
				self.flags.ignore_permissions = previous
			else:
				self.flags.pop("ignore_permissions", None)
