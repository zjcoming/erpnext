// Frappe v16 creates a Sidebar object during the setup wizard but returns from
// its constructor before making the DOM. Container.change_to still calls its
// toggle method, which otherwise aborts form navigation at wrapper.show/hide.
function installSidebarSetupCompatibility(frappeRef) {
	const NativeSidebar = frappeRef.ui?.Sidebar;
	if (!NativeSidebar || NativeSidebar.__ps_setup_compat) return false;

	class SetupAwareSidebar extends NativeSidebar {
		constructor(...args) {
			const setupPending = !frappeRef.boot?.setup_complete;
			super(...args);
			this.__ps_setup_deferred = setupPending && !this.wrapper;
			this.__ps_constructor_args = args;
		}

		toggle(...args) {
			if (!this.__ps_setup_deferred) return super.toggle(...args);
			if (this.__ps_ready_sidebar) return this.__ps_ready_sidebar.toggle(...args);
			// No sidebar is expected while the native setup wizard is incomplete.
			if (!frappeRef.boot?.setup_complete) return;
			// Only replace the active instance known to have skipped construction.
			// Unexpected missing DOM on any other instance remains a native error.
			if (frappeRef.app?.sidebar !== this) return super.toggle(...args);

			const sidebar = new SetupAwareSidebar(...this.__ps_constructor_args);
			this.__ps_ready_sidebar = sidebar;
			frappeRef.app.sidebar = sidebar;
			sidebar.set_workspace_sidebar();
			return sidebar.toggle(...args);
		}
	}
	SetupAwareSidebar.__ps_setup_compat = true;
	frappeRef.ui.Sidebar = SetupAwareSidebar;
	return true;
}

if (typeof module !== "undefined" && module.exports) {
	module.exports = { installSidebarSetupCompatibility };
}
if (typeof frappe !== "undefined") installSidebarSetupCompatibility(frappe);
