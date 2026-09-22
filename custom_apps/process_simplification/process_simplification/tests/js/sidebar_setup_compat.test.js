const test = require("node:test");
const assert = require("node:assert/strict");
const { installSidebarSetupCompatibility } = require("../../public/js/sidebar_setup_compat.js");

function fixture(setupComplete = false) {
	const state = { constructed: [], rendered: [], routeSetups: [], toggles: [] };
	const frappe = { boot: { setup_complete: setupComplete }, app: {}, ui: {} };
	// This contract reproduces the pinned Frappe Sidebar constructor's early
	// return and Container's unconditional toggle call, without a browser DOM.
	frappe.ui.Sidebar = class NativeSidebar {
		constructor(options) {
			state.constructed.push({ instance: this, options });
			if (!frappe.boot.setup_complete) return;
			state.rendered.push(this);
			this.wrapper = {
				show: () => state.toggles.push({ instance: this, hidden: false }),
				hide: () => state.toggles.push({ instance: this, hidden: true }),
			};
			// Event handlers must retain the live instance, not an Object.assign
			// copy of constructor state with callbacks closing over another object.
			this.nativeEvent = () => this;
		}
		toggle(hidden) { return hidden ? this.wrapper.hide() : this.wrapper.show(); }
		set_workspace_sidebar() { state.routeSetups.push(this); }
	};
	return { frappe, state };
}

test("pinned native contract reproduces the setup-time navigation exception", () => {
	const { frappe } = fixture();
	const sidebar = new frappe.ui.Sidebar();
	assert.throws(() => sidebar.toggle(false), /show/);
});

test("navigation can finish during setup without constructing or showing a sidebar", () => {
	const { frappe, state } = fixture();
	installSidebarSetupCompatibility(frappe);
	frappe.app.sidebar = new frappe.ui.Sidebar();
	assert.doesNotThrow(() => frappe.app.sidebar.toggle(false));
	assert.doesNotThrow(() => frappe.app.sidebar.toggle(true));
	assert.equal(state.rendered.length, 0);
	assert.equal(state.constructed.length, 1);
	assert.equal(state.toggles.length, 0);
});

test("first toggle after setup reconstructs once, restores route and keeps native event ownership", () => {
	const { frappe, state } = fixture();
	installSidebarSetupCompatibility(frappe);
	const options = { source: "native-app" };
	const deferred = frappe.app.sidebar = new frappe.ui.Sidebar(options);
	frappe.boot.setup_complete = true;
	deferred.toggle(true);
	const active = frappe.app.sidebar;
	assert.notEqual(active, deferred);
	assert.equal(state.constructed.length, 2);
	assert.equal(state.constructed[1].options, options);
	assert.equal(active.nativeEvent(), active);
	assert.deepEqual(state.routeSetups, [active]);
	assert.deepEqual(state.toggles, [{ instance: active, hidden: true }]);
	deferred.toggle(false);
	active.toggle(true);
	assert.equal(state.constructed.length, 2);
	assert.equal(state.routeSetups.length, 1);
	assert.equal(state.toggles.length, 3);
});

test("ordinary completed setup retains the native instance and toggle semantics", () => {
	const { frappe, state } = fixture(true);
	installSidebarSetupCompatibility(frappe);
	const original = frappe.app.sidebar = new frappe.ui.Sidebar();
	original.toggle(false);
	original.toggle(true);
	assert.equal(frappe.app.sidebar, original);
	assert.equal(state.constructed.length, 1);
	assert.deepEqual(state.toggles.map((row) => row.hidden), [false, true]);
	assert.equal(state.routeSetups.length, 0);
});

test("unexpected missing DOM and route errors are not swallowed", () => {
	const { frappe } = fixture(true);
	installSidebarSetupCompatibility(frappe);
	frappe.app.sidebar = new frappe.ui.Sidebar();
	delete frappe.app.sidebar.wrapper;
	assert.throws(() => frappe.app.sidebar.toggle(false), /show/);

	const pending = fixture();
	installSidebarSetupCompatibility(pending.frappe);
	pending.frappe.app.sidebar = new pending.frappe.ui.Sidebar();
	pending.frappe.ui.Sidebar.prototype.set_workspace_sidebar = () => { throw Error("route failure"); };
	pending.frappe.boot.setup_complete = true;
	assert.throws(() => pending.frappe.app.sidebar.toggle(false), /route failure/);
});

test("duplicate asset evaluation does not layer wrappers or register extra handlers", () => {
	const { frappe } = fixture();
	assert.equal(installSidebarSetupCompatibility(frappe), true);
	const wrapped = frappe.ui.Sidebar;
	assert.equal(installSidebarSetupCompatibility(frappe), false);
	assert.equal(frappe.ui.Sidebar, wrapped);
});
