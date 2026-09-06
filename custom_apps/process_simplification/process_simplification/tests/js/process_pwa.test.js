const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const { setupWorker, installationGuide, createController } = require("../../public/js/process_pwa.js");

const ORIGIN = "https://factory.example.com";
const WORKER = "/api/method/process_simplification.pwa.service_worker";
const CONFIG = {
	enabled: true, available: true, auto_prompt: true, prompt_interval_days: 7,
	app_name: "工厂工作台", short_name: "工厂工作台", scope: "/desk",
	manifest_url: "/api/method/process_simplification.pwa.manifest", icon_url: "/assets/process_simplification/images/pwa/icon-192.png",
};

function browser(overrides = {}) {
	return {
		location: { origin: ORIGIN }, isSecureContext: true,
		navigator: { userAgent: "Android Chrome", platform: "Linux", maxTouchPoints: 1 },
		matchMedia: () => ({ matches: false, addEventListener() {} }), ...overrides,
	};
}

function registration(scope, script) {
	return { scope: ORIGIN + scope, active: { scriptURL: ORIGIN + script }, removed: false,
		async unregister() { this.removed = true; } };
}

test("HR PWA service worker stays registered while Desk uses its own scope", async () => {
	const hr = registration("/assets/hrms/frontend/", "/assets/hrms/frontend/sw.js");
	const calls = [];
	const win = browser();
	win.navigator.serviceWorker = {
		getRegistrations: async () => [hr], register: async (...args) => calls.push(args),
	};
	assert.equal((await setupWorker(win, true)).state, "ready");
	assert.deepEqual(calls, [[WORKER, { scope: "/desk", updateViaCache: "none" }]]);
	assert.equal(hr.removed, false);
});

test("another root, Desk, or nested Desk worker is never overwritten", async () => {
	for (const scope of ["/", "/desk", "/desk/custom/"]) {
		const other = registration(scope, "/other/sw.js");
		const win = browser();
		win.navigator.serviceWorker = {
			getRegistrations: async () => [other], register: async () => assert.fail("must not replace worker"),
		};
		assert.equal((await setupWorker(win, true)).state, "conflict");
		assert.equal(other.removed, false);
	}
});

test("disabling removes only this app's exact registration and leaves HR intact", async () => {
	const own = registration("/desk", WORKER + "?old=1");
	const hr = registration("/assets/hrms/frontend/", "/assets/hrms/frontend/sw.js");
	const win = browser();
	win.navigator.serviceWorker = { getRegistrations: async () => [own, hr] };
	assert.equal((await setupWorker(win, false)).state, "disabled");
	assert.equal(own.removed, true);
	assert.equal(hr.removed, false);
});

test("HTTP and registration failures produce useful guidance without forcing a redirect", async () => {
	const win = browser({ isSecureContext: false });
	assert.equal((await setupWorker(win, true)).state, "insecure");
	assert.equal(installationGuide(win, { ...CONFIG, https_url: "https://factory.example.com" }).url, ORIGIN + "/desk");
	assert.equal(installationGuide(win, { ...CONFIG, https_url: "javascript:alert(1)" }).url, null);
	const secure = browser();
	secure.navigator.serviceWorker = { getRegistrations: async () => { throw new Error("network"); } };
	assert.equal((await setupWorker(secure, true)).state, "error");
});

test("iPhone, embedded browser, Android, and standalone have different install guidance", () => {
	const iphone = browser({ navigator: { userAgent: "iPhone Safari" } });
	assert.equal(installationGuide(iphone, CONFIG).kind, "ios");
	assert.ok(installationGuide(iphone, CONFIG).steps.join("").includes("添加到主屏幕"));
	const embedded = browser({ navigator: { userAgent: "Android MicroMessenger" } });
	assert.equal(installationGuide(embedded, CONFIG).kind, "embedded");
	assert.equal(installationGuide(browser(), CONFIG).kind, "android");
	iphone.navigator.standalone = true;
	assert.equal(installationGuide(iphone, CONFIG).kind, "installed");
});

function controllerHarness({ config = CONFIG, storage = new Map(), standalone = false, now = 1700000000000 } = {}) {
	const nodes = [];
	const listeners = {};
	const jqListeners = {};
	const dialogs = [];
	function element(tag) {
		const el = { tag, attrs: {}, children: [], buttons: {}, removed: false,
			setAttribute(name, value) { this.attrs[name] = value; },
			appendChild(child) { this.children.push(child); nodes.push(child); },
			remove() { this.removed = true; },
			querySelector(selector) { return this.buttons[selector] ||= { addEventListener(type, cb) { this[type] = cb; } }; },
		};
		return el;
	}
	const doc = {
		head: element("head"), body: element("body"), createElement: element,
		getElementById: (id) => nodes.find((node) => !node.removed && node.id === id),
		querySelectorAll: () => nodes.filter((node) => !node.removed && node.attrs["data-process-pwa"]),
		querySelector: (selector) => nodes.find((node) => !node.removed && (
			selector.includes("manifest") ? node.attrs.rel === "manifest" :
			selector.includes("apple-touch-icon") ? node.attrs.rel === "apple-touch-icon" :
			selector.includes("apple-mobile-web-app-title") && node.attrs.name === "apple-mobile-web-app-title")),
	};
	const win = browser({ document: doc, localStorage: {
		getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key),
	}, addEventListener: (name, cb) => { listeners[name] = cb; }, setTimeout: (cb) => cb() });
	win.navigator.standalone = standalone;
	win.navigator.serviceWorker = { getRegistrations: async () => [], register: async () => ({}) };
	const frappe = { boot: { process_pwa: config, navbar_settings: { settings_dropdown: [] } }, ui: {
		Dialog: class { constructor(options) { dialogs.push(options); this.fields_dict = { guide: { $wrapper: { html() {} } } }; } show() {} hide() {} },
	} };
	const $ = (target) => {
		if (typeof target === "function") { target(); return; }
		return { on: (name, cb) => { jqListeners[name] = cb; }, one: (name, cb) => { jqListeners[name] = cb; } };
	};
	const api = createController({ windowRef: win, frappeRef: frappe, jQueryRef: $, now: () => now });
	return { api, win, doc, frappe, listeners, jqListeners, dialogs, nodes, storage };
}

test("both menu locations are wired once and the avatar entry follows the theme item", async () => {
	const h = controllerHarness();
	h.api.start();
	h.api.start();
	await h.api.configure(CONFIG);
	const items = [];
	h.jqListeners["desktop_screen.process_pwa"]({}, { desktop: { add_menu_item: (item) => items.push(item) } });
	assert.equal(items[0].label, "安装应用");
	assert.equal(items[0].order, 25);
	assert.equal(items[0].condition(), true);
	assert.equal(h.frappe.boot.navbar_settings.settings_dropdown.length, 1);
});

test("native installation is called during click, only once, and accepted is not mistaken for appinstalled", async () => {
	const h = controllerHarness();
	h.api.start();
	await h.api.configure(CONFIG);
	let invoked = 0;
	const event = { preventDefault() {}, prompt() { invoked++; return Promise.resolve(); }, userChoice: Promise.resolve({ outcome: "accepted" }) };
	h.listeners.beforeinstallprompt(event);
	const result = h.api.install();
	assert.equal(invoked, 1);
	assert.equal(await result, "accepted");
	assert.equal(h.storage.has("process_simplification:pwa:installed"), false);
	await h.api.install();
	assert.equal(invoked, 1);
	h.listeners.appinstalled();
	assert.equal(h.api.shouldShowMenu(), false);
	assert.equal(h.storage.has("process_simplification:pwa:installed"), true);
});

test("dismissal cooldown suppresses the next page's banner but keeps its install menu", async () => {
	const h = controllerHarness();
	h.api.start();
	await h.api.configure(CONFIG);
	const banner = h.nodes.find((node) => node.className === "process-pwa-banner");
	assert.ok(banner);
	banner.buttons["[data-pwa-later]"].click();
	assert.equal(banner.removed, true);
	const next = controllerHarness({ storage: h.storage, now: 1700000000000 + 60000 });
	next.api.start();
	await next.api.configure(CONFIG);
	assert.equal(next.nodes.some((node) => node.className === "process-pwa-banner"), false);
	assert.equal(next.api.shouldShowMenu(), true);
});

test("standalone, disabled, and reminder-off sessions are not prompted", async () => {
	for (const options of [{ standalone: true }, { config: { ...CONFIG, enabled: false } }, { config: { ...CONFIG, auto_prompt: false } }]) {
		const h = controllerHarness(options);
		h.api.start();
		await h.api.configure(options.config || CONFIG);
		assert.equal(h.nodes.some((node) => node.className === "process-pwa-banner"), false);
	}
});

test("a foreign manifest is preserved and blocks our install flow", async () => {
	const h = controllerHarness();
	const manifest = h.doc.createElement("link");
	manifest.setAttribute("rel", "manifest");
	manifest.setAttribute("href", "/other/manifest.json");
	h.doc.head.appendChild(manifest);
	assert.equal((await h.api.configure(CONFIG)).state, "manifest-conflict");
	assert.equal(manifest.attrs.href, "/other/manifest.json");
	assert.equal(h.doc.getElementById("process-pwa-manifest"), undefined);
});

test("worker only falls back on failed Desk navigation and never caches or intercepts writes or HR", async () => {
	const handlers = {};
	vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, "../../public/js/process_pwa_worker.js"), "utf8"), {
		URL, Response, self: { location: { origin: ORIGIN }, addEventListener: (name, cb) => { handlers[name] = cb; } },
		fetch: async () => { throw new Error("offline"); },
		caches: { open: () => assert.fail("must not cache private data") },
	});
	for (const request of [
		{ method: "POST", mode: "navigate", url: ORIGIN + "/desk" },
		{ method: "GET", mode: "cors", url: ORIGIN + "/api/method/submit" },
		{ method: "GET", mode: "navigate", url: ORIGIN + "/hrms" },
	]) handlers.fetch({ request, respondWith: () => assert.fail("must not intercept") });
	let response;
	handlers.fetch({ request: { method: "GET", mode: "navigate", url: ORIGIN + "/desk/my-production-reporting" }, respondWith: (value) => { response = value; } });
	const offline = await response;
	assert.equal(offline.status, 503);
	assert.ok((await offline.text()).includes("请连接网络后重试"));
});
