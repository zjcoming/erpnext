const test = require("node:test");
const assert = require("node:assert/strict");
const { PROCESS_NOTIFICATION_EVENT, PROCESS_NOTIFICATION_SOUND_STORAGE_KEY,
	createProcessNotificationSoundController, setupProcessNotificationSound,
	enableProcessNotificationSound } = require("../../public/js/notification_sound.js");

function memoryStorage() {
	const values = new Map();
	return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)) };
}
function fixture(extra = {}) {
	const sounds = [], alerts = [], handlers = {};
	const audio = { paused: true, play: () => { sounds.push("process-notification"); return Promise.resolve(); } };
	const elements = new Map();
	const documentRef = { visibilityState: "visible", getElementById: (id) => elements.get(id),
		createElement: () => audio, body: { appendChild: (element) => elements.set(element.id, element) } };
	const windowRef = { document: documentRef };
	const frappeRef = { boot: { user: { mute_sounds: 0 } },
		realtime: { socket: {}, on: (event, handler) => { handlers[event] = handler; } },
		show_alert: (alert) => alerts.push(alert) };
	const options = { windowRef, documentRef, frappeRef, storage: memoryStorage(), now: () => 10_000, ...extra };
	return { options, audio, sounds, handlers, alerts, documentRef, windowRef, frappeRef };
}
test("respects account mute and keeps process audio separate from ordinary alerts", async () => {
	const f = fixture(), controller = createProcessNotificationSoundController(f.options);
	const ordinaryAudio = { src: "/assets/frappe/sounds/alert.mp3", volume: 0.2 };
	f.documentRef.body.appendChild({ id: "sound-alert", ...ordinaryAudio });
	f.frappeRef.boot.user.mute_sounds = 1;
	assert.equal(await controller.play(), false);
	f.frappeRef.boot.user.mute_sounds = 0;
	assert.equal(await controller.play(), true);
	assert.deepEqual(f.sounds, ["process-notification"]);
	assert.equal(f.audio.volume, 0.8);
	assert.equal(f.audio.src, "/assets/process_simplification/sounds/notification-chime-v1.wav");
	assert.deepEqual(f.documentRef.getElementById("sound-alert"), { id: "sound-alert", ...ordinaryAudio });
});
test("coalesces bursts and the same user's foreground tabs", async () => {
	let at = 20_000;
	const f = fixture({ now: () => at });
	const a = createProcessNotificationSoundController(f.options), b = createProcessNotificationSoundController(f.options);
	assert.equal(await a.play(), true);
	at += 200;
	assert.equal(await a.play(), false);
	assert.equal(await b.play(), false);
	at += 1800;
	assert.equal(await b.play(), true);
	assert.deepEqual(f.sounds, ["process-notification", "process-notification"]);
});
test("rejected autoplay releases cooldown and permits a gesture retry", async () => {
	const blocked = [], f = fixture({ onBlocked: (error) => blocked.push(error.name) });
	let gesture = false;
	f.audio.play = () => gesture ? Promise.resolve() : Promise.reject(Object.assign(new Error("user gesture required"), { name: "NotAllowedError" }));
	const controller = createProcessNotificationSoundController(f.options);
	assert.equal(await controller.play(), false);
	assert.deepEqual(blocked, ["NotAllowedError"]);
	assert.notEqual(f.options.storage.getItem(PROCESS_NOTIFICATION_SOUND_STORAGE_KEY), "10000");
	gesture = true;
	assert.equal(await controller.play({ preview: true }), true);
});
test("menu preview plays in the gesture stack and later realtime reuses that audio", async () => {
	let at = 30_000, gesture = false, unlocked = false, calls = 0;
	const f = fixture({ now: () => at });
	f.audio.play = () => {
		calls++;
		if (gesture) unlocked = true;
		return unlocked ? Promise.resolve() : Promise.reject(new Error("blocked"));
	};
	setupProcessNotificationSound(f.options);
	gesture = true;
	const preview = enableProcessNotificationSound(f.options);
	assert.equal(calls, 1, "play must run before leaving the menu gesture");
	gesture = false;
	assert.equal(await preview, true);
	f.documentRef.createElement = () => { throw new Error("must reuse the preview-unlocked element"); };
	at += 2000;
	f.handlers[PROCESS_NOTIFICATION_EVENT]({ play_sound: true });
	await Promise.resolve();
	assert.equal(calls, 2);
	assert.equal(f.alerts.at(-1).indicator, "green");
});
test("failed preview never claims a new notification or successful enablement", async () => {
	const blocked = [], f = fixture({ onBlocked: () => blocked.push(true) });
	f.audio.play = () => Promise.reject(new Error("NotAllowedError"));
	setupProcessNotificationSound(f.options);
	assert.equal(await enableProcessNotificationSound(f.options), false);
	assert.equal(f.alerts.at(-1).indicator, "orange");
	assert.deepEqual(blocked, []);
	f.frappeRef.boot.user.mute_sounds = 1;
	assert.equal(await enableProcessNotificationSound(f.options), false);
	assert.match(f.alerts.at(-1).message, /账号已静音/);
});
test("hidden tabs and pending attempts cannot consume another foreground sound", async () => {
	const f = fixture(), controller = createProcessNotificationSoundController(f.options);
	f.documentRef.visibilityState = "hidden";
	assert.equal(await controller.play(), false);
	assert.equal(f.options.storage.getItem(PROCESS_NOTIFICATION_SOUND_STORAGE_KEY), null);
	f.documentRef.visibilityState = "visible";
	let resolve;
	f.audio.play = () => new Promise((done) => { resolve = done; });
	const pending = controller.play();
	assert.equal(await controller.play(), false);
	resolve();
	assert.equal(await pending, true);
});
test("listener registers once and ignores disabled sound payloads and other apps", async () => {
	const f = fixture();
	assert.equal(setupProcessNotificationSound(f.options), true);
	assert.equal(setupProcessNotificationSound(f.options), false);
	assert.deepEqual(Object.keys(f.handlers), [PROCESS_NOTIFICATION_EVENT]);
	f.handlers[PROCESS_NOTIFICATION_EVENT]({ play_sound: false });
	f.handlers[PROCESS_NOTIFICATION_EVENT]({ play_sound: 0 });
	assert.deepEqual(f.sounds, []);
	f.handlers[PROCESS_NOTIFICATION_EVENT]({ play_sound: true });
	await Promise.resolve();
	assert.deepEqual(f.sounds, ["process-notification"]);
});
test("deferred app_ready preserves menu enablement and listener", async () => {
	const f = fixture();
	f.frappeRef.realtime.socket = null;
	let ready;
	f.options.jQueryRef = () => ({ one: (_event, callback) => { ready = callback; } });
	assert.equal(setupProcessNotificationSound(f.options), true);
	assert.equal(setupProcessNotificationSound(f.options), false);
	assert.deepEqual(f.handlers, {});
	f.frappeRef.realtime.socket = {};
	assert.equal(ready(), true);
	assert.equal(await enableProcessNotificationSound(f.options), true);
});
test("missing audio and storage exceptions fail safely", async () => {
	const f = fixture({ storage: { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } } });
	assert.equal(await createProcessNotificationSoundController(f.options).play(), true);
	f.documentRef.body = null;
	assert.equal(await createProcessNotificationSoundController(f.options).play(), false);
});

test("an old page's failed-play timestamp cannot silence the upgraded page", async () => {
	const f = fixture();
	f.options.storage.setItem("process_simplification:last_notification_sound_at", "10000");
	assert.equal(await createProcessNotificationSoundController(f.options).play(), true);
	assert.deepEqual(f.sounds, ["process-notification"]);
});
