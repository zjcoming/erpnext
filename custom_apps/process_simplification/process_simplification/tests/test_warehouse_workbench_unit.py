from unittest import TestCase
from unittest.mock import patch

import frappe

from process_simplification import page_refresh
from process_simplification.api import warehouse
from process_simplification.purchasing import allocation, rejections


class TestWarehouseWorkbench(TestCase):
	def test_worker_cannot_query_warehouse_tasks(self):
		with patch.object(warehouse, "user_has_capability", return_value=False), patch.object(frappe, "get_list") as query:
			with self.assertRaises(frappe.PermissionError):
				warehouse._companies()
			query.assert_not_called()

	def test_missing_company_scope_does_not_become_unrestricted(self):
		with patch.object(warehouse, "user_has_capability", return_value=True), patch.object(warehouse, "user_company_scope", return_value=set()), patch.object(frappe, "get_list") as query:
			with self.assertRaises(frappe.PermissionError):
				warehouse._companies()
			query.assert_not_called()

	def test_foreign_company_is_rejected_before_document_lookup(self):
		with patch.object(warehouse, "user_has_capability", return_value=True), patch.object(warehouse, "user_company_scope", return_value={"Company A"}), patch.object(frappe, "get_all", return_value=["Company A"]):
			with self.assertRaises(frappe.PermissionError):
				warehouse._companies("Company B")

	def test_missing_document_read_permission_does_not_query_rows(self):
		with patch.object(frappe, "has_permission", return_value=False), patch.object(frappe, "get_list") as query:
			self.assertEqual(warehouse._read_queue("issue", ["Company A"]), ([], None))
			query.assert_not_called()

	def test_granted_company_name_is_available_when_warehouse_scope_hides_company_form(self):
		with patch.object(warehouse, "user_has_capability", return_value=True), patch.object(warehouse, "user_company_scope", return_value={"Company A"}), patch.object(frappe, "get_all", return_value=["Company A"]) as query, patch.object(frappe, "get_list") as native_list:
			self.assertEqual(warehouse._companies(), ["Company A"])
			self.assertEqual(query.call_args.kwargs["filters"], {"name": ["in", ["Company A"]]})
			native_list.assert_not_called()

	def test_pagination_reaches_old_accessible_tasks_after_more_than_100_hidden_documents(self):
		candidates = [frappe._dict(name=f"STE-{i:03}") for i in range(109)]
		def get_list(_doctype, **kwargs):
			self.assertEqual(kwargs["filters"]["company"], ["in", ["Company A"]])
			return candidates[kwargs["limit_start"]:kwargs["limit_start"] + kwargs["limit_page_length"]]
		def permission(_doctype, _ptype, doc=None):
			return doc is None or int(doc.name.split("-")[1]) >= 105
		with patch.object(frappe, "get_list", side_effect=get_list), patch.object(frappe, "get_doc", side_effect=lambda dt, name: frappe._dict(doctype=dt, name=name, company="Company A")), patch.object(frappe, "has_permission", side_effect=permission):
			first, cursor = warehouse._read_queue("issue", ["Company A"], page_length=2)
			second, next_cursor = warehouse._read_queue("issue", ["Company A"], start=cursor, page_length=2)
		self.assertEqual([row.name for row in first], ["STE-105", "STE-106"])
		self.assertEqual([row.name for row in second], ["STE-107", "STE-108"])
		self.assertIsNone(next_cursor)

	def test_complete_document_permission_filters_mixed_warehouse_documents(self):
		candidates = [frappe._dict(name="MIXED"), frappe._dict(name="ALLOWED")]
		with patch.object(frappe, "get_list", return_value=candidates), patch.object(frappe, "get_doc", side_effect=lambda dt, name: frappe._dict(doctype=dt, name=name, company="Company A")), patch.object(frappe, "has_permission", side_effect=lambda dt, p, doc=None: doc is None or doc.name == "ALLOWED"):
			rows, _cursor = warehouse._read_queue("receipt", ["Company A"])
		self.assertEqual([row.name for row in rows], ["ALLOWED"])

	def test_receiving_quantity_keeps_order_uom_and_each_item_remainder(self):
		doc = frappe._dict(
			name="PO-1", doctype="Purchase Order", company="Company A", supplier="Supplier A",
			items=[
				frappe._dict(item_code="RM-1", item_name="Material", qty=20, received_qty=5, uom="Box", stock_uom="Nos", stock_qty=200, schedule_date="2026-09-07"),
				frappe._dict(item_code="RM-2", qty=10, received_qty=10, uom="Nos"),
			],
		)
		with patch.object(frappe, "has_permission", return_value=True), patch.object(warehouse, "nowdate", return_value="2026-09-08"):
			row = warehouse._document_row(doc, "purchase")
		self.assertEqual([(item["qty"], item["uom"]) for item in row["items"]], [(15, "Box")])
		self.assertTrue(row["overdue"])

	def test_return_direction_is_not_converted_to_positive_receipt(self):
		doc = frappe._dict(name="PR-RETURN", doctype="Purchase Receipt", company="Company A", is_return=1, items=[frappe._dict(item_code="RM-1", qty=-3, uom="Nos")])
		with patch.object(frappe, "has_permission", return_value=True):
			row = warehouse._document_row(doc, "receipt")
		self.assertTrue(row["is_return"])
		self.assertEqual(row["items"][0]["qty"], -3)

	def test_zero_shortage_does_not_hide_unchecked_demands(self):
		from process_simplification.api.production import production_overview_summary
		base = dict(unplanned_production_qty=1, delivery_timing="later", status_code="planning_required")
		rows = [
			{**base, "material_summary": {"status_code": "not_checked", "shortage_item_count": 0}},
			{**base, "material_summary": {"status_code": "ready", "shortage_item_count": 0}},
		]
		result = production_overview_summary(rows)
		self.assertEqual(result["material_shortage_demands"], 0)
		self.assertEqual(result["unchecked_material_demands"], 1)


class TestWarehouseActionSummary(TestCase):
	def setUp(self):
		self.enterContext(patch.object(warehouse, "_companies", return_value=["Company A", "Company B"]))
		self.enterContext(patch.object(warehouse, "user_has_capability", return_value=True))
		self.enterContext(patch.object(frappe, "has_permission", return_value=True))
		self.rejection_summary = self.enterContext(patch.object(rejections, "get_summary", return_value={
			"status": "ready", "has_pending": False, "receipt_count": 0,
		}))

	def test_no_purchasing_capability_hides_purchasing_but_keeps_permitted_returns(self):
		with patch.object(warehouse, "user_has_capability", return_value=False), patch.object(page_refresh, "company_shortages") as shortages, patch.object(allocation, "get_request_page") as requests:
			result = warehouse.get_action_summary("Company A")
		self.assertEqual(result["actions"], [
			{"key": "shortage", "status": "unavailable"},
			{"key": "purchase_followup", "status": "unavailable"},
			{"key": "rejection_followup", "status": "ready", "has_pending": False, "receipt_count": 0},
		])
		shortages.assert_not_called()
		requests.assert_not_called()
		self.rejection_summary.assert_called_once_with("Company A")

	def test_missing_receipt_permission_never_queries_rejection_counts(self):
		with patch.object(frappe, "has_permission", side_effect=lambda doctype, ptype: doctype != "Purchase Receipt"), patch.object(page_refresh, "company_shortages", return_value={"shortages": []}), patch.object(allocation, "get_request_page", return_value={"rows": [], "next_start": None}):
			result = warehouse.get_action_summary("Company A")
		self.assertEqual(result["actions"][2], {"key": "rejection_followup", "status": "unavailable"})
		self.rejection_summary.assert_not_called()
		self.assertEqual(result["actions"][1]["request_count"], 0)

	def test_rejection_failures_never_report_zero_or_hide_other_actions(self):
		for error, status in ((frappe.PermissionError(), "unavailable"), (RuntimeError("failed rejection query"), "error")):
			with self.subTest(status=status), patch.object(page_refresh, "company_shortages", return_value={"shortages": []}), patch.object(allocation, "get_request_page", return_value={"rows": [{"name": "MR-1", "company": "Company A"}], "next_start": None}), patch.object(frappe, "log_error") as log:
				self.rejection_summary.side_effect = error
				result = warehouse.get_action_summary("Company A")
				self.assertEqual(result["actions"][2], {"key": "rejection_followup", "status": status})
				self.assertEqual(result["actions"][1]["request_count"], 1)
				self.assertEqual(bool(result.get("_refresh_stale")), status == "error")
				self.assertEqual(log.call_count, int(status == "error"))

	def test_missing_document_permission_hides_only_inaccessible_action(self):
		with patch.object(frappe, "has_permission", side_effect=lambda doctype, ptype: doctype != "Sales Order"), patch.object(page_refresh, "company_shortages") as shortages, patch.object(allocation, "get_request_page", return_value={"rows": [], "next_start": None}):
			result = warehouse.get_action_summary("Company A")
		self.assertEqual(result["actions"][0], {"key": "shortage", "status": "unavailable"})
		self.assertEqual(result["actions"][1]["request_count"], 0)
		shortages.assert_not_called()

	def test_material_rows_match_item_warehouse_and_sales_orders_are_deduplicated(self):
		rows = [
			{"company": "Company A", "item_code": "RM-1", "warehouse": "W1", "shortage_qty": 10, "sources": [{"sales_order": "SO-1"}, {"sales_order": "SO-2"}]},
			{"company": "Company A", "item_code": "RM-1", "warehouse": "W1", "shortage_qty": 2, "sources": [{"sales_order": "SO-1"}]},
			{"company": "Company A", "item_code": "RM-1", "warehouse": "W2", "shortage_qty": 1, "sources": [{"sales_order": "SO-1"}]},
			{"company": "Company A", "item_code": "RM-2", "warehouse": "W1", "shortage_qty": 0, "sources": [{"sales_order": "SO-COVERED"}]},
			{"company": "Company B", "item_code": "PRIVATE", "warehouse": "B1", "shortage_qty": 5, "sources": [{"sales_order": "SO-PRIVATE"}]},
		]
		with patch.object(page_refresh, "company_shortages", return_value={"shortages": rows}) as query:
			result = warehouse._shortage_action(["Company A"])
		self.assertEqual(result, {"status": "ready", "has_pending": True, "material_count": 2, "sales_order_count": 2})
		query.assert_called_once_with(company="Company A")

	def test_same_material_in_two_companies_remains_two_rows(self):
		def shortages(company):
			return {"shortages": [{"company": company, "item_code": "RM-1", "warehouse": "W1", "shortage_qty": 1}]}
		with patch.object(page_refresh, "company_shortages", side_effect=shortages) as query:
			result = warehouse._shortage_action(["Company A", "Company B"])
		self.assertEqual(result["material_count"], 2)
		self.assertEqual(query.call_count, 2)

	def test_followup_counts_all_visible_pages_for_selected_company(self):
		pages = [
			{"rows": [{"name": "MR-1", "company": "Company A"}], "next_start": 50},
			{"rows": [{"name": "MR-2", "company": "Company A"}, {"name": "PRIVATE", "company": "Company B"}], "next_start": None},
		]
		with patch.object(allocation, "get_request_page", side_effect=pages) as query:
			result = warehouse._purchase_followup_action(["Company A"])
		self.assertEqual(result, {"status": "ready", "has_pending": True, "request_count": 2})
		self.assertEqual([call.kwargs for call in query.call_args_list], [
			{"company": "Company A", "view": "to_order", "start": 0, "page_length": 50},
			{"company": "Company A", "view": "to_order", "start": 50, "page_length": 50},
		])

	def test_pending_shortages_do_not_report_zero_or_suppress_document_queues(self):
		with patch.object(page_refresh, "company_shortages", return_value={"_refresh_pending": True}), patch.object(allocation, "get_request_page", return_value={"rows": [{"name": "MR-1", "company": "Company A"}], "next_start": None}):
			result = warehouse.get_action_summary("Company A")
		self.assertEqual(result["actions"][0], {"key": "shortage", "status": "pending"})
		self.assertEqual(result["actions"][1]["request_count"], 1)
		self.assertTrue(result["_refresh_stale"])
		self.assertNotIn("_refresh_pending", result)

	def test_error_preserves_other_section_and_permission_error_exposes_no_counts(self):
		with patch.object(page_refresh, "company_shortages", side_effect=RuntimeError("unavailable")), patch.object(allocation, "get_request_page", side_effect=frappe.PermissionError), patch.object(frappe, "log_error") as log:
			result = warehouse.get_action_summary("Company A")
		self.assertEqual(result["actions"], [
			{"key": "shortage", "status": "error"},
			{"key": "purchase_followup", "status": "unavailable"},
			{"key": "rejection_followup", "status": "ready", "has_pending": False, "receipt_count": 0},
		])
		self.assertTrue(result["_refresh_stale"])
		log.assert_called_once()

	def test_stale_shortage_snapshot_is_visible_but_keeps_retry_marker(self):
		with patch.object(page_refresh, "company_shortages", return_value={"shortages": [], "_refresh_stale": True}), patch.object(allocation, "get_request_page", return_value={"rows": [], "next_start": None}):
			result = warehouse.get_action_summary("Company A")
		self.assertEqual(result["actions"][0]["status"], "ready")
		self.assertFalse(result["actions"][0]["has_pending"])
		self.assertTrue(result["_refresh_stale"])

	def test_all_companies_keep_actions_grouped_and_failures_isolated(self):
		def shortages(company):
			if company == "Company A":
				raise RuntimeError("Company A unavailable")
			return {"shortages": [{"company": company, "item_code": "RM-1", "warehouse": "W1", "shortage_qty": 1, "sources": [{"sales_order": "SO-B"}]}]}
		with patch.object(page_refresh, "company_shortages", side_effect=shortages) as shortage_query, patch.object(allocation, "get_request_page", side_effect=lambda **kwargs: {"rows": [{"name": "MR-" + kwargs["company"], "company": kwargs["company"]}], "next_start": None}) as request_query, patch.object(frappe, "log_error"):
			result = warehouse.get_action_summary()
		self.assertIsNone(result["company"])
		self.assertNotIn("actions", result)
		self.assertEqual([group["company"] for group in result["groups"]], ["Company A", "Company B"])
		a, b = result["groups"]
		self.assertEqual(a["actions"][0], {"key": "shortage", "status": "error"})
		self.assertEqual(a["actions"][1]["request_count"], 1)
		self.assertTrue(a["_refresh_stale"])
		self.assertEqual(b["actions"][0]["material_count"], 1)
		self.assertEqual(b["actions"][0]["sales_order_count"], 1)
		self.assertEqual(b["actions"][1]["request_count"], 1)
		self.assertNotIn("_refresh_stale", b)
		self.assertTrue(result["_refresh_stale"])
		self.assertEqual([call.kwargs["company"] for call in shortage_query.call_args_list], ["Company A", "Company B"])
		self.assertEqual([call.kwargs["company"] for call in request_query.call_args_list], ["Company A", "Company B"])

	def test_single_accessible_company_returns_resolved_company_for_target_links(self):
		with patch.object(warehouse, "_companies", return_value=["Company A"]), patch.object(page_refresh, "company_shortages", return_value={"shortages": []}), patch.object(allocation, "get_request_page", return_value={"rows": [], "next_start": None}):
			result = warehouse.get_action_summary()
		self.assertEqual(result["company"], "Company A")
		self.assertIn("actions", result)
		self.assertNotIn("groups", result)
