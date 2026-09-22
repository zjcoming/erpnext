"""Signed accepted/rejected quantities for ERPNext purchase receipt rows.

The native rejected-warehouse return mapper puts the rejected return in ``qty``
and ``stock_qty`` instead of ``rejected_qty``. Its explicit row marker preserves
the distinction; warehouse names and the warehouse's current flags do not.
"""

from __future__ import annotations

from frappe.utils import cint, flt


def accepted_stock_qty(row):
	"""Accepted receipts less accepted returns, expressed in the stock unit."""
	return 0.0 if cint(row.get("return_qty_from_rejected_warehouse")) else flt(row.get("stock_qty"))


def rejected_stock_qty(row):
	"""Rejected receipts less rejected returns, expressed in the stock unit."""
	if cint(row.get("return_qty_from_rejected_warehouse")):
		return flt(row.get("stock_qty"))
	return flt(row.get("rejected_qty")) * (flt(row.get("conversion_factor")) or 1)


def accepted_stock_qty_expression(alias=None):
	"""SQL equivalent used by the native status updater and custom aggregations.

	``alias`` is an internal SQL table alias, never request input.
	"""
	prefix = f"{alias}." if alias else ""
	return (
		f"case when coalesce({prefix}return_qty_from_rejected_warehouse, 0)=1 "
		f"then 0 else {prefix}stock_qty end"
	)


class PurchaseReceiptAcceptedQuantityMixin:
	"""Keep native Material Request progress aligned with usable receipts.

	Only the MR accepted-quantity aggregate changes. PO total-delivery progress,
	return limits, stock ledgers and valuation retain their native behavior. The
	standard status updater recalculates the complete submitted history on both
	submit and cancel, including earlier rejected-warehouse returns.
	"""

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		for update in self.status_updater:
			if (
				update.get("source_dt") == "Purchase Receipt Item"
				and update.get("target_dt") == "Material Request Item"
				and update.get("target_field") == "received_qty"
				and update.get("source_field") == "stock_qty"
			):
				update["source_field"] = accepted_stock_qty_expression()
