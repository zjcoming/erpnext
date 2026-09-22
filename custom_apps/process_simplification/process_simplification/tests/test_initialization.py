from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from process_simplification import initialization


class TestInitialization(UnitTestCase):
	def setUp(self):
		super().setUp()
		self.stock = frappe._dict(enable_stock_reservation=1, auto_reserve_stock=0, allow_negative_stock=0)
		self.company = frappe._dict(default_wip_warehouse="WIP", default_fg_warehouse="FG")
		self.settings = {("Stock Settings", "default_warehouse"): "RM"}
		self.warehouses = {name: frappe._dict(company="Factory", is_group=0, disabled=0) for name in ("RM", "WIP", "FG")}
		def warehouse_value(doctype, name, fieldname=None, **kwargs):
			row = self.warehouses.get(name)
			return row.get(fieldname) if row and isinstance(fieldname, str) else row
		self.patches = [
			patch.object(initialization.frappe, "has_permission", return_value=True),
			patch.object(initialization.frappe, "get_roles", return_value=["System Manager"]),
			patch.object(initialization, "get_default_company", return_value="Factory"),
			patch.object(initialization, "validate_setup", return_value={"ok": True}),
			patch.object(initialization.frappe, "get_doc", return_value=self.company),
			patch.object(initialization.frappe, "get_single", return_value=self.stock),
			patch.object(initialization.frappe.db, "get_single_value", side_effect=lambda dt, field: self.settings.get((dt, field), 0)),
			patch.object(initialization.frappe.db, "get_value", side_effect=warehouse_value),
		]
		for item in self.patches:
			item.start()
			self.addCleanup(item.stop)

	def check(self, key):
		return next(row for row in initialization.get_status()["checks"] if row["key"] == key)

	def test_name_fallback_does_not_hide_missing_explicit_defaults(self):
		self.settings[("Stock Settings", "default_warehouse")] = None
		self.assertEqual(self.check("source_warehouse")["status"], "error")
		self.assertFalse(initialization.get_status()["configuration_ready"])

	def test_group_disabled_and_cross_company_warehouses_are_not_ready(self):
		for field, value in (("is_group", 1), ("disabled", 1), ("company", "Other Factory")):
			with self.subTest(field=field):
				self.warehouses["RM"] = frappe._dict(company="Factory", is_group=0, disabled=0)
				self.warehouses["RM"][field] = value
				self.assertEqual(self.check("source_warehouse")["status"], "error")

	def test_other_company_global_default_does_not_require_switching_it(self):
		self.warehouses["OTHER"] = frappe._dict(company="Other Factory", is_group=0, disabled=0)
		self.settings[("Stock Settings", "default_warehouse")] = "OTHER"
		initialization.validate_setup.return_value = {"ok": True, "defaults": {"source_warehouse": "RM"}}
		row = self.check("source_warehouse")
		self.assertEqual(row["status"], "warning")
		self.assertEqual(row["value"], "RM")
		self.assertIn("不要为不同公司反复切换", row["detail"])

	def test_read_only_configuration_is_not_presented_as_editable(self):
		def permission(dt, ptype, **kwargs):
			return ptype == "read" and dt != "Manufacturing Settings"
		with patch.object(initialization.frappe, "has_permission", side_effect=permission):
			self.assertFalse(self.check("enforce_time_logs")["can_open"])
			self.assertFalse(self.check("enable_stock_reservation")["can_configure"])

	def test_manual_reservation_mode_is_valid_and_check_does_not_mutate_settings(self):
		with patch.object(initialization.frappe.db, "set_single_value") as write:
			result = initialization.get_status()
		self.assertTrue(result["configuration_ready"])
		self.assertEqual(self.check("auto_reserve_stock")["status"], "info")
		write.assert_not_called()
		self.assertGreater(len(result["next_steps"]), 0)

	def test_optional_semi_finished_warehouse_shows_shared_mode_without_blocking(self):
		row = self.check("semi_finished_warehouse")
		self.assertEqual(row["status"], "info")
		self.assertIn("共用", row["value"])
		self.assertTrue(initialization.get_status()["configuration_ready"])

	def test_explicit_semi_finished_warehouse_is_checked_and_does_not_hide_invalid_setting(self):
		self.company.custom_default_semi_finished_warehouse = "SEMI"
		self.warehouses["SEMI"] = frappe._dict(company="Factory", is_group=0, disabled=0)
		self.assertEqual(self.check("semi_finished_warehouse")["status"], "ok")
		self.warehouses["SEMI"].disabled = 1
		row = self.check("semi_finished_warehouse")
		self.assertEqual(row["status"], "error")
		self.assertEqual(row["value"], "SEMI")
		self.assertIn("已禁用", row["detail"])
		self.assertFalse(initialization.get_status()["configuration_ready"])

	def test_work_in_progress_warehouse_cannot_double_as_independent_semi_finished_stock(self):
		self.company.custom_default_semi_finished_warehouse = "WIP"
		row = self.check("semi_finished_warehouse")
		self.assertEqual(row["status"], "error")
		self.assertIn("在制品仓", row["detail"])

	def test_missing_reservation_and_incompatible_reporting_are_visible(self):
		self.stock.enable_stock_reservation = 0
		self.stock.allow_negative_stock = 1
		self.settings[("Manufacturing Settings", "enforce_time_logs")] = 1
		for key in ("enable_stock_reservation", "allow_negative_stock", "enforce_time_logs"):
			self.assertEqual(self.check(key)["status"], "error")

	def test_company_scope_is_checked_before_reading_configuration(self):
		def permission(dt, *args, **kwargs):
			if dt == "Company":
				raise frappe.PermissionError
			return True
		with patch.object(initialization.frappe, "has_permission", side_effect=permission):
			with self.assertRaises(frappe.PermissionError):
				initialization.get_status(company="Other Factory")
		initialization.frappe.get_doc.assert_not_called()

	def test_empty_site_is_not_ready_and_does_not_create_company(self):
		initialization.get_default_company.return_value = None
		initialization.validate_setup.return_value = {"ok": False}
		result = initialization.get_status()
		self.assertFalse(result["configuration_ready"])
		self.assertIsNone(result["company"])
		initialization.frappe.get_doc.assert_not_called()

	def test_unconfigured_plan_is_stopped_before_demand_or_stock_changes(self):
		from process_simplification.api import actions
		from process_simplification.api.utils import SimplifiedFlowError

		self.stock.enable_stock_reservation = 0
		with patch.object(actions, "_row_from_workbench") as demand:
			with self.assertRaises(SimplifiedFlowError):
				actions._create_work_order("NO-ORDER", "NO-ROW")
		demand.assert_not_called()
