from __future__ import annotations

from contextlib import contextmanager

import frappe


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

	def submit(self):
		if _is_guided_stock_reservation_side_effect(self):
			self.flags.ignore_permissions = True
		return super().submit()
