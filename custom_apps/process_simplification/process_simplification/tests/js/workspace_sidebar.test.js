const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

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
	test(`${pageName} restores the process simplification sidebar on refresh`, () => {
		let restoreCount = 0;
		const context = {
			module: { exports: {} },
			exports: {},
			__: (message) => message,
			frappe: {
				pages: { [pageName]: {} },
				get_route: () => [],
				app: {
					sidebar: {
						set_workspace_sidebar: () => {
							restoreCount += 1;
						},
					},
				},
			},
		};
		const pagePath = path.join(pageDirectory, pageName.replaceAll("-", "_"), `${pageName.replaceAll("-", "_")}.js`);

		vm.runInNewContext(fs.readFileSync(pagePath, "utf8"), context, { filename: pagePath });
		assert.equal(typeof context.frappe.pages[pageName].refresh, "function");
		context.frappe.pages[pageName].refresh({ page: {} });
		assert.equal(restoreCount, 1);
	});
}

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
