from datetime import date
from unittest import TestCase
from unittest.mock import patch

import frappe

from process_simplification.production_reporting import summary
from process_simplification.purchasing import allocation, query, receipts
from process_simplification.api import executive_dashboard as dashboard


class TestReviewFollowups(TestCase):
	def test_month_close_rejects_running_paused_and_pending_reports(self):
		doc = frappe._dict(name="SUMMARY", company="A", employee="EMP", month_start="2026-08-01", details=[frappe._dict(source_report="APPROVED")])
		approved = frappe._dict(name="APPROVED", status="Approved")
		for status in ("In Progress", "Paused", "Pending Approval"):
			with self.subTest(status=status), patch.object(summary, "_month_report_rows", return_value=[approved, frappe._dict(name="UNFINISHED", status=status)]), patch.object(summary, "_eligible_reports", return_value=[approved]):
				with self.assertRaises(frappe.ValidationError):
					summary._assert_complete_source_set(doc)
		with patch.object(summary, "_month_report_rows", return_value=[approved, frappe._dict(name="REJECTED", status="Rejected")]), patch.object(summary, "_eligible_reports", return_value=[approved]):
			summary._assert_complete_source_set(doc)

	def test_document_pages_reach_old_matches_after_100_hidden_or_nonmatching_documents(self):
		names = [f"PO-{index:03}" for index in range(125)]
		def candidates(dt, **kwargs):
			return names[kwargs["limit_start"]:kwargs["limit_start"] + kwargs["limit_page_length"]]
		with patch.object(frappe, "get_list", side_effect=candidates), patch.object(frappe, "get_doc", side_effect=lambda dt, name: frappe._dict(name=name, company="A")), patch.object(query, "_user_matches_company", return_value=True), patch.object(frappe, "has_permission", side_effect=lambda dt, p, doc=None, **kw: doc is None or doc.name >= "PO-110"):
			project = lambda doc: dict(doc) if doc.name >= "PO-121" else None
			first = query.document_page("Purchase Order", {}, project, page_length=2)
			second = query.document_page("Purchase Order", {}, project, start=first["next_start"], page_length=2)
		self.assertEqual([row["name"] for row in first["rows"] + second["rows"]], ["PO-121", "PO-122", "PO-123", "PO-124"])
		self.assertIsNone(second["next_start"])

	def test_foreign_company_is_removed_before_projection(self):
		with patch.object(frappe, "has_permission", return_value=True), patch.object(frappe, "get_list", return_value=["FOREIGN"]), patch.object(frappe, "get_doc", return_value=frappe._dict(company="B")), patch.object(query, "_user_matches_company", return_value=False), patch("builtins.print") as project:
			self.assertEqual(query.document_page("Purchase Order", {}, project)["rows"], [])
			project.assert_not_called()

	def test_pending_requests_exclude_processed_history_and_search_item_names(self):
		seen = {}
		def page(dt, filters, project, **kwargs):
			seen.update(filters)
			doc = frappe.get_doc({"doctype": "Material Request", "name": "OLD", "company": "A", "status": "Pending", "items": [{"item_code": "PA6", "stock_qty": 10}]})
			return project(doc)
		with patch.object(allocation, "document_page", side_effect=page), patch.object(allocation, "item_search_codes", return_value={"PA6"}):
			row = allocation.get_request_page(search="德尔隆")
		self.assertEqual(row["name"], "OLD")
		self.assertIn("Received", seen["status"][1])

	def test_followup_keeps_order_units_and_does_not_reopen_fulfilled_lines(self):
		doc = frappe.get_doc({"doctype": "Purchase Order", "name": "PO", "supplier": "S", "company": "A", "docstatus": 1, "status": "To Receive and Bill", "items": [
			{"name": "LINE", "item_code": "RM", "qty": 10, "received_qty": 6, "uom": "Box", "conversion_factor": 20, "schedule_date": "2026-09-01"},
			{"name": "DONE", "item_code": "DONE", "qty": 10, "received_qty": 10, "uom": "Nos"},
		]})
		with patch.object(frappe.db, "sql", return_value=[frappe._dict(purchase_order_item="LINE", returned_stock_qty=40)]), patch.object(frappe, "has_permission", return_value=False), patch.object(allocation, "nowdate", return_value="2026-09-08"), patch.object(allocation, "normalize_purchase_qty", side_effect=lambda qty: round(qty, 6)), patch("process_simplification.api.utils.apply_current_item_names"):
			row = allocation._order_followup(doc)
		self.assertEqual(len(row["items"]), 1)
		self.assertEqual((row["items"][0]["pending_qty"], row["items"][0]["returned_qty"], row["items"][0]["uom"]), (4, 2, "Box"))
		self.assertEqual(row["items"][0]["overdue_days"], 7)
		self.assertFalse(row["can_receive"])
		self.assertFalse(row["can_submit"])

	def test_receipt_notice_company_filter_precedes_pagination(self):
		seen = {}
		def get_all(dt, **kwargs):
			if dt == "Company":
				return ["A", "B"]
			seen.update(kwargs)
			return [frappe._dict(name=f"NOTICE-{n}") for n in range(3)]
		with patch.object(frappe, "get_all", side_effect=get_all), patch.object(receipts, "_can_manage", side_effect=lambda company: company == "A"):
			result = receipts.get_notice_page(start=300, page_length=2, search="PA6")
		self.assertEqual(seen["filters"], [["company", "in", ["A"]]])
		self.assertEqual(seen["limit_start"], 300)
		self.assertEqual(result["next_start"], 302)
		self.assertEqual(len(result["rows"]), 2)

	def test_fully_received_order_cannot_offer_an_empty_receipt(self):
		doc = frappe.get_doc({"doctype": "Purchase Order", "name": "PO-DONE", "docstatus": 1, "status": "To Bill", "items": [{"item_code": "RM", "qty": 10, "received_qty": 10, "uom": "Nos", "conversion_factor": 1}]})
		with patch.object(frappe.db, "sql", return_value=[]), patch.object(frappe, "has_permission", return_value=True), patch.object(allocation, "normalize_purchase_qty", side_effect=lambda qty: round(qty, 6)), patch("process_simplification.api.utils.apply_current_item_names"):
			row = allocation._order_followup(doc, pending_only=False)
		self.assertFalse(row["can_receive"])
		self.assertFalse(row["can_submit"])


class TestDashboardAmountQueries(TestCase):
	def setUp(self):
		self.company = "Dashboard Query " + frappe.generate_hash(length=8)
		frappe.db.savepoint("review_dashboard_amounts")

	def tearDown(self):
		frappe.db.rollback(save_point="review_dashboard_amounts")

	def order(self, name, *, rounded=1100, grand=1100, disabled=0, due="2026-09-01", delivered=(0, 1)):
		name = self.company + name
		frappe.db.sql("""insert into `tabSales Order`
			(name, company, docstatus, status, transaction_date, delivery_date, creation,
			base_net_total, base_grand_total, base_rounded_total, disable_rounded_total, per_delivered)
			values (%s,%s,1,'To Deliver and Bill','2026-09-01',%s,now(),1100,%s,%s,%s,50)""",
			(name, self.company, due, grand, rounded, disabled))
		for index, amount in enumerate((100, 1000)):
			frappe.db.sql("""insert into `tabSales Order Item`
				(name,parent,parenttype,parentfield,qty,base_net_amount,delivered_qty)
				values (%s,%s,'Sales Order','items',1,%s,%s)""", (name + str(index), name, amount, delivered[index]))
		return name

	def test_high_price_line_delivered_first_leaves_low_price_value(self):
		self.order("ONE")
		health = dashboard._order_health(self.company, date(2026, 9, 8))
		self.assertEqual(health["pending_amount"], 100)
		self.assertEqual(health["overdue_amount"], 100)
		self.assertEqual(float(dashboard._overdue_orders(self.company, date(2026, 9, 8))[0].pending_amount), 100)

	def test_disabled_rounding_and_proportional_tax_are_consistent(self):
		self.order("TAX", rounded=0, grand=1243, disabled=1)
		totals = dashboard._order_totals(self.company, date(2026, 9, 1), date(2026, 9, 8))
		self.assertEqual(totals["order_amount"], 1243)
		self.assertEqual(dashboard._order_trend(self.company, date(2026, 9, 8))[-1]["order_amount"], 1243)
		self.assertEqual(dashboard._order_health(self.company, date(2026, 9, 8))["pending_amount"], 113)

	def test_overdue_amount_excludes_future_delivery_and_remaining_is_bounded(self):
		self.order("OVERDUE", delivered=(0, 1))
		self.order("FUTURE", due="2026-09-20", delivered=(0, 0))
		self.order("OVERDELIVERED", delivered=(2, 2))
		health = dashboard._order_health(self.company, date(2026, 9, 8))
		self.assertEqual(health["pending_amount"], 1200)
		self.assertEqual(health["overdue_amount"], 100)
