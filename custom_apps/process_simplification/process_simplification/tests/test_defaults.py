from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase


class TestManufacturingDefaults(UnitTestCase):
	def test_v16_source_warehouse_comes_from_stock_settings(self):
		from process_simplification.defaults import get_company_manufacturing_defaults

		company_defaults = frappe._dict(
			default_wip_warehouse="WIP - TC",
			default_fg_warehouse="Finished Goods - TC",
		)
		with (
			patch("process_simplification.defaults.get_default_company", return_value="Test Company"),
			patch(
				"process_simplification.defaults.frappe.get_cached_value",
				return_value=company_defaults,
			) as get_cached_value,
			patch(
				"process_simplification.defaults.frappe.db.get_single_value",
				return_value="Raw Material - TC",
			) as get_single_value,
			patch(
				"process_simplification.defaults.warehouse_belongs_to_company",
				return_value=True,
			),
		):
			result = get_company_manufacturing_defaults()

		get_cached_value.assert_called_once_with(
			"Company",
			"Test Company",
			["default_wip_warehouse", "default_fg_warehouse"],
			as_dict=True,
		)
		get_single_value.assert_called_once_with("Stock Settings", "default_warehouse")
		self.assertEqual(result.source_warehouse, "Raw Material - TC")
		self.assertEqual(result.wip_warehouse, "WIP - TC")
		self.assertEqual(result.fg_warehouse, "Finished Goods - TC")

	def test_configure_writes_source_to_stock_settings_not_company(self):
		from process_simplification.defaults import configure_company_manufacturing_defaults

		resolved = frappe._dict(
			company="Test Company",
			source_warehouse="Raw Material - TC",
			wip_warehouse="WIP - TC",
			fg_warehouse="Finished Goods - TC",
		)
		current_company_defaults = frappe._dict(
			default_wip_warehouse=None,
			default_fg_warehouse=None,
		)
		with (
			patch(
				"process_simplification.defaults.get_company_manufacturing_defaults",
				side_effect=[resolved, resolved],
			),
			patch(
				"process_simplification.defaults.frappe.get_cached_value",
				return_value=current_company_defaults,
			),
			patch(
				"process_simplification.defaults.frappe.db.get_single_value",
				return_value=None,
			),
			patch(
				"process_simplification.defaults.warehouse_belongs_to_company",
				return_value=False,
			),
			patch("process_simplification.defaults.frappe.db.set_single_value") as set_single_value,
			patch("process_simplification.defaults.frappe.db.set_value") as set_value,
			patch("process_simplification.defaults.frappe.clear_cache"),
		):
			result = configure_company_manufacturing_defaults()

		set_single_value.assert_called_once_with(
			"Stock Settings",
			"default_warehouse",
			"Raw Material - TC",
			update_modified=False,
		)
		set_value.assert_called_once_with(
			"Company",
			"Test Company",
			{
				"default_wip_warehouse": "WIP - TC",
				"default_fg_warehouse": "Finished Goods - TC",
			},
			update_modified=False,
		)
		self.assertEqual(result.source_warehouse, "Raw Material - TC")
