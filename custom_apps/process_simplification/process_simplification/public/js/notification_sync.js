const PROCESS_NOTIFICATION_SYNC_FLAG = "__process_simplification_notification_sync";
const PROCESS_NOTIFICATION_POLL_MS = 10000;
const PROCESS_NOTIFICATION_CONNECTED_POLL_MS = 60000;

function createNotificationSyncController(options) {
	const doc = options.documentRef;
	const win = options.windowRef;
	const socket = options.socket;
	const later = options.setTimeout || win.setTimeout.bind(win);
	const cancel = options.clearTimeout || win.clearTimeout.bind(win);
	let started = false, initialized = false, disposed = false;
	let timer = null, pending = null, latest = null;
	const visible = () => doc.visibilityState !== "hidden";
	const cursor = (row) => row ? `${row.creation}|${row.name}` : "";
	const schedule = () => {
		if (timer !== null) cancel(timer);
		timer = null;
		if (!disposed && visible()) {
			timer = later(sync, socket?.connected
				? PROCESS_NOTIFICATION_CONNECTED_POLL_MS : PROCESS_NOTIFICATION_POLL_MS);
		}
	};
	async function sync() {
		if (disposed || !visible()) return;
		if (pending) return pending;
		pending = (async () => {
			await Promise.resolve();
			try {
				const snapshot = await options.fetchSnapshot();
				if (disposed || !visible()) return;
				options.render(snapshot);
				const next = snapshot.latest_process_notification;
				const isNew = initialized && next && cursor(next) > cursor(latest);
				if (next && cursor(next) > cursor(latest)) latest = next;
				initialized = true;
				if (isNew && !next.read && snapshot.enabled && snapshot.play_sound) {
					options.play({ notificationId: next.name });
				}
			} catch (error) {
				// Retain the cursor on failures; the next successful request catches up.
				options.onError?.(error);
			} finally {
				pending = null;
				schedule();
			}
		})();
		return pending;
	}
	const wake = () => {
		if (timer !== null) cancel(timer);
		timer = null;
		if (visible()) sync();
	};
	return {
		sync,
		start() {
			if (started || disposed) return;
			started = true;
			doc.addEventListener("visibilitychange", wake);
			win.addEventListener("online", wake);
			win.addEventListener("focus", wake);
			socket?.on("connect", wake);
			socket?.on("disconnect", wake);
			socket?.on("notification", wake);
			return sync();
		},
		dispose() {
			disposed = true;
			if (timer !== null) cancel(timer);
			doc.removeEventListener("visibilitychange", wake);
			win.removeEventListener("online", wake);
			win.removeEventListener("focus", wake);
			for (const event of ["connect", "disconnect", "notification"]) socket?.off(event, wake);
		},
	};
}

function renderNotificationSnapshot(frappeRef, snapshot) {
	const view = frappeRef.app?.sidebar?.notifications?.tabs?.notifications;
	if (!view) return;
	frappeRef.update_user_info(snapshot.user_info || {});
	const logs = snapshot.notification_logs || [];
	// Re-render only when content changes, so reading a long notification stays stable.
	const signature = JSON.stringify([snapshot.enabled, logs]);
	if (JSON.stringify([Boolean(view.settings.enabled), view.dropdown_items]) !== signature) {
		view.settings.enabled = snapshot.enabled ? 1 : 0;
		view.dropdown_items = logs;
		view.container.empty();
		view.render_notifications_dropdown();
	}
	view.update_count_badge(snapshot.unread_count || 0);
	view.settings.seen = snapshot.seen;
	view.toggle_notification_icon(Boolean(snapshot.seen) || !snapshot.unread_count);
}

function setupNotificationSync({ frappeRef, windowRef, documentRef, jQueryRef }) {
	if (!windowRef || windowRef[PROCESS_NOTIFICATION_SYNC_FLAG]) return false;
	if (!frappeRef?.boot || frappeRef.session?.user === "Guest") return false;
	windowRef[PROCESS_NOTIFICATION_SYNC_FLAG] = true;
	const start = () => {
		const controller = createNotificationSyncController({
			windowRef, documentRef, socket: frappeRef.realtime?.socket,
			fetchSnapshot: async () => {
				const response = await frappeRef.call({
					method: "process_simplification.api.notification_sync.get_notification_snapshot",
					type: "POST", silent: true, timeout: 15000,
				});
				if (!response?.message) throw new Error("Notification snapshot unavailable");
				return response.message;
			},
			render: (snapshot) => renderNotificationSnapshot(frappeRef, snapshot),
			play: (options) => windowRef.__process_simplification_notification_sound_controller?.play(options),
		});
		windowRef[PROCESS_NOTIFICATION_SYNC_FLAG] = controller;
		controller.start();
	};
	if (frappeRef.app?.sidebar) start();
	else jQueryRef(documentRef).one("app_ready.process_simplification_notification_sync", start);
	return true;
}

if (typeof module !== "undefined" && module.exports) {
	module.exports = { createNotificationSyncController, renderNotificationSnapshot, setupNotificationSync,
		PROCESS_NOTIFICATION_POLL_MS, PROCESS_NOTIFICATION_CONNECTED_POLL_MS };
}
if (typeof window !== "undefined" && typeof frappe !== "undefined") {
	setupNotificationSync({ frappeRef: frappe, windowRef: window, documentRef: document, jQueryRef: window.jQuery });
}
