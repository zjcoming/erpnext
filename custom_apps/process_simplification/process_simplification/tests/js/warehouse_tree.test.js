const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.resolve(__dirname, "../../public/js/warehouse_tree.js"), "utf8");

test("navigation-only groups can expand but expose no document action toolbar", () => {
	let removed = false;
	const settings = {};
	vm.runInNewContext(script, { frappe: { treeview_settings: { Warehouse: settings } } });
	const node = { data: { ps_navigation_only: true }, expandable: 1, $toolbar: { remove() { removed = true; } } };
	settings.onrender(node);
	assert.equal(removed, true);
	assert.equal(node.$toolbar, null);
	assert.equal(node.hide_add, true);
	assert.equal(node.expandable, 1);
});

test("authorized warehouses and the virtual company root retain their native actions and renderer", () => {
	const rendered = [];
	const settings = { onrender(node) { rendered.push([this, node]); } };
	vm.runInNewContext(script, { frappe: { treeview_settings: { Warehouse: settings } } });
	for (const data of [{ value: "Warehouse" }, { value: "Company", is_root: true }]) {
		const toolbar = { remove() { assert.fail("native toolbar was removed"); } };
		const node = { data, $toolbar: toolbar };
		settings.onrender(node);
		assert.equal(node.$toolbar, toolbar);
		assert.equal(node.hide_add, undefined);
		assert.equal(rendered.at(-1)[0], settings);
		assert.equal(rendered.at(-1)[1], node);
	}
});
