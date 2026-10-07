from datetime import date
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import frappe
from jinja2 import FileSystemLoader, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from process_simplification.api import wage_slips


class TestWageSlips(TestCase):
	def setUp(self):
		self.manager = self.enterContext(
			patch.object(wage_slips, "require_wage_manager", return_value={"Factory"})
		)
		self.get_list = self.enterContext(patch.object(frappe, "get_list"))
		self.company = self.enterContext(
			patch.object(
				frappe,
				"get_cached_value",
				return_value=frappe._dict(company_name="测试工厂", default_currency="CNY"),
			)
		)
		self.enterContext(patch.object(wage_slips, "_", side_effect=lambda text: text))
		self.enterContext(patch.object(frappe, "throw", side_effect=self.throw))
		self.render = self.enterContext(
			patch.object(frappe, "render_template", side_effect=self.render_template)
		)

	@staticmethod
	def throw(message, exc=frappe.ValidationError):
		raise exc(message)

	@staticmethod
	def render_template(template, context):
		root = Path(wage_slips.__file__).parents[1]
		env = SandboxedEnvironment(loader=FileSystemLoader(root), undefined=StrictUndefined)
		return env.get_template(template).render(context)

	@staticmethod
	def row(index=1, **fields):
		return frappe._dict(
			{
				"name": f"WAGE-{index:03d}",
				"employee": f"EMP-{index:03d}",
				"employee_name": f"员工{index}",
				"docstatus": 1,
				"piecework_amount": 1234.56,
				"time_amount": 240,
				"total_amount": 1474.56,
				**fields,
			}
		)

	def test_all_employees_is_not_limited_to_the_list_page_and_only_saved_totals_are_printed(self):
		self.get_list.return_value = [self.row(index) for index in range(1, 24)]
		result = wage_slips.get_print_html("Factory", "2026-08-01")
		self.assertEqual(result["count"], 23)
		self.assertEqual(result["html"].count('<article class="wage-slip">'), 23)
		self.assertEqual(result["html"].count('<section class="wage-sheet">'), 5)
		self.assertIn("WAGE-023", result["html"])
		self.assertIn("1,474.56", result["html"])
		query = self.get_list.call_args.kwargs
		self.assertEqual(query["limit_page_length"], 0)
		self.assertEqual(
			query["filters"], {"company": "Factory", "month_start": date(2026, 8, 1), "docstatus": ("<", 2)}
		)
		self.assertNotIn("details", query["fields"])
		self.manager.assert_called_once_with("Factory")

	def test_selected_names_are_deduplicated_and_constrained_by_company_and_month(self):
		self.get_list.return_value = [self.row()]
		result = wage_slips.get_print_html("Factory", "2026-08-01", '["WAGE-001", "WAGE-001"]')
		self.assertEqual(result["count"], 1)
		self.assertEqual(self.get_list.call_args.kwargs["filters"]["name"], ("in", ["WAGE-001"]))

	def test_unavailable_selected_row_fails_instead_of_silently_printing_a_subset(self):
		self.get_list.return_value = [self.row()]
		with self.assertRaises(frappe.PermissionError):
			wage_slips.get_print_html("Factory", "2026-08-01", ["WAGE-001", "OTHER-COMPANY"])
		self.render.assert_not_called()

	def test_empty_or_malformed_selections_never_expand_to_everyone(self):
		for names in ([], "[]", "null", {}, "[1]", "bad-json", [""], [None]):
			with self.subTest(names=names), self.assertRaises(frappe.ValidationError):
				wage_slips.get_print_html("Factory", "2026-08-01", names)
		self.get_list.assert_not_called()

	def test_invalid_months_do_not_default_to_today(self):
		for month in (None, "", "2026-08", "2026-08-12", "2026-13-01", "0000-01-01"):
			with self.subTest(month=month), self.assertRaises(frappe.ValidationError):
				wage_slips.get_print_html("Factory", month)
		self.get_list.assert_not_called()

	def test_malformed_company_is_rejected_before_scope_lookup(self):
		for company in (None, "", "  ", [], {}):
			with self.subTest(company=company), self.assertRaises(frappe.ValidationError):
				wage_slips.get_print_html(company, "2026-08-01")
		self.manager.assert_not_called()
		self.get_list.assert_not_called()

	def test_wage_scope_is_required_for_every_entry_point_before_reading_data(self):
		self.manager.side_effect = frappe.PermissionError
		for call in (
			lambda: wage_slips.get_print_periods(),
			lambda: wage_slips.get_print_employees("Other", "2026-08-01"),
			lambda: wage_slips.get_print_html("Other", "2026-08-01"),
		):
			with self.assertRaises(frappe.PermissionError):
				call()
		self.get_list.assert_not_called()

	def test_periods_keep_company_scope_and_unlimited_history(self):
		self.get_list.return_value = []
		wage_slips.get_print_periods()
		query = self.get_list.call_args.kwargs
		self.assertEqual(query["filters"]["company"], ("in", ["Factory"]))
		self.assertEqual(query["limit_page_length"], 0)
		self.assertNotIn("month_start", query["filters"])

	def test_employee_picker_excludes_cancelled_rows_and_does_not_fetch_wage_amounts(self):
		self.get_list.return_value = []
		wage_slips.get_print_employees("Factory", "2026-08-01")
		query = self.get_list.call_args.kwargs
		self.assertEqual(query["filters"]["docstatus"], ("<", 2))
		self.assertNotIn("total_amount", query["fields"])

	def test_no_summaries_gives_a_useful_error(self):
		self.get_list.return_value = []
		with self.assertRaisesRegex(frappe.ValidationError, "请先生成月度汇总"):
			wage_slips.get_print_html("Factory", "2026-08-01")

	def test_names_are_escaped_drafts_are_explicit_and_details_are_absent(self):
		self.company.return_value.company_name = '<img src=x onerror="alert(1)">'
		self.company.return_value.default_currency = "USD"
		self.get_list.return_value = [
			self.row(
				employee_name="<script>bad()</script>",
				docstatus=0,
				details=[{"source_report": "PRIVATE-DETAIL"}],
			)
		]
		html = wage_slips.get_print_html("Factory", "2026-08-01")["html"]
		self.assertIn("草稿 · 待确认", html)
		self.assertIn("&lt;script&gt;", html)
		self.assertIn("&lt;img", html)
		self.assertNotIn("<script>bad", html)
		self.assertNotIn("<img src=x", html)
		self.assertNotIn("PRIVATE-DETAIL", html)
		self.assertIn("工资合计（USD）", html)
		self.assertNotIn("实发", html)
