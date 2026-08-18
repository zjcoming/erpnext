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

	def test_production_plan_center_patch_repairs_existing_navigation_identity(self):
		from process_simplification.patches.v0_0.rename_production_workbench_to_plan_center import execute

		sidebar = frappe.get_doc("Workspace Sidebar", "process-simplification")
		for item in sidebar.items:
			if item.link_to == "order-workbench":
				item.label = "订单履约总览"
			if item.link_to == "production-workbench":
				item.label = "生产工作台"
				item.icon = "manufacturing"
		sidebar.save(ignore_permissions=True)

		workspace = frappe.get_doc("Workspace", "process-simplification")
		for item in workspace.links:
			if item.link_to == "order-workbench":
				item.label = "订单履约总览"
			if item.link_to == "production-workbench":
				item.label = "生产工作台"
		workspace.save(ignore_permissions=True)
		frappe.db.set_value(
			"Page",
			"production-workbench",
			"title",
			"生产工作台",
			update_modified=False,
		)

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
		production_sidebar = next(
			item for item in sidebar.items if item.link_to == "production-workbench"
		)
		production_workspace = next(
			item for item in workspace.links if item.link_to == "production-workbench"
		)
		self.assertEqual(production_sidebar.label, "生产计划中心")
		self.assertEqual(production_sidebar.icon, "factory")
		self.assertEqual(production_workspace.label, "生产计划中心")
		self.assertEqual(
			frappe.db.get_value("Page", "production-workbench", "title"),
			"生产计划中心",
		)
		self.assertLess(
			next(index for index, item in enumerate(sidebar.items) if item.link_to == "production-workbench"),
			next(index for index, item in enumerate(sidebar.items) if item.link_to == "shortage-purchase-planning"),
		)
		self.assertLess(
			next(index for index, item in enumerate(workspace.links) if item.link_to == "production-workbench"),
			next(index for index, item in enumerate(workspace.links) if item.link_to == "shortage-purchase-planning"),
		)

	def test_production_navigation_patch_restores_missing_items(self):
		from process_simplification.patches.v0_0.add_production_workbench_navigation import execute

		sidebar = frappe.get_doc("Workspace Sidebar", "process-simplification")
		sidebar.items = [item for item in sidebar.items if item.link_to != "production-workbench"]
		sidebar.save(ignore_permissions=True)

		workspace = frappe.get_doc("Workspace", "process-simplification")
		workspace.links = [item for item in workspace.links if item.link_to != "production-workbench"]
		workspace.save(ignore_permissions=True)

		execute()

		sidebar.reload()
		workspace.reload()
		production_sidebar = next(
			item for item in sidebar.items if item.link_to == "production-workbench"
		)
		production_workspace = next(
			item for item in workspace.links if item.link_to == "production-workbench"
		)
		self.assertEqual(production_sidebar.label, "生产计划中心")
		self.assertEqual(production_sidebar.icon, "factory")
		self.assertEqual(production_workspace.label, "生产计划中心")
		self.assertLess(
			next(index for index, item in enumerate(sidebar.items) if item.link_to == "production-workbench"),
			next(index for index, item in enumerate(sidebar.items) if item.link_to == "shortage-purchase-planning"),
		)
		self.assertLess(
			next(index for index, item in enumerate(workspace.links) if item.link_to == "production-workbench"),
			next(index for index, item in enumerate(workspace.links) if item.link_to == "shortage-purchase-planning"),
		)

	def test_worker_reporting_navigation_is_idempotent_and_role_scoped(self):
		from process_simplification.patches.v0_0.add_worker_reporting_navigation import execute

		execute()
		execute()

		sidebar = frappe.get_doc("Workspace Sidebar", "process-simplification")
		workspace = frappe.get_doc("Workspace", "process-simplification")
		for route, label in {
			"my-production-reporting": "我的报工",
			"production-report-review": "报工审核",
		}.items():
			sidebar_rows = [row for row in sidebar.items if row.link_to == route]
			workspace_rows = [row for row in workspace.links if row.link_to == route]
			self.assertEqual(len(sidebar_rows), 1)
			self.assertEqual(len(workspace_rows), 1)
			self.assertEqual(sidebar_rows[0].label, label)
			self.assertEqual(sidebar_rows[0].link_type, "Page")
			self.assertEqual(workspace_rows[0].label, label)
			self.assertEqual(workspace_rows[0].link_type, "Page")
			self.assertLess(
				next(index for index, row in enumerate(sidebar.items) if row.link_to == route),
				next(index for index, row in enumerate(sidebar.items) if row.link_to == "shortage-purchase-planning"),
			)
		core_card = next(row for row in workspace.links if row.type == "Card Break" and row.label == "核心流程")
		self.assertEqual(core_card.link_count, 8)
		workspace_roles = {row.role for row in workspace.roles}
		self.assertTrue(
			{"Production Worker", "Production Supervisor", "Production Wage Manager"}.issubset(
				workspace_roles
			)
		)

	def test_worker_reporting_upgrade_removes_legacy_pages_and_navigation(self):
		from process_simplification.patches.v0_0.add_worker_reporting_navigation import (
			LEGACY_PAGE_ROUTES,
			execute,
		)

		for route in LEGACY_PAGE_ROUTES:
			frappe.db.sql(
				"""
				insert ignore into `tabPage`
					(name, page_name, title, module, standard, owner, modified_by,
					 creation, modified, docstatus, idx)
				values
					(%(route)s, %(route)s, %(route)s, 'Process Simplification', 'Yes',
					 'Administrator', 'Administrator', now(6), now(6), 0, 0)
				""",
				{"route": route},
			)

		sidebar = frappe.get_doc("Workspace Sidebar", "process-simplification")
		workspace = frappe.get_doc("Workspace", "process-simplification")
		for route in LEGACY_PAGE_ROUTES:
			sidebar.append(
				"items",
				{"type": "Link", "label": route, "link_type": "Page", "link_to": route},
			)
			workspace.append(
				"links",
				{"type": "Link", "label": route, "link_type": "Page", "link_to": route},
			)
		sidebar.save(ignore_permissions=True)
		workspace.save(ignore_permissions=True)

		execute()

		sidebar.reload()
		workspace.reload()
		self.assertFalse(set(LEGACY_PAGE_ROUTES).intersection(row.link_to for row in sidebar.items))
		self.assertFalse(set(LEGACY_PAGE_ROUTES).intersection(row.link_to for row in workspace.links))
		for route in LEGACY_PAGE_ROUTES:
			self.assertFalse(frappe.db.exists("Page", route))
		core_card = next(
			row for row in workspace.links if row.type == "Card Break" and row.label == "核心流程"
		)
		self.assertEqual(core_card.link_count, 8)
