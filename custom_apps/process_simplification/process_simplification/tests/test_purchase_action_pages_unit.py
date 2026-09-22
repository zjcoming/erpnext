from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from process_simplification.purchasing import allocation


def request_doc(name="MR-1", *, ordered_qty=0, received_qty=0, stock_qty=10, company="Company A"):
	doc = SimpleNamespace(
		name=name, company=company, status="Pending", transaction_date="2026-09-22",
		items=[frappe._dict(item_code="RM-1", stock_qty=stock_qty, ordered_qty=ordered_qty, received_qty=received_qty)],
	)
	doc.get = lambda key: getattr(doc, key, None)
	return doc


class TestPurchaseActionPages(TestCase):
	def setUp(self):
		self.enterContext(patch.object(allocation, "_user_matches_company", side_effect=lambda user, company: company == "Company A"))

	def test_company_filter_is_applied_before_pagination_for_both_lists(self):
		with patch.object(allocation, "document_page", return_value={"rows": [], "next_start": None}) as query:
			allocation.get_request_page(company="Company A", view="to_order")
			self.assertEqual(query.call_args.args[1]["company"], "Company A")
			self.assertEqual(query.call_args.args[1]["status"], ["not in", ["Stopped", "Cancelled", "Received", "Transferred", "Issued"]])
			allocation.get_supplier_followup(company="Company A")
			self.assertEqual(query.call_args.args[1]["company"], "Company A")

	def test_foreign_company_is_rejected_without_scanning_documents(self):
		with patch.object(allocation, "document_page") as query:
			for callback in (allocation.get_request_page, allocation.get_supplier_followup):
				with self.assertRaises(frappe.PermissionError):
					callback(company="Company B")
		query.assert_not_called()

	def test_to_order_follows_native_submitted_quantities_not_draft_commitments(self):
		with patch.object(allocation, "document_page") as query:
			allocation.get_request_page(company="Company A", view="to_order")
			project = query.call_args.args[2]
			# A draft order has no native ordered_qty yet and still needs confirmation.
			self.assertIsNotNone(project(request_doc(ordered_qty=0)))
			self.assertIsNotNone(project(request_doc(ordered_qty=4)))
			self.assertIsNone(project(request_doc(ordered_qty=10)))
			# Cancelling the submitted order restores the native remaining quantity.
			self.assertIsNotNone(project(request_doc(ordered_qty=0)))
			self.assertIsNone(project(request_doc(ordered_qty=0, received_qty=10)))
			self.assertIsNotNone(project(request_doc(ordered_qty=0, received_qty=4)))

	def test_to_order_keeps_small_remaining_qty_and_rejects_nonpositive_requests(self):
		with patch.object(allocation, "document_page") as query:
			allocation.get_request_page(view="to_order")
			project = query.call_args.args[2]
			self.assertIsNotNone(project(request_doc(ordered_qty=9.999999, stock_qty=10)))
			self.assertIsNone(project(request_doc(stock_qty=0)))
			self.assertIsNone(project(request_doc(stock_qty=-1)))

	def test_existing_pending_view_still_contains_fully_ordered_requests(self):
		with patch.object(allocation, "document_page") as query:
			allocation.get_request_page()
			self.assertNotIn("company", query.call_args.args[1])
			self.assertIsNotNone(query.call_args.args[2](request_doc(ordered_qty=10)))

	def test_to_order_uses_the_existing_permission_checked_document_page(self):
		# The extension only changes filters/projection, leaving native list and
		# complete-document permission checks to the existing shared query layer.
		from process_simplification.purchasing import query
		with patch.object(frappe, "has_permission", side_effect=lambda dt, ptype, doc=None, **kw: doc is None or doc.name == "READABLE"), patch.object(frappe, "get_list", return_value=["HIDDEN", "READABLE"]), patch.object(frappe, "get_doc", side_effect=lambda dt, name: request_doc(name)), patch.object(query, "_user_matches_company", return_value=True):
			result = allocation.get_request_page(company="Company A", view="to_order")
		self.assertEqual([row["name"] for row in result["rows"]], ["READABLE"])
