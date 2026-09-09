const PROCESS_NOTIFICATION_EVENT = "process_simplification_notification";
const PROCESS_NOTIFICATION_SOUND = "process-notification";
const PROCESS_NOTIFICATION_SOUND_URL =
	"/assets/process_simplification/sounds/notification-chime-v1.wav";
const PROCESS_NOTIFICATION_SOUND_COOLDOWN_MS = 2000;
const PROCESS_NOTIFICATION_SOUND_STORAGE_KEY =
	"process_simplification:last_notification_sound_at:v2";
const PROCESS_NOTIFICATION_SOUND_SETUP_FLAG =
	"__process_simplification_notification_sound_initialized";
const PROCESS_NOTIFICATION_SOUND_PENDING_FLAG =
	"__process_simplification_notification_sound_pending";
const PROCESS_NOTIFICATION_SOUND_CONTROLLER =
	"__process_simplification_notification_sound_controller";

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

function getProcessNotificationAudio(documentRef) {
	if (!documentRef?.body) return null;
	const id = "sound-process-notification";
	let audio = documentRef.getElementById(id);
	if (!audio) {
		audio = documentRef.createElement("audio");
		audio.id = id;
		audio.src = PROCESS_NOTIFICATION_SOUND_URL;
		audio.preload = "auto";
		audio.hidden = true;
		documentRef.body.appendChild(audio);
	}
	return audio;
}

function createProcessNotificationSoundController(options = {}) {
	const frappeRef = options.frappeRef;
	const storage = options.storage;
	const now = options.now || Date.now;
	const cooldownMs = options.cooldownMs ?? PROCESS_NOTIFICATION_SOUND_COOLDOWN_MS;
	const documentRef = options.documentRef || options.windowRef?.document;
	const getAudio = options.getAudio || (() => getProcessNotificationAudio(documentRef));
	let lastPlayedAt = Number.NEGATIVE_INFINITY;
	let inFlight = false;
	const handledNotifications = new Set();

	return {
		async play({ preview = false, notificationId = null } = {}) {
			if (frappeRef?.boot?.user?.mute_sounds) return false;
			const audio = getAudio();
			if (!audio) return false;
			// A hidden tab must not consume the foreground tab's sound cooldown.
			if (documentRef?.visibilityState === "hidden") return false;
			// Realtime and automatic catch-up can deliver the same notification.
			if (notificationId) {
				if (handledNotifications.has(notificationId)) return false;
				handledNotifications.add(notificationId);
				if (handledNotifications.size > 100) handledNotifications.delete(handledNotifications.values().next().value);
			}
			if (inFlight) return false;

			const timestamp = now();
			const latestTimestamp = Math.max(
				lastPlayedAt,
				readSharedSoundTimestamp(storage)
			);
			if (!preview && timestamp - latestTimestamp < cooldownMs) return false;

			const previousTimestamp = lastPlayedAt;
			const previousSharedTimestamp = readSharedSoundTimestamp(storage);
			lastPlayedAt = timestamp;
			writeSharedSoundTimestamp(storage, timestamp);
			inFlight = true;
			try {
				// iPhone uses the device's media volume; the chime itself carries
				// the repeated notes. Desktop playback should also be easy to hear.
				audio.volume = 0.8;
				if (!audio.paused) audio.currentTime = 0;
				// Keep play() in the menu click's call stack. Awaiting anything first
				// would lose Safari's user gesture. Preview and events reuse one element.
				await audio.play();
				return true;
			} catch (error) {
				lastPlayedAt = previousTimestamp;
				if (readSharedSoundTimestamp(storage) === timestamp) {
					writeSharedSoundTimestamp(storage, previousSharedTimestamp);
				}
				if (!preview) options.onBlocked?.(error);
				return false;
			} finally {
				inFlight = false;
			}
		},
	};
}

async function enableProcessNotificationSound(options = {}) {
	const windowRef = options.windowRef;
	const frappeRef = options.frappeRef;
	const translate = options.translate || ((message) => message);
	const controller = windowRef?.[PROCESS_NOTIFICATION_SOUND_CONTROLLER];
	if (frappeRef?.boot?.user?.mute_sounds) {
		frappeRef.show_alert?.({ message: translate("当前账号已静音，请先在用户设置中关闭静音。"), indicator: "orange" });
		return false;
	}
	const played = await controller?.play({ preview: true });
	frappeRef?.show_alert?.({
		message: translate(played
			? "提示音已启用。本页保持前台可接收声音提醒；刷新页面后请重新启用。"
			: "提示音未能播放，请再点一次试听，并检查手机媒体音量。"),
		indicator: played ? "green" : "orange",
	});
	return Boolean(played);
}

function setupProcessNotificationSound(options = {}) {
	const frappeRef = options.frappeRef;
	const windowRef = options.windowRef;
	if (!windowRef) return false;
	if (windowRef[PROCESS_NOTIFICATION_SOUND_SETUP_FLAG]) return false;
	if (windowRef[PROCESS_NOTIFICATION_SOUND_PENDING_FLAG]) return false;

	const register = () => {
		if (windowRef[PROCESS_NOTIFICATION_SOUND_SETUP_FLAG]) return false;
		if (!frappeRef?.realtime?.socket && !frappeRef?.realtime?.disabled) return false;

		windowRef[PROCESS_NOTIFICATION_SOUND_SETUP_FLAG] = true;
		windowRef[PROCESS_NOTIFICATION_SOUND_PENDING_FLAG] = false;
		const controller = createProcessNotificationSoundController(options);
		windowRef[PROCESS_NOTIFICATION_SOUND_CONTROLLER] = controller;
		frappeRef.realtime.on?.(PROCESS_NOTIFICATION_EVENT, (message) => {
			if (message?.play_sound === false || message?.play_sound === 0) return;
			controller.play({ notificationId: message?.notification_log });
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
	enableProcessNotificationSound,
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
	const soundOptions = {
		frappeRef: frappe, windowRef: window, storage,
		translate: typeof __ === "function" ? __ : undefined,
		onBlocked: () => frappe.show_alert?.({
			message: __("收到新通知，但浏览器未能播放提示音。请在任务菜单中启用/试听提示音。"),
			indicator: "orange",
		}),
	};
	setupProcessNotificationSound(soundOptions);
	window.process_simplification = window.process_simplification || {};
	window.process_simplification.notification_sound = {
		...processNotificationSoundApi,
		enable: () => enableProcessNotificationSound(soundOptions),
	};
}
