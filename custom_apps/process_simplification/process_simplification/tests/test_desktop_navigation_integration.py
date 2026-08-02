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

	def test_production_workbench_patch_updates_existing_navigation(self):
		from process_simplification.patches.v0_0.add_production_workbench_navigation import execute

		sidebar = frappe.get_doc("Workspace Sidebar", "process-simplification")
		for item in sidebar.items:
			if item.link_to == "order-workbench":
				item.label = "订单履约总览"
		sidebar.items = [item for item in sidebar.items if item.link_to != "production-workbench"]
		sidebar.save(ignore_permissions=True)

		workspace = frappe.get_doc("Workspace", "process-simplification")
		for item in workspace.links:
			if item.link_to == "order-workbench":
				item.label = "订单履约总览"
		workspace.links = [item for item in workspace.links if item.link_to != "production-workbench"]
		workspace.save(ignore_permissions=True)

		execute()

		sidebar.reload()
		workspace.reload()
		self.assertEqual(
			frappe.db.get_value("Page", "order-workbench", "title"),
			"订单工作台",
		)
		self.assertEqual(
			next(item.label for item in sidebar.items if item.link_to == "order-workbench"),
			"订单工作台",
		)
		self.assertEqual(
			next(item.label for item in workspace.links if item.link_to == "order-workbench"),
			"订单工作台",
		)
		self.assertLess(
			next(index for index, item in enumerate(sidebar.items) if item.link_to == "production-workbench"),
			next(index for index, item in enumerate(sidebar.items) if item.link_to == "shortage-purchase-planning"),
		)
		self.assertLess(
			next(index for index, item in enumerate(workspace.links) if item.link_to == "production-workbench"),
			next(index for index, item in enumerate(workspace.links) if item.link_to == "shortage-purchase-planning"),
		)
