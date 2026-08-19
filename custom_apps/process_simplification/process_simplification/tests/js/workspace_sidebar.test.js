const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const pageDirectory = path.resolve(__dirname, "../../process_simplification/page");
const appDirectory = path.resolve(__dirname, "../..");

for (const pageName of [
	"quick-sales-order",
	"order-workbench",
	"production-workbench",
	"my-production-reporting",
	"production-report-review",
	"shortage-purchase-planning",
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
