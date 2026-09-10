from __future__ import annotations

from contextlib import contextmanager

import frappe
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
	return max(flt(actual) - reserved, 0)


_GUIDED_SRE_VOUCHER_FLAG = "process_simplification_guided_sre_voucher"


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
