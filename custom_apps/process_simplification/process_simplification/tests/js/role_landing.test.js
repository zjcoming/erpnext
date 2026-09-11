const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const script = fs.readFileSync(path.join(__dirname, "../../public/js/role_landing.js"), "utf8");

function open(url, { landing = { route: "executive-dashboard" }, saved, setup = true } = {}) {
	const redirects = [];
	const storage = new Map(saved ? [["session_last_route", saved]] : []);
	const context = {
		URL,
		frappe: { boot: { process_role_landing: landing, setup_complete: setup } },
		window: {
			location: new URL(url, "https://erp.example.com"),
			history: { state: { test: true }, replaceState: (state, title, route) => redirects.push(route) },
		},
		localStorage: { getItem: (key) => storage.get(key), removeItem: (key) => storage.delete(key) },
	};
	vm.runInNewContext(script, context);
	return { redirects, storage };
}

test("generic login and app entries select the configured page before first render", () => {
	for (const route of ["/desk", "/desk/", "/app", "/desk/process-simplification", "/app/process-simplification/"]) {
		assert.deepEqual(open(route).redirects, ["/desk/executive-dashboard?sidebar=Process+Simplification"]);
	}
});

test("PWA entry parameters survive the default route", () => {
	assert.equal(open("/desk?pwa=1").redirects[0], "/desk/executive-dashboard?pwa=1&sidebar=Process+Simplification");
});

test("documents, notification details, explicit desktop, and other workspaces stay intact", () => {
	for (const route of ["/desk/sales-order/SO-001", "/desk/purchase-receipt-notice?name=abc", "/desk/desktop", "/desk/accounting", "/desk#Form/Sales%20Order/SO-001"]) {
		assert.deepEqual(open(route).redirects, []);
	}
});

test("a pending session route to a document is retained", () => {
	const result = open("/desk", { saved: "sales-order/SO-001" });
	assert.deepEqual(result.redirects, []);
	assert.equal(result.storage.get("session_last_route"), "sales-order/SO-001");
});

test("a restored generic workspace follows the configured role instead", () => {
	const result = open("/desk", { saved: "process-simplification" });
	assert.equal(result.redirects.length, 1);
	assert.equal(result.storage.has("session_last_route"), false);
});

test("disabled, unmatched and setup-wizard entries keep the native behavior", () => {
	assert.deepEqual(open("/desk", { landing: null }).redirects, []);
	assert.deepEqual(open("/desk", { setup: false }).redirects, []);
});

test("wage managers can land on a DocType list route", () => {
	assert.equal(open("/desk", { landing: { route: "monthly-worker-wage-summary" } }).redirects[0],
		"/desk/monthly-worker-wage-summary?sidebar=Process+Simplification");
});
