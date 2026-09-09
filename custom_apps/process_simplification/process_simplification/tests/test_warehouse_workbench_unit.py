from unittest import TestCase
from unittest.mock import patch

import frappe

from process_simplification.api import warehouse


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
