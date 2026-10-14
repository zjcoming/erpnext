"""Quantity eligible for receipt from submitted native operation facts."""

from frappe.utils import flt


def operation_receipt_remaining_qty(work_order) -> float:
	"""Cap receipts by the least completed operation, never Draft Job Card totals."""
	operations = work_order.get("operations")
	if operations is None and (
		work_order.get("operation_count") or work_order.get("job_card_count")
	):
		# A list/count without the native quantities is not evidence of completion.
		return 0.0
	completed = min(
		[flt(work_order.get("qty"))]
		+ [
			max(flt(row.get("completed_qty")) + flt(row.get("process_loss_qty")), 0)
			for row in operations or []
		]
	)
	return max(
		completed - flt(work_order.get("produced_qty"))
		- flt(work_order.get("process_loss_qty")),
		0,
	)
