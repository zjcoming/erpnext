const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const pageDirectory = path.resolve(__dirname, "../../process_simplification/page");

for (const pageName of [
	"quick-sales-order",
	"order-workbench",
	"production-workbench",
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
