const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../../public/js/warehouse_management.js"), "utf8");

function setup({ roles = ["System Manager"], write = true, isNew = false, parent = "All - A", dirty = false } = {}) {
	const state = { buttons: [], messages: [], cleared: [], routes: [], calls: [] };
	const frappe = {
		session: { user: "manager@example.com" }, user: { has_role: (role) => roles.includes(role) },
		ui: { form: { on: (_, handlers) => { state.handlers = handlers; } }, Dialog: class {
			constructor(options) { state.dialog = this; this.options = options; }
			show() { this.visible = true; } hide() { this.visible = false; }
			disable_primary_action() { this.disabled = true; } enable_primary_action() { this.disabled = false; }
		} },
		msgprint: (message) => state.messages.push(message),
		call: async (args) => { state.calls.push(args); return { message: { name: "原料仓 - A" } }; },
		model: { clear_doc: (...args) => state.cleared.push(args) },
		set_route: async (...args) => state.routes.push(args), show_alert: () => {},
	};
	vm.runInNewContext(source, { frappe, __: String });
	state.frm = { doc: { name: "Stores - A", warehouse_name: "Stores", company: "Company A", parent_warehouse: parent },
		is_new: () => isNew, has_perm: () => write, is_dirty: () => dirty,
		add_custom_button: (label, callback) => state.buttons.push({ label, callback }) };
	state.handlers.refresh(state.frm);
	return { state, frappe };
}

test("only a saved non-root warehouse with manager role and write permission has rename", () => {
	assert.equal(setup().state.buttons[0].label, "修改名称");
	for (const options of [{ roles: ["Process Simplification Owner"] }, { roles: ["Item Manager"] }, { write: false }, { isNew: true }, { parent: null }]) {
		assert.equal(setup(options).state.buttons.length, 0);
	}
});
test("unsaved edits must be saved before opening rename", () => {
	const { state } = setup({ dirty: true });
	state.buttons[0].callback();
	assert.equal(state.dialog, undefined);
	assert.match(state.messages[0], /请先保存/);
});
test("rename uses POST and opens the server-returned warehouse after success", async () => {
	const { state } = setup();
	state.buttons[0].callback();
	await state.dialog.options.primary_action({ new_name: "原料仓" });
	assert.equal(state.calls[0].type, "POST");
	assert.equal(state.calls[0].args.warehouse, "Stores - A");
	assert.equal(state.calls[0].args.new_name, "原料仓");
	assert.deepEqual(state.routes[0], ["Form", "Warehouse", "原料仓 - A"]);
	assert.equal(state.dialog.visible, false);
});
test("failed rename keeps the dialog open and restores the action", async () => {
	const { state, frappe } = setup();
	let refreshed = false;
	frappe.views = { trees: { Warehouse: { tree: {}, make_tree: () => { refreshed = true; } } } };
	frappe.call = async () => { throw Error("permission denied"); };
	state.buttons[0].callback();
	await assert.rejects(state.dialog.options.primary_action({ new_name: "原料仓" }), /permission denied/);
	assert.equal(state.dialog.visible, true);
	assert.equal(state.dialog.disabled, false);
	assert.equal(state.routes.length, 0);
	assert.equal(refreshed, false);
});
test("successful rename refreshes the cached native Warehouse tree with its filters intact", async () => {
	const { state, frappe } = setup();
	const filters = { doctype: "Warehouse", company: "Company A", include_disabled: true };
	let refreshCount = 0;
	const treeview = { args: filters, tree: { nodes: { "Stores - A": {} } }, make_tree() {
		assert.equal(this, treeview);
		assert.equal(this.args, filters);
		assert.equal(state.routes.length, 0);
		refreshCount++;
		this.tree = { nodes: { "原料仓 - A": {} } };
	} };
	const otherTree = { make_tree: () => assert.fail("unrelated tree must not refresh") };
	frappe.views = { trees: { Warehouse: treeview, Item: otherTree } };
	state.buttons[0].callback();
	await state.dialog.options.primary_action({ new_name: "原料仓" });
	assert.equal(refreshCount, 1);
	assert.equal(treeview.tree.nodes["Stores - A"], undefined);
	assert.ok(treeview.tree.nodes["原料仓 - A"]);
	assert.equal(frappe.views.trees.Warehouse, treeview);
	assert.deepEqual(state.routes[0], ["Form", "Warehouse", "原料仓 - A"]);
});
test("a tree view still loading its root is not prematurely rebuilt", async () => {
	const { state, frappe } = setup();
	frappe.views = { trees: { Warehouse: { make_tree: () => assert.fail("root not ready") } } };
	state.buttons[0].callback();
	await state.dialog.options.primary_action({ new_name: "原料仓" });
	assert.equal(state.routes.length, 1);
});
test("double clicks submit only one rename", async () => {
	const { state, frappe } = setup();
	let finish;
	frappe.call = (args) => { state.calls.push(args); return new Promise((resolve) => { finish = resolve; }); };
	state.buttons[0].callback();
	const first = state.dialog.options.primary_action({ new_name: "原料仓" });
	await state.dialog.options.primary_action({ new_name: "原料仓" });
	assert.equal(state.calls.length, 1);
	finish({ message: { name: "原料仓 - A" } });
	await first;
});
