const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const appDirectory = path.resolve(__dirname, "../..");
const {
	PROCESS_NOTIFICATION_EVENT,
	PROCESS_NOTIFICATION_SOUND_STORAGE_KEY,
	createProcessNotificationSoundController,
	setupProcessNotificationSound,
} = require(path.join(appDirectory, "public/js/notification_sound.js"));

function memoryStorage() {
	const values = new Map();
	return {
		getItem: (key) => values.get(key) ?? null,
		setItem: (key, value) => values.set(key, value),
	};
}

test("process notification sound respects mute and uses the Frappe alert sound", () => {
	const sounds = [];
	const frappeRef = {
		boot: { user: { mute_sounds: 1 } },
		utils: { play_sound: (name) => sounds.push(name) },
	};
	const controller = createProcessNotificationSoundController({
		frappeRef,
		storage: memoryStorage(),
		now: () => 10_000,
	});

	assert.equal(controller.play(), false);
	frappeRef.boot.user.mute_sounds = 0;
	assert.equal(controller.play(), true);
	assert.deepEqual(sounds, ["alert"]);
});

test("process notification sound collapses a burst in one tab", () => {
	let timestamp = 20_000;
	const sounds = [];
	const controller = createProcessNotificationSoundController({
		frappeRef: {
			boot: { user: { mute_sounds: 0 } },
			utils: { play_sound: (name) => sounds.push(name) },
		},
		storage: memoryStorage(),
		now: () => timestamp,
	});

	assert.equal(controller.play(), true);
	timestamp += 200;
	assert.equal(controller.play(), false);
	timestamp += 1300;
	assert.equal(controller.play(), true);
	assert.deepEqual(sounds, ["alert", "alert"]);
});

test("process notification sound shares the cooldown across browser tabs", () => {
	const storage = memoryStorage();
	const sounds = [];
	const options = {
		frappeRef: {
			boot: { user: { mute_sounds: 0 } },
			utils: { play_sound: (name) => sounds.push(name) },
		},
		storage,
		now: () => 30_000,
	};
	const firstTab = createProcessNotificationSoundController(options);
	const secondTab = createProcessNotificationSoundController(options);

	assert.equal(firstTab.play(), true);
	assert.equal(secondTab.play(), false);
	assert.equal(storage.getItem(PROCESS_NOTIFICATION_SOUND_STORAGE_KEY), "30000");
	assert.deepEqual(sounds, ["alert"]);
});

test("sound setup listens only once to the dedicated process notification event", () => {
	const handlers = {};
	const sounds = [];
	const windowRef = {};
	const frappeRef = {
		boot: { user: { mute_sounds: 0 } },
		utils: { play_sound: (name) => sounds.push(name) },
		realtime: {
			socket: {},
			on: (event, handler) => (handlers[event] = handler),
		},
	};

	assert.equal(
		setupProcessNotificationSound({
			frappeRef,
			windowRef,
			storage: memoryStorage(),
			now: () => 40_000,
		}),
		true
	);
	assert.equal(setupProcessNotificationSound({ frappeRef, windowRef }), false);
	assert.equal(typeof handlers[PROCESS_NOTIFICATION_EVENT], "function");

	handlers[PROCESS_NOTIFICATION_EVENT]();
	assert.deepEqual(sounds, ["alert"]);
	handlers[PROCESS_NOTIFICATION_EVENT]({ play_sound: false });
	assert.deepEqual(sounds, ["alert"]);
});

test("sound setup waits for app_ready when Frappe has not created its socket yet", () => {
	const handlers = {};
	const sounds = [];
	const windowRef = {};
	const documentRef = {};
	let appReadyHandler = null;
	const frappeRef = {
		boot: { user: { mute_sounds: 0 } },
		utils: { play_sound: (name) => sounds.push(name) },
		realtime: {
			socket: null,
			on: (event, handler) => (handlers[event] = handler),
		},
	};
	const jQueryRef = (target) => {
		assert.equal(target, documentRef);
		return {
			one(event, handler) {
				assert.equal(
					event,
					"app_ready.process_simplification_notification_sound"
				);
				appReadyHandler = handler;
			},
		};
	};
	const options = {
		frappeRef,
		windowRef,
		documentRef,
		jQueryRef,
		storage: memoryStorage(),
		now: () => 50_000,
	};

	assert.equal(setupProcessNotificationSound(options), true);
	assert.equal(setupProcessNotificationSound(options), false);
	assert.equal(typeof appReadyHandler, "function");
	assert.equal(handlers[PROCESS_NOTIFICATION_EVENT], undefined);

	frappeRef.realtime.socket = {};
	assert.equal(appReadyHandler(), true);
	handlers[PROCESS_NOTIFICATION_EVENT]({ play_sound: true });
	assert.deepEqual(sounds, ["alert"]);
});
