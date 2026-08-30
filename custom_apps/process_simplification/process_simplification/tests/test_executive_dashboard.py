from datetime import date
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from process_simplification.api.executive_dashboard import (
	_companies,
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
