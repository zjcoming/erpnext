const test = require("node:test");
const assert = require("node:assert/strict");
const { createNotificationSyncController, renderNotificationSnapshot,
	PROCESS_NOTIFICATION_POLL_MS, PROCESS_NOTIFICATION_CONNECTED_POLL_MS } = require("../../public/js/notification_sync.js");

function target(extra = {}) {
	const handlers = {};
	return { handlers, addEventListener: (event, fn) => { handlers[event] = fn; },
		removeEventListener: (event) => { delete handlers[event]; }, ...extra };
}
function snapshot(id = "OLD", creation = "2026-09-09 00:00:00", extra = {}) {
	return { enabled: true, seen: 0, unread_count: 1, notification_logs: [], play_sound: true,
		latest_process_notification: id ? { name: id, creation, read: 0 } : null, ...extra };
}
function fixture() {
	const documentRef = target({ visibilityState: "visible" }), windowRef = target();
	const socket = { connected: false, handlers: {}, on(event, fn) { this.handlers[event] = fn; }, off(event) { delete this.handlers[event]; } };
	const timers = new Map(), renders = [], plays = [];
	let next = snapshot(), calls = 0, id = 0;
	const options = { documentRef, windowRef, socket,
		setTimeout: (fn, ms) => { timers.set(++id, { fn, ms }); return id; }, clearTimeout: (id) => timers.delete(id),
		fetchSnapshot: async () => { calls++; return next; },
		render: (data) => renders.push(data), play: (data) => plays.push(data) };
	return { options, documentRef, windowRef, socket, timers, renders, plays,
		set: (data) => { next = data; }, calls: () => calls };
}
test("a disconnected phone automatically refreshes and sounds only new notifications", async () => {
	const f = fixture(), sync = createNotificationSyncController(f.options);
	await sync.start();
	assert.equal(f.renders.length, 1);
	assert.deepEqual(f.plays, [], "opening a page must not sound old unread notifications");
	assert.equal([...f.timers.values()][0].ms, PROCESS_NOTIFICATION_POLL_MS);
	f.set(snapshot("NEW", "2026-09-09 00:01:00"));
	await sync.sync();
	await sync.sync();
	assert.deepEqual(f.plays, [{ notificationId: "NEW" }]);
	assert.equal(f.renders.length, 3);
	sync.dispose();
});
test("an initially empty inbox still alerts for its first arriving notification", async () => {
	const f = fixture(), sync = createNotificationSyncController(f.options);
	f.set(snapshot(null));
	await sync.start();
	f.set(snapshot("FIRST"));
	await sync.sync();
	assert.deepEqual(f.plays, [{ notificationId: "FIRST" }]);
});
test("background suspends polling; foreground and network recovery catch up", async () => {
	const f = fixture(), sync = createNotificationSyncController(f.options);
	await sync.start();
	f.documentRef.visibilityState = "hidden";
	f.documentRef.handlers.visibilitychange();
	assert.equal(f.timers.size, 0);
	f.set(snapshot("WHILE-HIDDEN", "2026-09-09 00:02:00"));
	await sync.sync();
	assert.equal(f.calls(), 1);
	f.documentRef.visibilityState = "visible";
	f.documentRef.handlers.visibilitychange();
	await sync.sync();
	assert.deepEqual(f.plays, [{ notificationId: "WHILE-HIDDEN" }]);
	f.windowRef.handlers.online();
	await sync.sync();
	assert.equal(f.calls(), 3);
});
test("healthy realtime uses a slower safety check and events refresh immediately", async () => {
	const f = fixture();
	f.socket.connected = true;
	const sync = createNotificationSyncController(f.options);
	await sync.start();
	assert.equal([...f.timers.values()][0].ms, PROCESS_NOTIFICATION_CONNECTED_POLL_MS);
	f.set(snapshot("EVENT", "2026-09-09 00:02:00"));
	f.socket.handlers.notification();
	await sync.sync();
	assert.deepEqual(f.plays, [{ notificationId: "EVENT" }]);
	f.socket.connected = false;
	f.socket.handlers.disconnect();
	await sync.sync();
	assert.equal([...f.timers.values()][0].ms, PROCESS_NOTIFICATION_POLL_MS);
});
test("failures retain the cursor, and overlapping wake-ups share one request", async () => {
	const f = fixture(), sync = createNotificationSyncController(f.options);
	await sync.start();
	let reject;
	f.options.fetchSnapshot = () => new Promise((_, no) => { reject = no; });
	const a = sync.sync();
	await Promise.resolve();
	const b = sync.sync();
	reject(new Error("offline"));
	await Promise.all([a, b]);
	assert.equal(f.renders.length, 1);
	assert.equal(f.timers.size, 1);
	f.options.fetchSnapshot = async () => snapshot("RECOVERED", "2026-09-09 00:03:00");
	await sync.sync();
	assert.deepEqual(f.plays, [{ notificationId: "RECOVERED" }]);
});
test("read messages, muted payloads and older snapshots never produce a sound", async () => {
	const f = fixture(), sync = createNotificationSyncController(f.options);
	await sync.start();
	f.set(snapshot("MUTED", "2026-09-09 00:03:00", { play_sound: false }));
	await sync.sync();
	f.set(snapshot("READ", "2026-09-09 00:04:00"));
	f.options.fetchSnapshot = async () => ({ ...snapshot(), latest_process_notification: { name: "READ", creation: "2026-09-09 00:04:00", read: 1 } });
	await sync.sync();
	f.options.fetchSnapshot = async () => snapshot("OLDER", "2026-09-09 00:01:00");
	await sync.sync();
	assert.deepEqual(f.plays, []);
	sync.dispose();
	assert.equal(f.timers.size, 0);
	assert.equal(Object.keys(f.socket.handlers).length, 0);
});
test("fresh snapshots repair the native cached dropdown without marking notifications read", () => {
	let renders = 0, count, seen;
	const view = { settings: { enabled: 1 }, dropdown_items: [{ name: "CACHED" }],
		container: { empty() {} }, render_notifications_dropdown() { renders++; },
		update_count_badge(value) { count = value; }, toggle_notification_icon(value) { seen = value; } };
	const frappe = { app: { sidebar: { notifications: { tabs: { notifications: view } } } }, update_user_info() {} };
	const data = snapshot("NEW", "2026-09-09", { notification_logs: [{ name: "NEW", read: 0 }], unread_count: 7, seen: 1 });
	renderNotificationSnapshot(frappe, data);
	renderNotificationSnapshot(frappe, data);
	assert.equal(renders, 1);
	assert.equal(count, 7);
	assert.equal(seen, true);
	view.dropdown_items = [{ name: "CACHED" }];
	renderNotificationSnapshot(frappe, data);
	assert.equal(renders, 2);
	assert.equal(view.dropdown_items[0].read, 0);
});
