from datetime import date
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from process_simplification.api.executive_dashboard import (
	_companies,
	_delivery_orders,
	_order_health,
	_resolve_company,
	classify_stock,
	owner_company_scope,
	percentage_change,
	previous_period,
)


class TestExecutiveDashboardHelpers(IntegrationTestCase):
	def test_previous_period_has_the_same_inclusive_length(self):
		self.assertEqual(
			previous_period(date(2026, 8, 1), date(2026, 8, 31)),
			(date(2026, 7, 1), date(2026, 7, 31)),
		)

	def test_percentage_change_handles_zero_comparison(self):
		self.assertIsNone(percentage_change(100, 0))
		self.assertEqual(percentage_change(120, 100), 20.0)

	def test_inventory_classification_prioritizes_wip_warehouse(self):
		self.assertEqual(classify_stock("Products", "在制品仓 - C"), "work_in_progress")
		self.assertEqual(classify_stock("Products", "Finished Goods - C"), "finished_goods")
		self.assertEqual(classify_stock("Sub Assemblies", "Stores - C"), "semi_finished")
		self.assertEqual(classify_stock("Raw Material", "Stores - C"), "raw_material")
		self.assertEqual(classify_stock("Consumable", "Stores - C"), "other")

	@patch("process_simplification.api.executive_dashboard.frappe.get_all")
	def test_owner_company_scope_uses_top_level_company_user_permissions(self, get_all):
		get_all.return_value = [
			frappe._dict(for_value="Company A", applicable_for=None),
			frappe._dict(for_value="Company B", applicable_for="Sales Order"),
		]

		self.assertEqual(owner_company_scope("owner@example.com"), {"Company A"})

	@patch("process_simplification.api.executive_dashboard.owner_company_scope", return_value={"Company A"})
	@patch("process_simplification.api.executive_dashboard.frappe.get_all")
	def test_company_list_is_filtered_to_the_owner_scope(self, get_all, owner_scope):
		get_all.return_value = [frappe._dict(name="Company A", default_currency="CNY")]

		self.assertEqual([row.name for row in _companies()], ["Company A"])
		self.assertEqual(get_all.call_args.kwargs["filters"], {"name": ["in", ["Company A"]]})

	def test_requested_company_outside_owner_scope_is_rejected(self):
		with self.assertRaises(frappe.PermissionError):
			_resolve_company(
				"Company B",
				[frappe._dict(name="Company A", default_currency="CNY")],
			)


class TestExecutiveDashboardDeliveries(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.company = f"_Test Dashboard {frappe.generate_hash(length=10)}"
		self.reference_date = date(2026, 9, 27)

	def add_order(self, key, delivery_date, *, company=None, docstatus=1, status="To Deliver", progress=0):
		# Minimal transaction-scoped rows exercise the actual SQL, including order
		# eligibility, without invoking unrelated sales/manufacturing workflows.
		name = f"{self.company}-{key}"
		frappe.db.sql(
			"""insert into `tabSales Order`
			(name, company, docstatus, status, delivery_date, per_delivered, creation,
			 base_net_total, base_grand_total, disable_rounded_total)
			values (%s, %s, %s, %s, %s, %s, %s, 100, 100, 1)""",
			(name, company or self.company, docstatus, status, delivery_date, progress, "2026-09-01 12:00:00"),
		)
		return name

	def test_overdue_today_and_seventh_day_share_the_summary_scope(self):
		seventh = self.add_order("seventh", "2026-10-04")
		self.add_order("eighth", "2026-10-05")
		today_order = self.add_order("today", "2026-09-27")
		overdue = self.add_order("overdue", "2026-09-25", progress=40)
		self.add_order("draft", "2026-09-20", docstatus=0)
		self.add_order("cancelled", "2026-09-20", docstatus=2)
		self.add_order("closed", "2026-09-20", status="Closed")
		self.add_order("completed", "2026-09-20", status="Completed")
		self.add_order("delivered", "2026-09-20", progress=100)
		self.add_order("foreign", "2026-09-20", company="Another Company")
		self.add_order("undated", None)
		rows = _delivery_orders(self.company, self.reference_date)
		self.assertEqual([row.name for row in rows], [overdue, today_order, seventh])
		health = _order_health(self.company, self.reference_date)
		self.assertEqual(health["overdue_orders"], 1)
		self.assertEqual(health["due_within_7_days"], 2)
		self.assertEqual(health["other_open_orders"], 2)

	def test_five_row_limit_prioritizes_overdue_and_breaks_date_ties_stably(self):
		self.add_order("upcoming", "2026-09-28")
		expected = sorted(self.add_order(f"overdue-{key}", "2026-09-25") for key in range(6))
		self.assertEqual(
			[row.name for row in _delivery_orders(self.company, self.reference_date)], expected[:5]
		)

	def test_upcoming_orders_are_returned_without_any_overdue_orders(self):
		expected = self.add_order("upcoming", "2026-09-30")
		self.assertEqual([row.name for row in _delivery_orders(self.company, self.reference_date)], [expected])
		self.assertEqual(_delivery_orders("No Such Company", self.reference_date), [])
