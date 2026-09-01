const PROCESS_NOTIFICATION_EVENT = "process_simplification_notification";
const PROCESS_NOTIFICATION_SOUND = "alert";
const PROCESS_NOTIFICATION_SOUND_COOLDOWN_MS = 1500;
const PROCESS_NOTIFICATION_SOUND_STORAGE_KEY =
	"process_simplification:last_notification_sound_at";
const PROCESS_NOTIFICATION_SOUND_SETUP_FLAG =
	"__process_simplification_notification_sound_initialized";
const PROCESS_NOTIFICATION_SOUND_PENDING_FLAG =
	"__process_simplification_notification_sound_pending";

function readSharedSoundTimestamp(storage) {
	try {
		const value = Number(storage?.getItem(PROCESS_NOTIFICATION_SOUND_STORAGE_KEY));
		return Number.isFinite(value) && value > 0 ? value : Number.NEGATIVE_INFINITY;
	} catch (error) {
		return Number.NEGATIVE_INFINITY;
	}
}

function writeSharedSoundTimestamp(storage, value) {
	try {
		storage?.setItem(PROCESS_NOTIFICATION_SOUND_STORAGE_KEY, String(value));
	} catch (error) {
		// Private browsing or a browser policy may make localStorage unavailable.
	}
}

function createProcessNotificationSoundController(options = {}) {
	const frappeRef = options.frappeRef;
	const storage = options.storage;
	const now = options.now || Date.now;
	const cooldownMs = options.cooldownMs ?? PROCESS_NOTIFICATION_SOUND_COOLDOWN_MS;
	let lastPlayedAt = Number.NEGATIVE_INFINITY;

	return {
		play() {
			if (frappeRef?.boot?.user?.mute_sounds) return false;
			if (typeof frappeRef?.utils?.play_sound !== "function") return false;

			const timestamp = now();
			const latestTimestamp = Math.max(
				lastPlayedAt,
				readSharedSoundTimestamp(storage)
			);
			if (timestamp - latestTimestamp < cooldownMs) return false;

			lastPlayedAt = timestamp;
			writeSharedSoundTimestamp(storage, timestamp);
			frappeRef.utils.play_sound(PROCESS_NOTIFICATION_SOUND);
			return true;
		},
	};
}

function setupProcessNotificationSound(options = {}) {
	const frappeRef = options.frappeRef;
	const windowRef = options.windowRef;
	if (!windowRef) return false;
	if (windowRef[PROCESS_NOTIFICATION_SOUND_SETUP_FLAG]) return false;
	if (windowRef[PROCESS_NOTIFICATION_SOUND_PENDING_FLAG]) return false;

	const register = () => {
		if (windowRef[PROCESS_NOTIFICATION_SOUND_SETUP_FLAG]) return false;
		if (!frappeRef?.realtime?.on || !frappeRef.realtime.socket) return false;

		windowRef[PROCESS_NOTIFICATION_SOUND_SETUP_FLAG] = true;
		windowRef[PROCESS_NOTIFICATION_SOUND_PENDING_FLAG] = false;
		const controller = createProcessNotificationSoundController(options);
		frappeRef.realtime.on(PROCESS_NOTIFICATION_EVENT, (message) => {
			if (message?.play_sound === false || message?.play_sound === 0) return;
			controller.play();
		});
		return true;
	};

	if (register()) return true;

	const jQueryRef = options.jQueryRef || windowRef.jQuery;
	const documentRef = options.documentRef || windowRef.document;
	if (typeof jQueryRef !== "function" || !documentRef) return false;

	windowRef[PROCESS_NOTIFICATION_SOUND_PENDING_FLAG] = true;
	jQueryRef(documentRef).one(
		"app_ready.process_simplification_notification_sound",
		register
	);
	return true;
}

const processNotificationSoundApi = {
	PROCESS_NOTIFICATION_EVENT,
	PROCESS_NOTIFICATION_SOUND,
	PROCESS_NOTIFICATION_SOUND_COOLDOWN_MS,
	PROCESS_NOTIFICATION_SOUND_STORAGE_KEY,
	PROCESS_NOTIFICATION_SOUND_PENDING_FLAG,
	createProcessNotificationSoundController,
	setupProcessNotificationSound,
};

if (typeof module !== "undefined" && module.exports) {
	module.exports = processNotificationSoundApi;
}

if (typeof window !== "undefined" && typeof frappe !== "undefined") {
	let storage = null;
	try {
		storage = window.localStorage;
	} catch (error) {
		// The in-tab cooldown still works when localStorage is unavailable.
	}
	setupProcessNotificationSound({ frappeRef: frappe, windowRef: window, storage });
	window.process_simplification = window.process_simplification || {};
	window.process_simplification.notification_sound = processNotificationSoundApi;
}
