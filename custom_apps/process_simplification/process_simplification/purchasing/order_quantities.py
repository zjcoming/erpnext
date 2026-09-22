"""Release cancelled supplier commitments while keeping accepted receipt coverage."""

from __future__ import annotations

import frappe

from process_simplification.purchasing.receipt_quantities import accepted_stock_qty_expression


def request_commitment_expression():
	"""Closed ordinary POs cover only their accepted stock, not rejected goods.

	Stock-updating invoices are the native second receipt source. Non-stock
	invoices do not add supply, including invoices billed against a receipt.
	Subcontracting keeps its native finished-goods quantity semantics.
	"""
	accepted = accepted_stock_qty_expression("receipt_item")
	return f"""case when exists (
		select 1 from `tabPurchase Order` closed_order
		where closed_order.name=`tabPurchase Order Item`.parent
		and closed_order.status='Closed' and coalesce(closed_order.is_subcontracted, 0)=0
	) then least(stock_qty, greatest(0,
		coalesce((select sum({accepted})
			from `tabPurchase Receipt Item` receipt_item
			join `tabPurchase Receipt` receipt on receipt.name=receipt_item.parent
			where receipt_item.purchase_order_item=`tabPurchase Order Item`.name
			and receipt.docstatus=1), 0)
		+ coalesce((select sum(invoice_item.stock_qty)
			from `tabPurchase Invoice Item` invoice_item
			join `tabPurchase Invoice` invoice on invoice.name=invoice_item.parent
			where invoice_item.po_detail=`tabPurchase Order Item`.name
			and invoice.docstatus=1 and invoice.update_stock=1), 0)
	)) else stock_qty end"""


class PurchaseOrderRequestCommitmentMixin:
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		for update in self.status_updater:
			if (
				update.get("source_dt") == "Purchase Order Item"
				and update.get("target_dt") == "Material Request Item"
				and update.get("target_field") == "ordered_qty"
				and update.get("source_field") == "stock_qty"
			):
				update["source_field"] = request_commitment_expression()

	def update_status(self, status):
		if not self.is_subcontracted:
			from process_simplification.purchasing.allocation import validate_order_allocation
			from process_simplification.purchasing.receipts import lock_material_requests

			# Native status changes do not run before_validate. Serialize with
			# allocation and receipt submission before changing the supplier state.
			lock_material_requests(row.material_request for row in self.items)
			previous_status = self.status
			try:
				self.status = status or self.status
				validate_order_allocation(self)
			finally:
				self.status = previous_status
		result = super().update_status(status)
		if not self.is_subcontracted:
			# The native close/reopen method updates Bin requested stock but does
			# not recompute the Material Request's ordered quantity first.
			self.update_prevdoc_status()
			self.update_requested_qty()
		return result


def refresh_closed_order_commitments(doc, method=None):
	"""Refresh retained accepted coverage after a closed PO's receipt correction."""
	if doc.doctype == "Purchase Invoice" and not doc.get("update_stock"):
		return
	from process_simplification.purchasing.receipts import lock_material_requests

	for name in sorted({row.purchase_order for row in doc.items if row.purchase_order}):
		order = frappe.get_doc("Purchase Order", name)
		if order.docstatus != 1 or order.status != "Closed" or order.is_subcontracted:
			continue
		lock_material_requests(row.material_request for row in order.items)
		order.update_prevdoc_status()
		order.update_requested_qty()
