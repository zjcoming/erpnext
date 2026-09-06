(function () {
	"use strict";

	const WORKER_URL = "/api/method/process_simplification.pwa.service_worker";
	const SCOPE = "/desk";
	const STORAGE_PREFIX = "process_simplification:pwa:";
	const MANIFEST_ID = "process-pwa-manifest";
	const DAY = 86400000;

	function isIOS(win) {
		return /iPhone|iPad|iPod/i.test(win.navigator.userAgent) ||
			(win.navigator.platform === "MacIntel" && win.navigator.maxTouchPoints > 1);
	}

	function isMobile(win) {
		return isIOS(win) || /Android/i.test(win.navigator.userAgent) ||
			win.navigator.userAgentData?.mobile === true ||
			!!win.matchMedia?.("(max-width: 767px)").matches;
	}

	function isStandalone(win) {
		return win.navigator.standalone === true ||
			!!win.matchMedia?.("(display-mode: standalone)").matches ||
			!!win.matchMedia?.("(display-mode: fullscreen)").matches;
	}

	function escapeHTML(value) {
		return String(value ?? "").replace(/[&<>"']/g, (char) =>
			({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
	}

	function secureEntry(config) {
		try {
			const url = new URL(config.https_url);
			if (url.protocol !== "https:" || url.username || url.password) return null;
			return new URL(SCOPE, url.origin).href;
		} catch (_) { return null; }
	}

	function ownsWorker(registration, origin) {
		const workers = [registration.active, registration.waiting, registration.installing].filter(Boolean);
		return registration.scope === new URL(SCOPE, origin).href && workers.length > 0 &&
			workers.every((worker) => {
				const url = new URL(worker.scriptURL, origin);
				return url.origin === origin && url.pathname === WORKER_URL;
			});
	}

	async function setupWorker(win, enabled) {
		if (!win.isSecureContext) return { state: enabled ? "insecure" : "disabled" };
		const serviceWorker = win.navigator.serviceWorker;
		if (!serviceWorker?.getRegistrations) return { state: enabled ? "unsupported" : "disabled" };
		try {
			const origin = win.location.origin;
			const scope = new URL(SCOPE, origin).href;
			const registrations = await serviceWorker.getRegistrations();
			if (!enabled) {
				for (const registration of registrations) {
					if (ownsWorker(registration, origin)) await registration.unregister();
				}
				return { state: "disabled" };
			}
			const conflict = registrations.find((registration) =>
				!ownsWorker(registration, origin) &&
				(scope.startsWith(registration.scope) || registration.scope.startsWith(scope)));
			if (conflict) return { state: "conflict", scope: conflict.scope };
			await serviceWorker.register(WORKER_URL, { scope: SCOPE, updateViaCache: "none" });
			return { state: "ready" };
		} catch (error) {
			return { state: "error", message: String(error.message || error) };
		}
	}

	function installationGuide(win, config, state = {}) {
		if (!config.enabled || !config.available) {
			return { kind: "disabled", text: "手机应用安装尚未开启，请联系管理员。" };
		}
		if (isStandalone(win)) {
			return { kind: "installed", text: "你已在桌面应用中，无需再次安装。" };
		}
		if (!win.isSecureContext) {
			return {
				kind: "insecure", text: "请使用 HTTPS 安全网址安装手机应用。",
				steps: [config.https_url ? "打开下方安全网址，登录后再点“安装应用”。" :
					"请联系管理员配置 HTTPS 访问网址，再用手机浏览器打开。"],
				url: secureEntry(config),
			};
		}
		if (["conflict", "manifest-conflict"].includes(state.state)) {
			return { kind: "conflict", text: "当前页面已有其他应用的安装配置，请管理员在设置中检查后再安装。" };
		}
		if (state.state === "error") {
			return { kind: "error", text: "安装准备未完成，请刷新后重试。若仍出现此提示，请管理员检查手机应用设置。" };
		}
		if (/MicroMessenger|QQ\/|FBAN|FBAV|Instagram|; wv\)/i.test(win.navigator.userAgent)) {
			return {
				kind: "embedded", text: "请先用手机浏览器打开系统。",
				steps: ["点右上角菜单，选择“在浏览器打开”，或复制网址。",
					isIOS(win) ? "使用 Safari 打开，然后点“安装应用”。" :
						"使用支持安装应用的浏览器（如 Chrome 或 Edge）打开，然后点“安装应用”。"],
			};
		}
		if (isIOS(win)) {
			return {
				kind: "ios", text: "将应用添加到 iPhone / iPad 主屏幕：",
				steps: ["在 Safari 中打开当前系统网址。", "点“共享”（部分版本需先点“更多”）。",
					"选择“添加到主屏幕”；若显示“作为网页 App 打开”，请开启，再点“添加”。"],
			};
		}
		return {
			kind: isMobile(win) ? "android" : "desktop",
			text: "如果没有出现安装确认，可以从浏览器菜单安装：",
			steps: ["打开浏览器右上角菜单。", "选择“安装应用”或“添加到主屏幕”，按提示确认。",
				"若没有这些选项，请用支持 PWA 的浏览器打开此网址后重试。"],
		};
	}

	function createController({ windowRef: win, frappeRef: frappe, jQueryRef: $, now = Date.now }) {
		const doc = win.document;
		let config = { ...frappe.boot?.process_pwa };
		let deferredPrompt = null;
		let prompting = false;
		let installedThisPage = false;
		let ready = false;
		let started = false;
		let banner = null;
		let state = { state: "checking" };
		let setupQueue = Promise.resolve();
		let bannerShownThisPage = false;
		let storage;
		try { storage = win.localStorage; } catch (_) { /* Private browsing. */ }
		const read = (key) => { try { return storage?.getItem(STORAGE_PREFIX + key); } catch (_) { return null; } };
		const write = (key, value) => { try { storage?.setItem(STORAGE_PREFIX + key, String(value)); } catch (_) { /* Optional. */ } };
		const remove = (key) => { try { storage?.removeItem(STORAGE_PREFIX + key); } catch (_) { /* Optional. */ } };

		function dismissBanner() {
			banner?.remove();
			banner = null;
		}

		function updateMetadata() {
			const enabled = config.enabled && config.available;
			if (!enabled) {
				doc.querySelectorAll("[data-process-pwa]").forEach((element) => element.remove());
				return true;
			}
			const existing = doc.querySelector('link[rel~="manifest"]');
			if (existing && existing.id !== MANIFEST_ID) return false;
			const ensure = (tag, id, attributes) => {
				let element = doc.getElementById(id);
				if (!element) {
					element = doc.createElement(tag);
					element.id = id;
					element.setAttribute("data-process-pwa", "1");
					doc.head.appendChild(element);
				}
				Object.entries(attributes).forEach(([name, value]) => element.setAttribute(name, value));
			};
			ensure("link", MANIFEST_ID, {
				rel: "manifest", href: config.manifest_url, crossorigin: "use-credentials",
			});
			if (!doc.querySelector('link[rel="apple-touch-icon"]') || doc.getElementById("process-pwa-apple-icon")) {
				ensure("link", "process-pwa-apple-icon", { rel: "apple-touch-icon", href: config.icon_url });
			}
			if (!doc.querySelector('meta[name="apple-mobile-web-app-title"]') || doc.getElementById("process-pwa-apple-title")) {
				ensure("meta", "process-pwa-apple-title", { name: "apple-mobile-web-app-title", content: config.short_name });
			}
			return true;
		}

		function showGuide() {
			const guide = installationGuide(win, config, state);
			const dialog = new frappe.ui.Dialog({
				title: "安装应用", size: "small",
				fields: [{ fieldtype: "HTML", fieldname: "guide" }],
				primary_action_label: guide.url ? "打开安全网址" : "知道了",
				primary_action() {
					dialog.hide();
					if (guide.url) win.location.assign(guide.url);
				},
			});
			dialog.fields_dict.guide.$wrapper.html(`<div class="process-pwa-guide">
				<p>${escapeHTML(guide.text)}</p>
				${guide.steps ? `<ol>${guide.steps.map((step) => `<li>${escapeHTML(step)}</li>`).join("")}</ol>` : ""}
				${guide.url ? `<p>${escapeHTML(guide.url)}</p>` : ""}
				<p class="text-muted">安装后沿用现有账号和权限，报工与审批需要联网。</p></div>`);
			dialog.show();
			return guide.kind;
		}

		async function install() {
			if (prompting) return "busy";
			dismissBanner();
			const guide = installationGuide(win, config, state);
			if (!deferredPrompt || !["android", "desktop", "ios"].includes(guide.kind)) return showGuide();
			const event = deferredPrompt;
			deferredPrompt = null; // A browser install event can only be used once.
			prompting = true;
			write("last_prompt", now());
			try {
				// Call prompt before the first await, while the user's click is still active.
				const result = await event.prompt();
				const choice = event.userChoice ? await event.userChoice : result;
				return choice?.outcome || "dismissed";
			} catch (_) {
				return showGuide();
			} finally {
				prompting = false;
			}
		}

		function maybeShowBanner() {
			if (!ready || banner || bannerShownThisPage || !config.auto_prompt || !api.shouldShowMenu()) return;
			if (read("installed") || !["ready", "unsupported"].includes(state.state)) return;
			const lastPrompt = Number(read("last_prompt"));
			if (lastPrompt > 0 && now() - lastPrompt < (config.prompt_interval_days || 7) * DAY) return;
			bannerShownThisPage = true;
			write("last_prompt", now());
			banner = doc.createElement("aside");
			banner.className = "process-pwa-banner";
			banner.setAttribute("aria-label", "手机应用安装");
			banner.innerHTML = `<div class="process-pwa-banner-heading">
				<img src="${escapeHTML(config.icon_url)}" alt="" width="44" height="44">
				<strong>将${escapeHTML(config.short_name)}添加到桌面</strong></div>
				<p>下次直接点手机桌面图标，就能进入系统。</p>
				<div class="process-pwa-banner-actions"><button type="button" class="btn btn-default" data-pwa-later>稍后</button>
				<button type="button" class="btn btn-primary" data-pwa-install>安装应用</button></div>`;
			banner.querySelector("[data-pwa-later]").addEventListener("click", dismissBanner);
			banner.querySelector("[data-pwa-install]").addEventListener("click", install);
			doc.body.appendChild(banner);
		}

		const api = {
			install,
			shouldShowMenu: () => !!(config.enabled && config.available && isMobile(win) && !isStandalone(win) && !installedThisPage),
			getState: () => ({ ...state, secure: !!win.isSecureContext, standalone: isStandalone(win) }),
			configure(nextConfig) {
				config = { ...nextConfig };
				if (!config.enabled || !config.available || !config.auto_prompt) dismissBanner();
				setupQueue = setupQueue.then(async () => {
					if (!updateMetadata()) {
						state = { state: "manifest-conflict" };
					} else {
						state = await setupWorker(win, !!(config.enabled && config.available));
					}
					if (["conflict", "manifest-conflict", "disabled"].includes(state.state)) deferredPrompt = null;
					maybeShowBanner();
					return api.getState();
				});
				return setupQueue;
			},
			start() {
				if (started) return;
				started = true;
				// sessions.get replaces Navbar Settings after boot_session hooks. Extend the
				// final client copy before SidebarHeader is built, without changing core data.
				const dropdown = frappe.boot?.navbar_settings?.settings_dropdown;
				if (dropdown && !dropdown.some((item) => item.name === "process-pwa-install")) {
					dropdown.push({
						name: "process-pwa-install", item_label: "安装应用", item_type: "Action",
						icon: "download", condition: api.shouldShowMenu, onClick: install,
					});
				}
				win.addEventListener("beforeinstallprompt", (event) => {
					if (!config.enabled || !config.available || ["conflict", "manifest-conflict"].includes(state.state)) return;
					event.preventDefault();
					deferredPrompt = event;
					installedThisPage = false;
					remove("installed");
					maybeShowBanner();
				});
				win.addEventListener("appinstalled", () => {
					installedThisPage = true;
					deferredPrompt = null;
					write("installed", now());
					dismissBanner();
				});
				win.matchMedia?.("(display-mode: standalone)").addEventListener?.("change", () => {
					if (isStandalone(win)) dismissBanner();
				});
				$(doc).on("desktop_screen.process_pwa", (_event, { desktop }) => {
					desktop.add_menu_item({
						label: "安装应用", icon: "download", order: 25,
						condition: api.shouldShowMenu, onClick: install,
					});
				});
				const onReady = () => {
					if (ready) return;
					ready = true;
					win.setTimeout(maybeShowBanner, 1200);
				};
				$(doc).one("app_ready.process_pwa", onReady);
				$(onReady);
				api.configure(config);
			},
		};
		return api;
	}

	const exported = { isIOS, isMobile, isStandalone, secureEntry, ownsWorker, setupWorker, installationGuide, createController };
	if (typeof module !== "undefined" && module.exports) module.exports = exported;
	if (typeof window !== "undefined" && window.frappe && window.jQuery) {
		window.process_simplification = window.process_simplification || {};
		if (!window.process_simplification.pwa) {
			window.process_simplification.pwa = createController({ windowRef: window, frappeRef: window.frappe, jQueryRef: window.jQuery });
			window.process_simplification.pwa.start();
		}
	}
})();
