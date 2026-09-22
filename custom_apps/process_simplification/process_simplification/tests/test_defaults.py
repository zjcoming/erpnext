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
			[
				"default_wip_warehouse", "default_fg_warehouse", "custom_default_semi_finished_warehouse",
				"default_scrap_warehouse", "custom_material_quarantine_warehouse", "custom_material_rework_warehouse",
			],
			as_dict=True,
		)
		get_single_value.assert_called_once_with("Stock Settings", "default_warehouse")
		self.assertEqual(result.source_warehouse, "Raw Material - TC")
		self.assertEqual(result.wip_warehouse, "WIP - TC")
		self.assertEqual(result.fg_warehouse, "Finished Goods - TC")
		self.assertIsNone(result.configured_semi_finished_warehouse)
		self.assertEqual(result.semi_finished_warehouse, "Raw Material - TC")
		self.assertIsNone(result.semi_finished_warehouse_error)

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


class TestSemiFinishedWarehouseConfiguration(UnitTestCase):
	def setUp(self):
		super().setUp()
		from process_simplification import defaults
		self.module = defaults
		self.company = frappe._dict(name="Factory", default_wip_warehouse="WIP")
		self.warehouses = {
			name: frappe._dict(company="Factory", is_group=0, disabled=0)
			for name in ("RM", "ORDER-RM", "SEMI", "WIP", "FG")
		}
		for mock in (
			patch.object(defaults.frappe.db, "get_value", side_effect=lambda dt, name, *a, **kw: self.warehouses.get(name)),
			patch.object(defaults.frappe, "get_cached_value", return_value=self.company),
			patch.object(defaults.frappe.db, "get_single_value", return_value="RM"),
			patch.object(defaults, "warehouse_belongs_to_company", return_value=True),
			patch.object(defaults, "find_company_warehouse", return_value=None),
		):
			mock.start()
			self.addCleanup(mock.stop)

	def test_blank_setting_uses_the_orders_source_even_when_global_default_differs(self):
		result = self.module.resolve_semi_finished_warehouse(
			"Factory", "ORDER-RM", defaults=frappe._dict(source_warehouse="RM")
		)
		self.assertEqual(result.warehouse, "ORDER-RM")
		self.assertIsNone(result.configured_warehouse)
		self.assertTrue(result.can_use)

	def test_explicit_setting_replaces_only_semi_finished_route(self):
		self.company.custom_default_semi_finished_warehouse = "SEMI"
		resolved = self.module.get_company_manufacturing_defaults("Factory")
		self.assertEqual(resolved.source_warehouse, "RM")
		self.assertEqual(resolved.semi_finished_warehouse, "SEMI")
		self.assertEqual(resolved.configured_semi_finished_warehouse, "SEMI")
		self.assertIsNone(resolved.semi_finished_warehouse_error)
		result = self.module.resolve_semi_finished_warehouse("Factory", "ORDER-RM", defaults=resolved)
		self.assertEqual(result.warehouse, "SEMI")
		self.assertTrue(result.can_use)

	def test_invalid_explicit_setting_never_falls_back_to_raw_material_warehouse(self):
		self.company.custom_default_semi_finished_warehouse = "SEMI"
		cases = (
			(None, "warehouse_missing"),
			(frappe._dict(company="Other", is_group=0, disabled=0), "warehouse_company_mismatch"),
			(frappe._dict(company="Factory", is_group=1, disabled=0), "warehouse_is_group"),
			(frappe._dict(company="Factory", is_group=0, disabled=1), "warehouse_disabled"),
		)
		for warehouse, reason in cases:
			with self.subTest(reason=reason):
				self.warehouses["SEMI"] = warehouse
				defaults = self.module.get_company_manufacturing_defaults("Factory")
				self.assertEqual(defaults.semi_finished_warehouse, "SEMI")
				self.assertEqual(defaults.semi_finished_warehouse_error, reason)
				resolved = self.module.resolve_semi_finished_warehouse("Factory", "RM", defaults=defaults)
				self.assertFalse(resolved.can_use)
				self.assertEqual(resolved.warehouse, "SEMI")
				self.assertEqual(resolved.reason, reason)
				with self.assertRaises(frappe.ValidationError):
					self.module.validate_company_manufacturing_warehouses(self.company)

	def test_stock_in_progress_or_quarantine_is_not_available_semi_finished_stock(self):
		self.company.custom_default_semi_finished_warehouse = "SEMI"
		for field in self.module.SEMI_FINISHED_CONFLICT_FIELDS:
			with self.subTest(field=field):
				self.company[field] = "SEMI"
				self.assertEqual(
					self.module.get_company_manufacturing_defaults("Factory").semi_finished_warehouse_error,
					"warehouse_role_conflict",
				)
				with self.assertRaises(frappe.ValidationError):
					self.module.validate_company_manufacturing_warehouses(self.company)
				self.company[field] = None

	def test_raw_material_and_finished_goods_warehouse_sharing_is_allowed(self):
		self.company.default_fg_warehouse = "FG"
		for warehouse in ("RM", "FG"):
			with self.subTest(warehouse=warehouse):
				self.company.custom_default_semi_finished_warehouse = warehouse
				self.module.validate_company_manufacturing_warehouses(self.company)
				self.assertTrue(self.module.resolve_semi_finished_warehouse("Factory").can_use)

	def test_company_document_validation_does_not_require_mapping_iteration(self):
		class CompanyDocument:
			name = "Factory"

			def get(self, field):
				return {"custom_default_semi_finished_warehouse": "SEMI", "default_wip_warehouse": "WIP"}.get(field)

		self.module.validate_company_manufacturing_warehouses(CompanyDocument())

	def test_name_resolved_wip_cannot_be_used_as_available_semi_finished_stock(self):
		self.company.default_wip_warehouse = None
		self.company.default_fg_warehouse = "FG"
		self.company.custom_default_semi_finished_warehouse = "SEMI"
		with (
			patch.object(self.module, "warehouse_belongs_to_company", side_effect=lambda warehouse, company: bool(warehouse)),
			patch.object(self.module, "find_company_warehouse", return_value="SEMI"),
		):
			result = self.module.get_company_manufacturing_defaults("Factory")
			with self.assertRaises(frappe.ValidationError):
				self.module.validate_company_manufacturing_warehouses(self.company)
		self.assertEqual(result.wip_warehouse, "SEMI")
		self.assertEqual(result.semi_finished_warehouse_error, "warehouse_role_conflict")

	def test_clearing_setting_restores_legacy_mode_and_does_not_modify_work_orders(self):
		self.company.custom_default_semi_finished_warehouse = None
		with patch.object(self.module.frappe.db, "set_value") as write:
			self.module.validate_company_manufacturing_warehouses(self.company)
			result = self.module.get_company_manufacturing_defaults("Factory")
		write.assert_not_called()
		self.assertEqual(result.semi_finished_warehouse, "RM")

	def test_missing_company_is_safe_during_first_install(self):
		with patch.object(self.module, "get_default_company", return_value=None):
			result = self.module.get_company_manufacturing_defaults()
		self.assertIsNone(result.company)
		self.assertIsNone(result.semi_finished_warehouse)
		self.assertIsNone(result.semi_finished_warehouse_error)

	def test_invalid_semi_finished_configuration_blocks_setup_gate(self):
		from process_simplification.api import setup
		self.company.custom_default_semi_finished_warehouse = "SEMI"
		self.warehouses["SEMI"].disabled = 1
		with patch.object(setup.frappe, "has_permission", return_value=True):
			result = setup.validate_setup(company="Factory")
		self.assertFalse(result["ok"])
		message = next(row for row in result["messages"] if row["fieldname"] == "custom_default_semi_finished_warehouse")
		self.assertEqual(message["status"], "error")
		self.assertIn("已禁用", message["detail"])
