import frappe
from frappe.tests import IntegrationTestCase


class TestDesktopNavigationIntegration(IntegrationTestCase):
	def test_apps_screen_route_targets_the_slugged_workspace(self):
		from process_simplification import hooks

		self.assertEqual(
			hooks.add_to_apps_screen[0]["route"],
			"/desk/process-simplification",
		)

	def test_workspace_patch_repairs_existing_desktop_navigation(self):
		from process_simplification.patches.v0_0.fix_desktop_navigation import execute

		frappe.db.set_value(
			"Workspace",
			"process-simplification",
			{"label": "流程简化", "title": "流程简化"},
			update_modified=False,
		)
		frappe.db.set_value(
			"Desktop Icon",
			"流程简化",
			{"link": "/app/process-simplification", "hidden": 0},
			update_modified=False,
		)
		frappe.db.set_value(
			"Desktop Icon",
			"process-simplification",
			{"parent_icon": "流程简化", "hidden": 0},
			update_modified=False,
		)

		execute()

		self.assertEqual(
			frappe.db.get_value(
				"Workspace",
				"process-simplification",
				["label", "title"],
				as_dict=True,
			),
			{"label": "process-simplification", "title": "process-simplification"},
		)
		self.assertEqual(
			frappe.db.get_value("Desktop Icon", "流程简化", "link"),
			"/desk/process-simplification",
		)
		self.assertEqual(
			frappe.db.get_value(
				"Desktop Icon",
				"process-simplification",
				["parent_icon", "hidden"],
				as_dict=True,
			),
			{"parent_icon": None, "hidden": 1},
		)
