// Ancestors returned only to connect the tree are navigation, not document grants.
(() => {
	const settings = frappe.treeview_settings.Warehouse;
	const previous = settings.onrender;
	settings.onrender = function (node) {
		previous?.call(this, node);
		if (node.data?.ps_navigation_only) {
			node.$toolbar?.remove();
			node.$toolbar = null;
			node.hide_add = true;
		}
	};
})();
