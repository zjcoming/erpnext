const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const pageDirectory = path.resolve(__dirname, "../../process_simplification/page");
const appDirectory = path.resolve(__dirname, "../..");
const stylesheetPath = path.resolve(__dirname, "../../public/css/process_simplification.css");

for (const pageName of [
	"quick-sales-order",
	"order-workbench",
	"production-workbench",
	"active-production-work",
	"my-production-reporting",
	"production-report-history",
	"production-report-review",
	"shortage-purchase-planning",
	"executive-dashboard",
	"process-access-management",
]) {
	test(`${pageName} leaves sidebar lifecycle to the native router`, () => {
		const pagePath = path.join(pageDirectory, pageName.replaceAll("-", "_"), `${pageName.replaceAll("-", "_")}.js`);
		const pageJavascript = fs.readFileSync(pagePath, "utf8");

		assert.doesNotMatch(pageJavascript, /set_workspace_sidebar/);
	});
}

test("workspace sidebar uses the native module identity while the workspace keeps its route slug", () => {
	const sidebar = JSON.parse(
		fs.readFileSync(
			path.join(appDirectory, "workspace_sidebar", "process_simplification.json"),
			"utf8"
		)
	);

	assert.equal(sidebar.name, "Process Simplification");
	assert.equal(sidebar.title, "Process Simplification");
	assert.equal(sidebar.module, "Process Simplification");
	assert.equal(sidebar.standard, 1);
	assert.equal(sidebar.items[0].link_type, "Workspace");
	assert.equal(sidebar.items[0].link_to, "process-simplification");
	assert.equal(
		sidebar.items.find((item) => item.link_to === "production-report-review").icon,
		"file-check"
	);
});

test("management links and role-scoped pages are exported", () => {
	const sidebar = JSON.parse(
		fs.readFileSync(path.join(appDirectory, "workspace_sidebar", "process_simplification.json"), "utf8")
	);
	const executiveMetadata = JSON.parse(
		fs.readFileSync(path.join(pageDirectory, "executive_dashboard", "executive_dashboard.json"), "utf8")
	);
	const accessMetadata = JSON.parse(
		fs.readFileSync(
			path.join(pageDirectory, "process_access_management", "process_access_management.json"),
			"utf8"
		)
	);

	assert.equal(sidebar.items.filter((item) => item.link_to === "executive-dashboard").length, 1);
	assert.equal(sidebar.items.filter((item) => item.link_to === "process-access-management").length, 1);
	assert.deepEqual(executiveMetadata.roles.map((row) => row.role), ["Process Simplification Owner"]);
	assert.deepEqual(
		accessMetadata.roles.map((row) => row.role),
		["Process Simplification Access Manager", "System Manager"]
	);
});

test("collapsed production demand cards do not paint interactive details over later cards", () => {
	const stylesheet = fs.readFileSync(stylesheetPath, "utf8");

	assert.match(
		stylesheet,
		/\.production-demand:not\(\[open\]\)\s*>\s*\.production-demand-details\s*\{[^}]*display:\s*none;/s
	);
});

test("workspace sidebar groups business pages and hides permission-empty groups", () => {
	const sidebar = JSON.parse(
		fs.readFileSync(path.join(appDirectory, "workspace_sidebar", "process_simplification.json"), "utf8")
	);
	const expectedGroups = [
		["总览与报表", ["executive-dashboard"]],
		["销售与订单", ["quick-sales-order", "order-workbench"]],
		[
			"生产",
			[
				"production-workbench",
				"active-production-work",
				"my-production-reporting",
				"production-report-history",
				"production-report-review",
				"production-exception-review",
			],
		],
		["采购", ["shortage-purchase-planning"]],
		["工资管理", ["Operation Wage Rate", "Monthly Worker Wage Summary"]],
		["系统管理", ["Process Simplification Settings", "process-access-management"]],
	];

	assert.equal(sidebar.items[0].link_to, "process-simplification");
	assert.equal(sidebar.items[0].child, 0);

	let cursor = 1;
	for (const [label, links] of expectedGroups) {
		const section = sidebar.items[cursor++];
		assert.equal(section.type, "Section Break");
		assert.equal(section.label, label);
		assert.equal(section.collapsible, 1);
		for (const link of links) {
			const item = sidebar.items[cursor++];
			assert.equal(item.link_to, link);
			assert.equal(item.child, 1);
		}
	}
	assert.equal(cursor, sidebar.items.length);

	const visibleGroupsFor = (allowedLinks) => {
		const filtered = sidebar.items.filter(
			(item) => item.type === "Section Break" || allowedLinks.has(item.link_to)
		);
		const groups = [];
		let currentGroup = null;
		for (const item of filtered) {
			if (item.type === "Section Break") {
				currentGroup = { label: item.label, links: [] };
				groups.push(currentGroup);
			} else if (currentGroup && item.child) {
				currentGroup.links.push(item.link_to);
			}
		}
		return groups.filter((group) => group.links.length > 0);
	};

	assert.deepEqual(visibleGroupsFor(new Set([
		"active-production-work",
		"my-production-reporting",
		"production-report-history",
	])), [
		{
			label: "生产",
			links: ["active-production-work", "my-production-reporting", "production-report-history"],
		},
	]);
	assert.deepEqual(
		visibleGroupsFor(new Set(["Operation Wage Rate", "Monthly Worker Wage Summary"])),
		[
			{
				label: "工资管理",
				links: ["Operation Wage Rate", "Monthly Worker Wage Summary"],
			},
		]
	);
});

test("workbench focus uses query options and legacy path URLs are normalized", () => {
	const orderWorkbench = fs.readFileSync(
		path.join(pageDirectory, "order_workbench", "order_workbench.js"),
		"utf8"
	);
	const productionWorkbench = fs.readFileSync(
		path.join(pageDirectory, "production_workbench", "production_workbench.js"),
		"utf8"
	);

	assert.match(orderWorkbench, /set_route\("order-workbench", \{ sales_order: route\[1\] \}\)/);
	assert.match(productionWorkbench, /set_route\("production-workbench", \{ demand_key: route\[1\] \}\)/);
	assert.match(orderWorkbench, /frappe\.route_options\?\.sales_order/);
	assert.match(productionWorkbench, /frappe\.route_options\?\.demand_key/);
});

test("production plan center keeps its route and approved navigation identity", () => {
	const sidebar = JSON.parse(
		fs.readFileSync(
			path.join(appDirectory, "workspace_sidebar", "process_simplification.json"),
			"utf8"
		)
	);
	const workspace = JSON.parse(
		fs.readFileSync(
			path.join(
				appDirectory,
				"process_simplification",
				"workspace",
				"process_simplification",
				"process_simplification.json"
			),
			"utf8"
		)
	);
	const pagePath = path.join(pageDirectory, "production_workbench");
	const pageMetadata = JSON.parse(
		fs.readFileSync(path.join(pagePath, "production_workbench.json"), "utf8")
	);
	const pagePython = fs.readFileSync(path.join(pagePath, "production_workbench.py"), "utf8");
	const pageJavascript = fs.readFileSync(path.join(pagePath, "production_workbench.js"), "utf8");
	const sidebarItem = sidebar.items.find((item) => item.link_to === "production-workbench");
	const workspaceItem = workspace.links.find((item) => item.link_to === "production-workbench");

	assert.equal(sidebarItem.label, "生产计划中心");
	assert.equal(sidebarItem.icon, "factory");
	assert.equal(workspaceItem.label, "生产计划中心");
	assert.equal(pageMetadata.name, "production-workbench");
	assert.equal(pageMetadata.title, "生产计划中心");
	assert.match(pagePython, /return \{"title": "生产计划中心"\}/);
	assert.match(pageJavascript, /title: __\("生产计划中心"\)/);
});
