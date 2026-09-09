/* One visible-page coordinator; events invalidate data, never overwrite input. */
const PS_REFRESH_EVENT = "process_simplification_data_changed";
const PS_REFRESH_HEALTHY_MS = 120000;
const PS_REFRESH_OFFLINE_MS = 30000;

function createPageRefreshController(options) {
	const clock = options.now || Date.now;
	const later = options.setTimeout || setTimeout;
	const cancel = options.clearTimeout || clearTimeout;
	let active = null, timer = null, checkTimer = null, checking = null;
	let disposed = false, failures = 0, checkAgain = false;
	const known = {};
	const visible = () => !disposed && options.visible();
	function scheduleCheck() {
		if (checkTimer !== null) cancel(checkTimer);
		checkTimer = null;
		if (visible()) {
			const interval = failures ? Math.min(120000, 30000 * (2 ** (failures - 1))) :
				(options.connected() ? PS_REFRESH_HEALTHY_MS : PS_REFRESH_OFFLINE_MS);
			checkTimer = later(check, interval + (options.jitter?.(interval) || 0));
		}
	}
	function schedulePage() {
		if (timer !== null) cancel(timer);
		timer = null;
		const page = active;
		if (!visible() || !page?.dirty || page.pending) return;
		if (page.editable?.() || page.manual) {
			page.status?.("changed");
			return;
		}
		page.dueAt = page.dueAt || Math.max(clock() + 1000, (page.lastRead || 0) + page.interval, page.retryAt || 0);
		const wait = Math.max(0, page.dueAt - clock());
		page.status?.("waiting");
		timer = later(() => refresh(false), wait);
	}
	async function refresh(manual = false) {
		const page = active;
		if (!visible() || !page?.load || page.pending) return page?.pending;
		if (page.editable?.()) { page.status?.("changed"); return; }
		page.dirty = false;
		page.dueAt = null;
		page.lastRead = clock();
		page.status?.("loading");
		page.pending = Promise.resolve().then(() => page.load({ background: !manual })).then((applied) => {
			page.failures = 0;
			page.retryAt = 0;
			if (applied === false) page.dirty = true;
			page.status?.(page.dirty ? "waiting" : "fresh");
		}, () => {
			page.dirty = true;
			page.failures = (page.failures || 0) + 1;
			page.retryAt = clock() + Math.min(120000, 30000 * (2 ** (page.failures - 1)));
			page.status?.("error");
		}).finally(() => {
			page.pending = null;
			if (page === active) schedulePage();
		});
		return page.pending;
	}
	function invalidate(topics) {
		if (active?.topics.some((topic) => topics.includes(topic))) {
			active.dirty = true;
			active.revision = (active.revision || 0) + 1;
			schedulePage();
		}
	}
	async function check() {
		if (!visible()) return;
		if (checking) { checkAgain = true; return checking; }
		const page = active;
		const topics = [...new Set(["notifications", ...(page?.topics || [])])];
		checking = Promise.resolve().then(() => options.check({ topics, known: { ...known } })).then(async (data) => {
			if (!visible()) return;
			failures = 0;
			const changed = Object.keys(data.versions || {}).filter((topic) => known[topic] !== data.versions[topic]);
			if (page === active) invalidate(changed.filter((topic) => topic !== "notifications"));
			if (changed.includes("notifications")) await options.notifications?.();
			Object.assign(known, data.versions || {});
		}).catch(() => { failures++; }).finally(() => {
			checking = null;
			if (checkAgain && visible()) { checkAgain = false; check(); }
			else scheduleCheck();
		});
		return checking;
	}
	return {
		check, refresh, invalidate, resumePage: schedulePage,
		activate(page) {
			if (timer !== null) cancel(timer);
			timer = null;
			active = page;
			schedulePage();
			check();
		},
		wake() {
			if (timer !== null) cancel(timer);
			timer = null;
			if (visible()) { check(); schedulePage(); }
			else if (checkTimer !== null) { cancel(checkTimer); checkTimer = null; }
		},
		event(message) { invalidate(message?.topics || []); if (message?.topics?.includes("notifications")) check(); },
		dispose() { disposed = true; if (timer !== null) cancel(timer); if (checkTimer !== null) cancel(checkTimer); },
	};
}

function createPageReadLoader(call, isCurrent) {
	const states = new WeakMap();
	return function read(page, options) {
		let state = states.get(page);
		if (!state) { state = { running: null, queued: null, desired: null }; states.set(page, state); }
		const key = JSON.stringify([options.method, options.args || {}]);
		const args = JSON.parse(JSON.stringify(options.args || {}));
		if (state.desired?.key === key) return state.desired.promise;
		if (state.queued) { state.queued.resolve(false); state.queued = null; }
		if (state.running?.key === key) {
			state.desired = state.running;
			return state.running.promise;
		}
		const entry = { key, options, args };
		entry.promise = new Promise((resolve, reject) => { entry.resolve = resolve; entry.reject = reject; });
		state.desired = entry;
		function run(current) {
			state.running = current;
			const opts = current.options;
			Promise.resolve().then(() => call({
				method: opts.method, args: current.args, type: opts.type || "POST",
				freeze: !opts.background, freeze_message: opts.freeze_message,
				silent: Boolean(opts.background), timeout: 120000,
			})).then(async (response) => {
				if (current !== state.desired || !isCurrent(page)) return false;
				if (opts.background && page.ps_refresh?.editable?.()) return false;
				if (response.message?._refresh_pending) {
					if (page.ps_refresh) page.ps_refresh.dirty = true;
					return false;
				}
				return (await opts.apply(response)) !== false && !response.message?._refresh_stale;
			}).catch((error) => {
				if (current !== state.desired || !isCurrent(page)) return false;
				throw error;
			}).then((value) => { finish(); current.resolve(value); }, (error) => { finish(); current.reject(error); });
			function finish() {
				state.running = null;
				if (state.desired === current) state.desired = null;
				const next = state.queued;
				state.queued = null;
				if (next && isCurrent(page)) run(next);
				else if (next) { next.resolve(false); state.desired = null; }
			}
		}
		if (state.running) state.queued = entry;
		else run(entry);
		return entry.promise;
	};
}

function bindFreshNotificationView(view, snapshot, requestUpdate) {
	if (!view || view.ps_refresh_bound) return;
	view.ps_refresh_bound = true;
	const render = view.render_notifications_dropdown.bind(view);
	view.render_notifications_dropdown = () => {
		// The native initial cached GET can finish after our fresh POST. Keep its
		// late callback from overwriting the authoritative snapshot or duplicating rows.
		const data = snapshot();
		if (data) {
			view.dropdown_items = data.notification_logs;
			view.settings.enabled = data.enabled ? 1 : 0;
			view.settings.seen = data.seen;
			view.update_count_badge(data.unread_count);
			view.toggle_notification_icon(Boolean(data.seen) || !data.unread_count);
			view.container.empty();
		}
		return render();
	};
	view.update_dropdown = requestUpdate;
}

function setupPageRefresh(frappeRef, win, doc, $) {
	if (!frappeRef.boot || frappeRef.session?.user === "Guest" || win.__ps_page_refresh) return;
	// Retire the previous complete-notification polling loop when both asset
	// versions are present during a rolling upgrade.
	win.__process_simplification_notification_sync?.dispose?.();
	const soundSeen = new Set();
	let notificationCursor = null, notificationInitialized = false, notificationPending = null, latestSnapshot = null;
	const notificationKey = (row) => row ? `${row.creation}|${row.name}` : "";
	async function notifications() {
		if (notificationPending) return notificationPending;
		notificationPending = Promise.resolve().then(() => frappeRef.call({
			method: "process_simplification.page_refresh.get_notifications", type: "POST", silent: true, timeout: 15000,
		})).then(({ message: data }) => {
			if (!data || doc.visibilityState === "hidden") return;
			latestSnapshot = data;
			const view = frappeRef.app?.sidebar?.notifications?.tabs?.notifications;
			if (view) {
				frappeRef.update_user_info(data.user_info || {});
				if (JSON.stringify([Boolean(view.settings.enabled), view.dropdown_items]) !== JSON.stringify([data.enabled, data.notification_logs])) {
					view.settings.enabled = data.enabled ? 1 : 0;
					view.dropdown_items = data.notification_logs;
					view.container.empty();
					view.render_notifications_dropdown();
				}
				view.update_count_badge(data.unread_count);
				view.settings.seen = data.seen;
				view.toggle_notification_icon(Boolean(data.seen) || !data.unread_count);
			}
			const next = data.latest_process_notification;
			if (notificationInitialized && next && notificationKey(next) > notificationKey(notificationCursor) &&
				!next.read && data.enabled && data.play_sound && !soundSeen.has(next.name)) {
				soundSeen.add(next.name);
				win.__process_simplification_notification_sound_controller?.play({ notificationId: next.name });
			}
			if (next && notificationKey(next) > notificationKey(notificationCursor)) notificationCursor = next;
			notificationInitialized = true;
		}).finally(() => { notificationPending = null; });
		return notificationPending;
	}
	const controller = createPageRefreshController({
		visible: () => doc.visibilityState !== "hidden",
		connected: () => Boolean(frappeRef.realtime?.socket?.connected),
		jitter: (interval) => Math.round(Math.random() * interval * 0.05),
		check: async (args) => (await frappeRef.call({
			method: "process_simplification.page_refresh.check_updates", args, type: "POST", silent: true, timeout: 15000,
		})).message,
		notifications,
	});
	win.__ps_page_refresh = controller;
	bindFreshNotificationView(frappeRef.app?.sidebar?.notifications?.tabs?.notifications,
		() => latestSnapshot, () => controller.event({ topics: ["notifications"] }));
	const read = frappeRef.ps_read_page;
	function register(page, options) {
		if (page.ps_refresh) { page.ps_refresh.configure(options); return page.ps_refresh; }
		const banner = $('<div class="ps-refresh-status text-muted" role="status" hidden></div>').prependTo(page.main);
		let inputDirty = false;
		page.main.on("input.ps-refresh change.ps-refresh", "input, select, textarea", (event) => {
			// Frappe also emits change while setting initial/default control values.
			if (options.protectInputs && event.originalEvent?.isTrusted) inputDirty = true;
		});
		const adapter = {
			...options, lastRead: Date.now(), dirty: false, revision: 0,
			editable: () => Boolean(win.cur_dialog?.display || inputDirty || options.editable?.() ||
				(page.main[0]?.contains(doc.activeElement) && /^(INPUT|SELECT|TEXTAREA)$/.test(doc.activeElement.tagName))),
			status(state) {
				if (!doc.contains(banner[0])) banner.prependTo(page.main);
				const texts = {
					changed: options.manual && !options.protectInputs ? "数据有更新，可使用页面的“刷新”重新查询。" :
						"数据有更新，当前填写内容已保留。完成操作后可刷新查看。",
					waiting: "数据有更新，正在自动同步…", loading: "正在更新数据…",
					fresh: "已更新 " + new Date().toLocaleTimeString(), error: "暂时未能更新，将自动重试。",
				};
				banner.prop("hidden", false).text(texts[state]);
				if (state === "changed" && !inputDirty && options.load) {
					$('<button type="button" class="btn btn-xs btn-default ml-2">刷新数据</button>')
						.appendTo(banner).on("click", () => controller.refresh(true));
				}
			},
			resetDirty() { inputDirty = false; },
			configure(next) { Object.assign(options, next); Object.assign(adapter, next); },
		};
		page.ps_refresh = adapter;
		return adapter;
	}
	function routeChanged() {
		const wrapper = frappeRef.container?.page;
		const page = wrapper?.page;
		const route = frappeRef.get_route()?.[0];
		const config = {
			"my-production-reporting": { topics: ["tasks"], interval: 3000, load: page?.worker_reporting?.load },
			"active-production-work": { topics: ["tasks"], interval: 3000, load: page?.worker_reporting?.load },
			"production-report-review": { topics: ["review"], interval: 5000, load: page?.report_review?.load },
			"production-exception-review": { topics: ["review", "warehouse"], interval: 5000, load: page?.production_exception_review?.load },
			"order-workbench": { topics: ["orders"], interval: 30000, load: page?.fulfillment_overview?.loadOverview },
			"production-workbench": { topics: ["production"], interval: 30000, load: page?.production_workbench?.loadOverview },
			"warehouse-workbench": { topics: ["warehouse"], interval: 5000, load: page?.warehouseWorkbench?.backgroundRefresh },
			"shortage-purchase-planning": { topics: ["purchase"], interval: 30000, load: page?.shortage_refresh, protectInputs: true },
			"purchase-supplier-allocation": { topics: ["purchase"], interval: 5000, load: (opts) => page.purchase_refresh?.(opts),
				protectInputs: Boolean(frappeRef.route_options?.material_request || win.location.search.includes("material_request=")),
				manual: Boolean(frappeRef.route_options?.material_request || win.location.search.includes("material_request=")) },
			"quick-sales-order": { topics: ["orders", "purchase"], interval: 30000, manual: true, protectInputs: true },
			"executive-dashboard": { topics: ["dashboard"], interval: 120000, load: (opts) => wrapper.executive_dashboard.load(opts) },
			"production-report-history": { topics: ["tasks", "wages"], interval: 120000, manual: true },
			"purchase-receipt-notice": { topics: ["purchase"], interval: 120000, manual: true },
			"query-report": ["Stock Balance", "Stock Ledger"].includes(frappeRef.get_route()?.[1]) ?
				{ topics: ["warehouse"], interval: 120000, manual: true } : null,
		}[route];
		controller.activate(page && config && (config.load || config.manual) ? register(page, config) : null);
	}
	frappeRef.realtime?.on?.(PS_REFRESH_EVENT, (data) => controller.event(data));
	frappeRef.realtime?.on?.("process_simplification_notification", (data) => {
		if (doc.visibilityState !== "hidden" && data?.notification_log) {
			soundSeen.add(data.notification_log);
			if (soundSeen.size > 100) soundSeen.delete(soundSeen.values().next().value);
		}
	});
	for (const event of ["connect", "disconnect"]) frappeRef.realtime?.socket?.on(event, () => controller.wake());
	for (const event of ["focus", "online"]) win.addEventListener(event, () => controller.wake());
	doc.addEventListener("visibilitychange", () => controller.wake());
	$(doc).on("page-change.ps-page-refresh", routeChanged);
	$(doc).on("hidden.bs.modal.ps-page-refresh", () => controller.resumePage());
	$(doc).on("focusout.ps-page-refresh", () => win.setTimeout(() => controller.resumePage(), 0));
	win.process_simplification = win.process_simplification || {};
	win.process_simplification.page_refresh = { register, read, notifications, refresh: () => controller.refresh(true) };
	frappeRef.ps_read_page = read;
	routeChanged();
}

if (typeof module !== "undefined" && module.exports) {
	module.exports = { createPageRefreshController, createPageReadLoader, bindFreshNotificationView, PS_REFRESH_HEALTHY_MS, PS_REFRESH_OFFLINE_MS };
}
if (typeof frappe !== "undefined" && typeof window !== "undefined") {
	const read = createPageReadLoader((args) => frappe.call(args),
		(page) => frappe.container?.page?.page === page);
	frappe.ps_read_page = (page, options) => {
		const revision = page.ps_refresh?.revision;
		return read(page, { ...options, async apply(response) {
		const root = page.main?.[0];
		const scrollY = window.scrollY;
		const controls = Array.from(root?.querySelectorAll("input, select, textarea") || []).map((node) => ({
			key: node.id || node.getAttribute("data-filter") || node.getAttribute("aria-label"), value: node.value,
		})).filter((row) => row.key);
		const detailsKey = (node) => node.id || JSON.stringify(node.dataset) + (node.querySelector("summary")?.textContent || "");
		const expanded = new Set(Array.from(root?.querySelectorAll("details[open]") || []).map(detailsKey));
		const applied = await options.apply(response);
		if (applied !== false && !options.background && page.ps_refresh) {
			page.ps_refresh.resetDirty();
			page.ps_refresh.lastRead = Date.now();
			if (response.message?._refresh_stale) page.ps_refresh.dirty = true;
			else if (revision === page.ps_refresh.revision) page.ps_refresh.dirty = false;
		}
		if (options.background) {
			for (const node of root?.querySelectorAll("input, select, textarea") || []) {
				const key = node.id || node.getAttribute("data-filter") || node.getAttribute("aria-label");
				const saved = controls.find((row) => row.key === key);
				if (saved && (node.tagName !== "SELECT" || Array.from(node.options).some((option) => option.value === saved.value))) node.value = saved.value;
			}
			for (const node of root?.querySelectorAll("details") || []) node.open = expanded.has(detailsKey(node));
			window.scrollTo({ top: scrollY, behavior: "instant" });
		}
		return applied;
	} }).finally(() => window.__ps_page_refresh?.resumePage());
	};
	const start = () => setupPageRefresh(frappe, window, document, window.jQuery);
	if (frappe.app?.sidebar) start();
	else window.jQuery(document).one("app_ready.ps-page-refresh", start);
}
