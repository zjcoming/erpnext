(function () {
	"use strict";
	const landing = frappe.boot?.process_role_landing;
	if (!landing?.route || !frappe.boot.setup_complete) return;
	const isEntry = (path) => /^(?:\/(?:desk|app))?\/?(?:process-simplification)?\/?$/.test(path);
	// Only the initial generic entry is redirected. A later click on 工作台 stays there.
	if (!isEntry(window.location.pathname) || window.location.hash) return;
	const savedRoute = localStorage.getItem("session_last_route");
	if (savedRoute && !isEntry(savedRoute)) return;
	if (savedRoute) localStorage.removeItem("session_last_route");
	const target = new URL(window.location.href);
	target.pathname = `/desk/${landing.route}`;
	target.searchParams.set("sidebar", "Process Simplification");
	window.history.replaceState(window.history.state, "", target.pathname + target.search);
})();
