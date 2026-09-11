"use strict";

const DOCUMENT_SCAN_TYPES = { "purchase-order": "Purchase Order", "job-card": "Job Card", "purchase-receipt": "Purchase Receipt", "stock-entry": "Stock Entry", "delivery-note": "Delivery Note" };
const DOCUMENT_SCAN_LABELS = { "Purchase Order": "采购订单", "Job Card": "生产任务单", "Purchase Receipt": "采购收退货单", "Stock Entry": "库存作业单", "Delivery Note": "销售出退货单" };
const DOCUMENT_SCAN_INVALID = "无法识别这个单据码，请扫描本系统采购、生产或库存单据上的二维码。";
const DOCUMENT_SCAN_LEGACY = "旧版网址二维码已停用，请重新打印单据，在系统内“扫一扫”中扫描新码。";
const DOCUMENT_SCAN_NOT_INITIALIZED = "扫码功能尚未初始化，请联系管理员完成系统升级。";

function encodeScanName(name) {
	if (typeof name !== "string" || !name.trim() || [...name].length > 140 || /[\x00-\x1f\x7f]/.test(name)) {
		throw new Error(DOCUMENT_SCAN_INVALID);
	}
	const bytes = new TextEncoder().encode(name);
	return btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function decodeScanTarget(kind, token) {
	if (!Object.hasOwn(DOCUMENT_SCAN_TYPES, kind) || typeof token !== "string" || !/^[A-Za-z0-9_-]{1,750}$/.test(token)) {
		throw new Error(DOCUMENT_SCAN_INVALID);
	}
	try {
		const binary = atob(token.replaceAll("-", "+").replaceAll("_", "/"));
		const name = new TextDecoder("utf-8", { fatal: true }).decode(Uint8Array.from(binary, (char) => char.charCodeAt(0)));
		if (encodeScanName(name) !== token) throw new Error();
		return { kind, token, name, doctype: DOCUMENT_SCAN_TYPES[kind] };
	} catch {
		throw new Error(DOCUMENT_SCAN_INVALID);
	}
}

function parseDocumentScan(input, siteId) {
	if (typeof input !== "string" || input.length > 1024) throw new Error(DOCUMENT_SCAN_INVALID);
	const value = input.trim();
	if (/^(?:https?:\/\/|\/)/i.test(value)) throw new Error(DOCUMENT_SCAN_LEGACY);
	if (typeof siteId !== "string" || !/^[0-9a-f]{32}$/.test(siteId)) throw new Error(DOCUMENT_SCAN_NOT_INITIALIZED);
	const parts = value.split("|");
	if (parts.length !== 5 || parts[0] !== "HSERP" || parts[1] !== "1" || !/^[0-9a-f]{32}$/.test(parts[2])) {
		throw new Error(DOCUMENT_SCAN_INVALID);
	}
	if (parts[2] !== siteId) throw new Error("这不是当前系统的单据二维码，请确认单据所属系统。");
	return { ...decodeScanTarget(parts[3], parts[4]), code: value };
}

function createScanFocusStore() {
	let pending = null;
	return {
		set(value) { pending = { ...value, created: Date.now() }; },
		peek(user, mode) {
			if (!pending || pending.user !== user || Date.now() - pending.created > 60000) {
				pending = null;
				return null;
			}
			return pending.mode === mode ? pending : null;
		},
		take(user, mode) { const value = this.peek(user, mode); if (value) pending = null; return value; },
		clear() { pending = null; },
	};
}

function cameraErrorMessage(error) {
	if (error?.name === "ScanVideoError") return error.message;
	if (error?.name === "NotAllowedError" || /permission|notallowed/i.test(String(error))) {
		return "摄像头权限未开启，请在浏览器中允许访问摄像头，也可以使用下方输入框。";
	}
	if (error?.name === "NotFoundError" || /notfound|not found/i.test(String(error))) {
		return "没有找到可用摄像头，请使用扫码枪或粘贴单据码。";
	}
	return "摄像头启动失败，请检查摄像头是否被占用，或使用下方输入框。";
}

function documentScanIcon() {
	return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3H4a1 1 0 0 0-1 1v4m13-5h4a1 1 0 0 1 1 1v4M3 16v4a1 1 0 0 0 1 1h4m13-5v4a1 1 0 0 1-1 1h-4M7 7h3v3H7zm7 0h3v3h-3zM7 14h3v3H7zm7 0h3m-3 3h3v-3"/></svg>';
}

function createScanFeedback({ windowRef, muted = () => false, resumeTimeout = 500 }) {
	let context, playingUntil = 0, originalSessionType;
	const activeTones = new Set(), pendingClose = new Map();
	const stopTones = () => {
		for (const item of activeTones) {
			try { item.tone.stop(); } catch { /* Already ended. */ }
			item.cleanup();
		}
	};
	const restoreSession = () => {
		if (!context && !pendingClose.size && originalSessionType !== undefined) {
			try { if (windowRef.navigator?.audioSession?.type === "playback") windowRef.navigator.audioSession.type = originalSessionType; } catch { /* Optional API. */ }
			originalSessionType = undefined;
		}
	};
	const closeAudio = (audio) => {
		try { if (audio?.close) void audio.close().catch(() => {}); } catch { /* Already closed. */ }
		pendingClose.delete(audio);
		restoreSession();
	};
	const patterns = {
		success: [[0, 988, 0.16], [0.22, 1319, 0.22]],
		error: [[0, 740, 0.16], [0.24, 622, 0.16], [0.48, 494, 0.25]],
	};
	const resume = (audio) => {
		try { return Promise.resolve(audio.resume()).catch(() => {}); }
		catch { return Promise.resolve(); }
	};
	async function play(kind, isCurrent = () => true) {
		const audio = context;
		if (muted() || !audio || !isCurrent()) return false;
		if (audio.state !== "running") {
			let timer;
			try { await Promise.race([resume(audio), new Promise((done) => { timer = setTimeout(done, resumeTimeout); })]); }
			finally { clearTimeout(timer); }
		}
		if (audio !== context || muted() || audio.state !== "running" || !isCurrent()) return false;
		try {
			stopTones(); // A new cue replaces a previous test/result instead of amplifying overlapping notes.
			const pattern = patterns[kind];
			// Keep release after the entire cue, including the longer rejection pattern.
			playingUntil = Date.now() + Math.ceil((pattern.at(-1)[0] + pattern.at(-1)[2] + 0.08) * 1000);
			for (const [offset, frequency, duration] of pattern) {
				const tone = audio.createOscillator(), gain = audio.createGain();
				const start = audio.currentTime + offset;
				tone.type = "sine";
				tone.frequency.setValueAtTime(frequency, start);
				gain.gain.setValueAtTime(0.0001, start);
				// Three times the former peak gain, with a sustained note instead of an immediate fade.
				gain.gain.exponentialRampToValueAtTime(0.36, start + 0.012);
				gain.gain.setValueAtTime(0.36, start + duration - 0.035);
				gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
				tone.connect(gain);
				gain.connect(audio.destination);
				const item = { tone, cleanup: () => { tone.disconnect(); gain.disconnect(); activeTones.delete(item); } };
				activeTones.add(item);
				tone.onended = item.cleanup;
				tone.start(start);
				tone.stop(start + duration + 0.01);
			}
			return true;
		} catch { return false; }
	}
	return {
		unlock() {
			if (muted()) return;
			try {
				const AudioContext = windowRef.AudioContext || windowRef.webkitAudioContext;
				if (!AudioContext) return;
				if (!context || context.state === "closed") context = new AudioContext();
				// iOS otherwise treats Web Audio as ambient audio, including its ringer mute policy.
				try {
					const session = windowRef.navigator?.audioSession;
					if (session) {
						if (originalSessionType === undefined) originalSessionType = session.type;
						session.type = "playback";
					}
				} catch { /* Audio Session is optional on other browsers. */ }
				// Resume inside the camera / submit click, before permissions or HTTP awaits.
				if (context.state !== "running") void resume(context);
				// Prime the output during the gesture, before camera activation can interrupt it.
				if (context.createBufferSource && context.createBuffer) {
					const prime = context.createBufferSource();
					prime.buffer = context.createBuffer(1, 1, context.sampleRate);
					prime.connect(context.destination);
					prime.onended = () => prime.disconnect();
					prime.start();
				}
			} catch { /* Sound must never block scanning. */ }
		},
		success: (isCurrent) => play("success", isCurrent),
		error: (isCurrent) => play("error", isCurrent),
		release({ immediate = false } = {}) {
			const previous = context;
			context = null;
			if (immediate) {
				stopTones();
				for (const [audio, timer] of pendingClose) { clearTimeout(timer); closeAudio(audio); }
				if (previous) closeAudio(previous);
			} else if (previous) {
				pendingClose.set(previous, setTimeout(() => closeAudio(previous), Math.max(0, playingUntil - Date.now())));
			}
			restoreSession();
		},
	};
}

const scanVideoError = () => Object.assign(new Error("相机没有传回画面，已停止本次扫码。请重新开启；若桌面应用仍无画面，可用 Safari 打开同一网址扫码。"), { name: "ScanVideoError" });

function waitForScanStep(promise, signal, timeout) {
	return new Promise((resolve, reject) => {
		const aborted = () => finish(reject, Object.assign(new Error("Scan cancelled"), { name: "AbortError" }));
		const timer = setTimeout(() => finish(reject, scanVideoError()), timeout);
		const finish = (done, value) => { clearTimeout(timer); signal.removeEventListener("abort", aborted); done(value); };
		signal.addEventListener("abort", aborted, { once: true });
		Promise.resolve(promise).then((value) => finish(resolve, value), (error) => finish(reject, error));
		if (signal.aborted) aborted();
	});
}

// Use the bundled ZXing core only for pixels. Camera ownership stays here: no
// library permission-probe stream, unobserved video.play(), file/blob URLs or
// scanner-state-dependent cleanup. Frames never leave the browser.
function createScanDecoder(Library) {
	const reader = new Library.QRCodeReader();
	return (canvas) => {
		const { width, height } = canvas;
		const pixels = canvas.getContext("2d", { willReadFrequently: true }).getImageData(0, 0, width, height).data;
		const luminance = new Uint8ClampedArray(width * height);
		for (let i = 0, p = 0; i < luminance.length; i++, p += 4) luminance[i] = (pixels[p] + 2 * pixels[p + 1] + pixels[p + 2]) >> 2;
		try {
			return reader.decode(new Library.BinaryBitmap(new Library.HybridBinarizer(new Library.RGBLuminanceSource(luminance, width, height)))).getText();
		} finally { reader.reset(); }
	};
}

class DocumentScanCamera {
	constructor({ readerId, loadLibrary, secure, onScan, onCameras = () => {}, onRegion = () => {}, onError = () => {},
		mediaDevices = globalThis.navigator?.mediaDevices, documentRef = globalThis.document, startupTimeout = 12000, decodeFactory = createScanDecoder }) {
		Object.assign(this, { readerId, loadLibrary, secure, onScan, onCameras, onRegion, onError, mediaDevices, documentRef, startupTimeout, decodeFactory });
		this.session = null;
		this.sequence = 0;
	}
	start(deviceId) {
		if (this.session && !this.session.abort.signal.aborted) return this.session.starting;
		if (this.session) this.close(this.session); // Retry any previous failed release before opening another device.
		const session = { abort: new AbortController(), cleanup: [], id: ++this.sequence, lastFrameAt: Date.now() };
		this.session = session;
		session.starting = this.open(session, deviceId).catch((error) => {
			const cancelled = session.abort.signal.aborted;
			this.close(session);
			if (!cancelled) throw error;
		});
		return session.starting;
	}
	current(session) { return this.session === session && !session.abort.signal.aborted; }
	async open(session, deviceId) {
		if (!this.secure()) throw new Error("调用摄像头需要 HTTPS，请通过安全地址打开系统，或使用下方输入框。");
		if (!this.mediaDevices?.getUserMedia) throw scanVideoError();
		const parent = this.documentRef.getElementById(this.readerId);
		const video = this.documentRef.createElement("video");
		session.video = video;
		video.muted = true;
		video.autoplay = true;
		video.playsInline = true;
		for (const name of ["muted", "autoplay", "playsinline", "webkit-playsinline"]) video.setAttribute(name, "");
		parent.appendChild(video);
		// Request once, directly in the start-button gesture. A late permission
		// result belongs to this session and is stopped even after cancel/reopen.
		const request = this.mediaDevices.getUserMedia({ audio: false, video: {
			...(deviceId ? { deviceId: { exact: deviceId } } : { facingMode: { ideal: "environment" } }),
			width: { ideal: 1280 }, height: { ideal: 720 },
		} }).then((stream) => {
			session.stream = stream;
			if (!this.current(session)) { this.close(session); return; }
			return stream;
		});
		const stream = await waitForScanStep(request, session.abort.signal, this.startupTimeout);
		if (!this.current(session)) return;
		const interrupted = () => this.fail(session, scanVideoError());
		for (const track of stream.getTracks()) {
			track.addEventListener("ended", interrupted);
			session.cleanup.push(() => track.removeEventListener("ended", interrupted));
		}
		video.srcObject = stream;
		const ready = new Promise((resolve, reject) => {
			const check = () => { if (!video.paused && video.readyState >= 2 && video.videoWidth && video.videoHeight) resolve(); };
			const failed = () => reject(scanVideoError());
			video.addEventListener("playing", check);
			video.addEventListener("loadeddata", check);
			video.addEventListener("error", failed);
			session.cleanup.push(() => { video.removeEventListener("playing", check); video.removeEventListener("loadeddata", check); video.removeEventListener("error", failed); });
			Promise.resolve(video.play()).then(check, failed);
		});
		await waitForScanStep(ready, session.abort.signal, this.startupTimeout);
		if (!this.current(session)) return;
		this.updateRegion(session);
		// enumerateDevices is optional, never opens a second stream or blocks video.
		Promise.resolve().then(() => this.mediaDevices.enumerateDevices?.()).then((devices) => {
			if (this.current(session)) this.onCameras((devices || []).filter((item) => item.kind === "videoinput").map((item) => ({ id: item.deviceId, label: item.label })), stream.getVideoTracks()[0]?.getSettings?.().deviceId || deviceId);
		}).catch(() => {});
		const Library = await waitForScanStep(this.loadLibrary(), session.abort.signal, this.startupTimeout);
		if (!this.current(session)) return;
		session.decode = this.decodeFactory(Library);
		session.canvas = this.documentRef.createElement("canvas");
		this.scanFrame(session);
	}
	updateRegion(session) {
		if (!this.current(session) || !session.video?.videoWidth) return;
		const { clientWidth: width, clientHeight: height } = session.video;
		if (!width || !height) return;
		const size = Math.floor(Math.min(width, height) * 0.72);
		session.region = { width: size, height: size, left: (width - size) / 2, top: (height - size) / 2 };
		this.onRegion(session.region);
	}
	resize() { if (this.session) this.updateRegion(this.session); }
	scanFrame(session) {
		if (!this.current(session)) return;
		const video = session.video;
		if (!video.paused && video.readyState >= 2 && video.currentTime !== session.lastTime) {
			session.lastTime = video.currentTime;
			session.lastFrameAt = Date.now();
			const region = session.region;
			if (region) {
				// Map the visible guide through object-fit:cover to the source pixels.
				const scale = Math.max(video.clientWidth / video.videoWidth, video.clientHeight / video.videoHeight);
				const side = region.width / scale;
				const canvas = session.canvas;
				canvas.width = canvas.height = Math.min(640, Math.floor(side));
				try {
					canvas.getContext("2d", { willReadFrequently: true }).drawImage(video, (video.videoWidth - side) / 2, (video.videoHeight - side) / 2, side, side, 0, 0, canvas.width, canvas.height);
					const text = session.decode(canvas);
					if (text && this.current(session)) this.onScan(text);
				} catch { /* No QR in this frame is normal. */ }
			}
		} else if (Date.now() - session.lastFrameAt > this.startupTimeout) {
			this.fail(session, scanVideoError());
			return;
		}
		if (this.current(session)) session.timer = setTimeout(() => this.scanFrame(session), 200);
	}
	fail(session, error) {
		if (!this.current(session)) return;
		try { this.close(session); } catch (cleanupError) { error = cleanupError; }
		this.onError(error);
	}
	close(session) {
		session.abort.abort();
		clearTimeout(session.timer);
		for (const cleanup of session.cleanup.splice(0)) cleanup();
		// Synchronous release is essential before iOS suspends a background PWA.
		let failed = false;
		for (const track of session.stream?.getTracks() || []) {
			try { if (track.readyState !== "ended") track.stop(); } catch { failed = true; }
			if (track.readyState !== "ended") failed = true;
		}
		if (failed) throw Object.assign(new Error("相机未能完全关闭，请退出当前页面后重试。"), { name: "ScanVideoError" });
		session.video?.pause();
		if (session.video) session.video.srcObject = null;
		session.video?.remove();
		if (session.canvas) session.canvas.width = session.canvas.height = 0;
		if (this.session === session) this.session = null;
	}
	async stop() { if (this.session) this.close(this.session); }
}

function mountScanPage(page) {
	const esc = frappe.utils.escape_html;
	const root = $('<div class="ps-document-scan">').appendTo(page.main);
	root.html(`<div class="ps-scan-intro"><span class="ps-scan-heading-icon">${documentScanIcon()}</span><div><h2>${__("扫一扫")}</h2><p>${__("采购 · 生产 · 库存单据")}</p></div></div>
		<div class="ps-scan-viewport" data-state="idle">
			<div class="ps-scan-reader" aria-label="${esc(__("相机实时画面"))}"></div>
			<div class="ps-scan-placeholder"><span class="ps-scan-placeholder-icon">${documentScanIcon()}</span><strong class="ps-scan-preview-title">${__("对准单据二维码")}</strong><span class="ps-scan-preview-detail">${__("点击下方按钮，开启相机")}</span></div>
			<div class="ps-scan-guide" hidden aria-hidden="true"><span class="ps-scan-corner ps-scan-corner-tl"></span><span class="ps-scan-corner ps-scan-corner-tr"></span><span class="ps-scan-corner ps-scan-corner-bl"></span><span class="ps-scan-corner ps-scan-corner-br"></span><span class="ps-scan-line"></span></div>
			<span class="ps-scan-live-label" hidden>${__("正在扫码")}</span><span class="ps-scan-view-tip" hidden>${__("二维码放入框内，保持纸面平整")}</span>
		</div>
		<div class="ps-scan-camera-actions"><button type="button" class="btn btn-primary ps-scan-start">${documentScanIcon()}<span>${__("开始扫码")}</span></button><button type="button" class="btn btn-default ps-scan-stop" hidden>${__("关闭相机")}</button><button type="button" class="btn btn-default ps-scan-sound" aria-label="${esc(__("扫码提示音"))}" aria-pressed="true"></button></div>
		<label class="ps-scan-camera-choice" hidden>${__("选择摄像头")}<select class="form-control"></select></label>
		<div class="ps-scan-audio-check"><button type="button" class="btn btn-default ps-scan-test-sound">${__("成功试音")}</button><button type="button" class="btn btn-default ps-scan-test-error">${__("错误试音")}</button><span>${__("车间使用请调高手机媒体音量")}</span></div>
		<p class="ps-scan-status" role="status" aria-live="polite"></p>
		<form class="ps-scan-form"><label for="ps-scan-input">${__("输入单号或名称关键词")}</label><select class="form-control ps-scan-document-type" aria-label="${esc(__("单据类型"))}"><option value="">${__("全部单据类型")}</option>${Object.entries(DOCUMENT_SCAN_LABELS).map(([value, label]) => `<option value="${esc(value)}">${__(label)}</option>`).join("")}</select><div class="ps-scan-input-row"><input id="ps-scan-input" class="form-control" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="${esc(__("例如 JOB002、物料名或供应商"))}"><button class="btn btn-default" type="submit">${__("搜索单据")}</button></div><p class="ps-scan-search-hint">${__("搜索后点击结果打开；扫码枪可在此输入完整二维码。")}</p></form><section class="ps-scan-results" aria-label="${esc(__("搜索结果"))}" hidden></section>`);
	const readerId = frappe.dom.set_unique_id(root.find(".ps-scan-reader"));
	const focusStore = window.process_simplification.document_scan.focusStore;
	let active = false, busy = false, generation = 0, routeKey = null, previewState = "idle", soundEnabled = true;
	let searchResults = [], resultOwner = null;
	const clearResults = () => {
		searchResults = []; resultOwner = null;
		root.find(".ps-scan-results").empty().prop("hidden", true);
	};
	const muted = () => !soundEnabled || Boolean(frappe.boot?.user?.mute_sounds);
	const feedback = createScanFeedback({ windowRef: window, muted });
	const siteId = () => frappe.boot?.process_document_scan?.site_id;
	const status = (message, error = false) => root.find(".ps-scan-status").text(__(message)).toggleClass("text-danger", error);
	const buttons = () => {
		const unavailable = !/^[0-9a-f]{32}$/.test(siteId() || "");
		const scanning = ["starting", "live", "closing"].includes(previewState);
		root.find(".ps-scan-start").prop("hidden", scanning).prop("disabled", busy || unavailable);
		root.find(".ps-scan-stop").prop("hidden", !scanning).prop("disabled", busy).text(__(previewState === "closing" ? "正在关闭…" : "关闭相机"));
		root.find(".ps-scan-form button").prop("disabled", busy || unavailable);
		root.find(".ps-scan-form input, .ps-scan-document-type").prop("disabled", busy);
		root.find(".ps-scan-result").prop("disabled", busy);
		root.find(".ps-scan-camera-choice select").prop("disabled", busy || unavailable || previewState === "starting");
		root.find(".ps-scan-sound").text(__(muted() ? "提示音关" : "提示音开"))
			.attr("aria-pressed", String(!muted())).prop("disabled", Boolean(frappe.boot?.user?.mute_sounds));
		root.find(".ps-scan-test-sound, .ps-scan-test-error").prop("disabled", muted() || busy);
	};
	const preview = (state) => {
		previewState = state;
		const live = state === "live";
		root.find(".ps-scan-viewport").attr("data-state", state);
		root.find(".ps-scan-placeholder").prop("hidden", live);
		root.find(".ps-scan-guide, .ps-scan-live-label, .ps-scan-view-tip").prop("hidden", !live);
		root.find(".ps-scan-preview-title").text(__(state === "starting" ? "正在打开相机…" : state === "closing" ? "正在关闭相机…" : state === "closed" ? "相机已关闭" : state === "success" ? "已找到单据" : "对准单据二维码"));
		root.find(".ps-scan-preview-detail").text(__(state === "starting" ? "首次使用请允许摄像头权限" : state === "success" ? "正在打开，请稍候" : "点击下方按钮，开启相机"));
		if (!live) root.find(".ps-scan-camera-choice").prop("hidden", true);
		buttons();
	};
	let cameraCount = 0;
	const camera = new DocumentScanCamera({
		readerId,
		mediaDevices: window.navigator?.mediaDevices,
		documentRef: window.document,
		secure: () => window.isSecureContext,
		loadLibrary: async () => {
			await frappe.require("/assets/frappe/node_modules/html5-qrcode/third_party/zxing-js.umd.js");
			return window.ZXing;
		},
		onScan: (value) => { void find(value); },
		onError: (error) => {
			if (!active) return;
			++generation;
			busy = false;
			preview("idle");
			feedback.release();
			status(cameraErrorMessage(error), true);
		},
		onRegion: (region) => {
			if (!active || busy || previewState !== "starting" && previewState !== "live") return;
			root.find(".ps-scan-guide").css(Object.fromEntries(Object.entries(region).map(([key, value]) => [key, `${value}px`])));
			preview("live");
			root.find(".ps-scan-camera-choice").prop("hidden", cameraCount < 2);
			status("将二维码对准框内，识别后自动查找单据。");
		},
		onCameras: (cameras, selected) => {
			cameraCount = cameras.length;
			const select = root.find(".ps-scan-camera-choice select").empty();
			cameras.forEach((item, index) => $("<option>").val(item.id).text(item.label || `${__("摄像头")} ${index + 1}`).appendTo(select));
			select.val(selected);
			root.find(".ps-scan-camera-choice").prop("hidden", previewState !== "live" || cameraCount < 2);
		},
	});
	const readerElement = root.find(".ps-scan-reader")[0];
	const resizeObserver = readerElement && window.ResizeObserver ? new window.ResizeObserver(() => {
		// Crop from the current display geometry; resizing does not reopen hardware.
		if (active && !busy && previewState === "live") camera.resize();
	}) : null;
	async function find(input, manual = false) {
		if (!active || busy) return;
		const value = String(input || "").trim();
		if (manual && !value.startsWith("HSERP|") && !/^(?:[a-z][a-z0-9+.-]*:|\/\/|\/desk\/)/i.test(value)) {
			try { encodeScanName(value); return search(value); }
			catch (error) { status(error.message, true); return; }
		}
		if (manual) feedback.unlock(); // A keyboard scanner submits its complete code in this gesture.
		let args, scanError;
		try { args = { code: parseDocumentScan(value, siteId()).code }; }
		catch (error) { scanError = error; }
		clearResults();
		busy = true;
		const current = ++generation;
		const user = frappe.session.user;
		buttons();
		status("正在查找单据…");
		try {
			await camera.stop();
			if (!active || current !== generation || user !== frappe.session.user) return;
			preview("closed");
			if (scanError) {
				status(scanError.message, true);
				await feedback.error(() => active && current === generation && user === frappe.session.user);
				return;
			}
			const response = await frappe.call({ method: "process_simplification.api.document_scan.resolve", type: "GET", args });
			if (!active || current !== generation || user !== frappe.session.user) return;
			const result = response.message;
			if (!result?.found) {
				status(result?.message || "没有找到对应单据，请重试。", true);
				await feedback.error(() => active && current === generation && user === frappe.session.user);
				return;
			}
			await navigateResult(result, current, user, true);
		} catch {
			if (active && current === generation && user === frappe.session.user) {
				status("单据读取失败，请确认登录状态和网络后重试。", true);
				await feedback.error(() => active && current === generation && user === frappe.session.user);
			}
		} finally {
			if (current === generation) { busy = false; buttons(); feedback.release(); }
		}
	}
	async function navigateResult(result, current, user, withSound = false) {
		preview("success");
		status("已找到单据，正在打开…");
		if (withSound) await feedback.success(() => active && current === generation && user === frappe.session.user);
		if (!active || current !== generation || user !== frappe.session.user) return;
		focusStore.clear();
		if (result.job_card) focusStore.set({ user, jobCard: result.job_card, mode: result.route[0] === "active-production-work" ? "active" : "queue" });
		frappe.route_options = null;
		await frappe.set_route(result.route);
	}
	async function search(query) {
		clearResults();
		busy = true;
		const current = ++generation, user = frappe.session.user;
		buttons();
		try {
			await camera.stop();
			if (!active || current !== generation || user !== frappe.session.user) return;
			preview("closed");
			feedback.release();
			status("正在搜索单据…");
			const response = await frappe.call({ method: "process_simplification.api.document_scan.search_documents", type: "GET", args: { query, doctype: root.find(".ps-scan-document-type").val() || "" } });
			if (!active || current !== generation || user !== frappe.session.user) return;
			searchResults = (response.message?.results || []).filter((row) => Object.hasOwn(DOCUMENT_SCAN_LABELS, row.doctype) && typeof row.name === "string");
			resultOwner = user;
			if (!searchResults.length) {
				status("未找到当前账号可见的单据，请调整关键词或单据类型。工人仅能搜索自己的任务。");
				return;
			}
			root.find(".ps-scan-results").html(`<h3>${__("搜索结果")} <span>${searchResults.length}${response.message.has_more ? "+" : ""}</span></h3>${searchResults.map((row, index) => `<button type="button" class="ps-scan-result" data-result-index="${index}"><span class="ps-scan-result-type">${__(DOCUMENT_SCAN_LABELS[row.doctype])}</span><strong>${esc(row.name)}</strong><span class="ps-scan-result-title">${esc(row.title || "")}</span><span class="ps-scan-result-detail">${esc([row.detail, row.status].filter(Boolean).join(" · "))}</span><span class="ps-scan-result-open">${__("点击打开")} →</span></button>`).join("")}${response.message.has_more ? `<p class="ps-scan-search-hint">${__("结果较多，请补充关键词或选择单据类型。")}</p>` : ""}`).prop("hidden", false);
			status("请选择要打开的单据，点击后会检查当前权限或派单状态。");
			root.find(".ps-scan-results h3")[0]?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
		} catch {
			if (active && current === generation) status("搜索失败，请检查登录状态和网络后重试。", true);
		} finally {
			if (current === generation) { busy = false; buttons(); }
		}
	}
	async function openSearchResult(index) {
		if (!active || busy) return;
		const result = searchResults[index], user = frappe.session.user;
		if (!result || resultOwner !== user) { clearResults(); return; }
		busy = true;
		const current = ++generation;
		buttons(); status("正在检查权限并打开…");
		try {
			const response = await frappe.call({ method: "process_simplification.api.document_scan.open_result", type: "GET", args: { doctype: result.doctype, name: result.name } });
			if (!active || current !== generation || user !== frappe.session.user) return;
			if (!response.message?.found) { status(response.message?.message || "暂时无法打开，请重新搜索或联系主管。", true); return; }
			await navigateResult(response.message, current, user);
		} catch {
			if (active && current === generation) status("无法打开单据，请检查登录状态、权限和网络。", true);
		} finally {
			if (current === generation) { busy = false; buttons(); }
		}
	}
	async function startCamera(deviceId) {
		if (!active || busy || !/^[0-9a-f]{32}$/.test(siteId() || "")) return;
		clearResults();
		feedback.unlock();
		const current = ++generation;
		preview("starting");
		status("正在打开相机，请允许浏览器使用摄像头。");
		try { await camera.start(deviceId); }
		catch (error) {
			feedback.release();
			if (active && current === generation) { preview("idle"); status(error.message?.includes("HTTPS") ? error.message : cameraErrorMessage(error), true); }
		}
	}
	root.on("click", ".ps-scan-start", () => { void startCamera(); });
	root.on("click", ".ps-scan-stop", async () => {
		const current = ++generation;
		busy = true;
		preview("closing");
		try {
			await camera.stop();
			if (!active || current !== generation) return;
			preview("closed");
			status("摄像头已关闭，可重新扫码或使用输入框。");
		} catch (error) {
			if (active && current === generation) { preview("idle"); status(cameraErrorMessage(error), true); }
		} finally {
			feedback.release();
			if (current === generation) { busy = false; buttons(); }
		}
	});
	async function switchCamera(deviceId) {
		if (!active || busy) return;
		const current = ++generation;
		busy = true;
		preview("starting");
		try {
			await camera.stop();
			if (!active || current !== generation) return;
			busy = false;
			await startCamera(deviceId);
		} catch (error) {
			if (active && current === generation) { busy = false; preview("idle"); status(cameraErrorMessage(error), true); }
		}
	}
	root.on("change", ".ps-scan-camera-choice select", (event) => { void switchCamera(event.target.value); });
	root.on("click", ".ps-scan-sound", () => { soundEnabled = !soundEnabled; if (!muted() && previewState === "live") feedback.unlock(); else feedback.release({ immediate: true }); buttons(); });
	async function testSound(kind) {
		if (!active || busy || muted()) return;
		const current = generation;
		feedback.unlock();
		const played = await feedback[kind](() => active && current === generation);
		if (active && current === generation) status(played ? (kind === "error" ? "已播放错误提示音（三声）；请按车间环境调整媒体音量。" : "已播放成功提示音（两声）；请按车间环境调整媒体音量。") : "提示音暂未播放，请重新点击试音或检查浏览器声音权限。", !played);
		if (previewState !== "live" && previewState !== "starting") feedback.release();
	}
	root.on("click", ".ps-scan-test-sound", () => testSound("success"));
	root.on("click", ".ps-scan-test-error", () => testSound("error"));
	root.on("submit", ".ps-scan-form", (event) => { event.preventDefault(); void find(root.find("input").val(), true); });
	root.on("click", ".ps-scan-result", (event) => { void openSearchResult(Number(event.currentTarget.dataset.resultIndex)); });
	root.on("input", "#ps-scan-input", clearResults);
	root.on("change", ".ps-scan-document-type", clearResults);
	const controller = {
		refresh() {
			const route = frappe.get_route();
			const key = JSON.stringify(route);
			if (active && key === routeKey) return;
			active = true;
			if (resizeObserver) resizeObserver.observe(readerElement);
			focusStore.clear();
			clearResults();
			routeKey = key;
			++generation;
			busy = false;
			preview("idle");
			void camera.stop().catch((error) => status(cameraErrorMessage(error), true));
			root.find("input").val("");
			if (route.length > 1) {
				// Old URL routes may still be printed on paper; never turn their path into a lookup.
				status(DOCUMENT_SCAN_LEGACY, true);
			} else if (!/^[0-9a-f]{32}$/.test(siteId() || "")) {
				status(DOCUMENT_SCAN_NOT_INITIALIZED, true);
			} else {
				status("找到单据后，请在原页面核对并操作。");
			}
		},
		deactivate() {
			active = false;
			clearResults();
			resizeObserver?.disconnect();
			busy = false;
			++generation;
			void camera.stop().catch((error) => status(cameraErrorMessage(error), true));
			feedback.release();
			preview("closed");
		},
		suspend() {
			if (!active) return;
			const wasOpen = ["starting", "live", "closing"].includes(previewState) || busy;
			++generation;
			busy = false;
			// stop() releases tracks synchronously before the browser freezes the page.
			void camera.stop().then(() => {
				if (wasOpen) { preview("closed"); status("相机已关闭，返回后请点击“开始扫码”。"); }
			}).catch((error) => { preview("idle"); status(cameraErrorMessage(error), true); });
			feedback.release();
		},
	};
	window.document?.addEventListener("visibilitychange", () => { if (window.document.hidden) controller.suspend(); });
	window.addEventListener?.("pagehide", () => controller.suspend());
	window.process_simplification.document_scan.currentPage = controller;
	return controller;
}

const documentScanApi = { encodeScanName, decodeScanTarget, parseDocumentScan, createScanFocusStore, cameraErrorMessage, createScanFeedback, DocumentScanCamera, mountScanPage };
if (typeof module !== "undefined" && module.exports) module.exports = documentScanApi;
if (typeof window !== "undefined" && typeof frappe !== "undefined") {
	window.process_simplification = window.process_simplification || {};
	window.process_simplification.document_scan = { ...documentScanApi, focusStore: createScanFocusStore() };
	const api = window.process_simplification.document_scan;
	const open = () => { frappe.route_options = null; return frappe.set_route("document-scan"); };
	function installEntry() {
		if (!frappe.boot?.page_info?.["document-scan"]) return;
		const page = frappe.container?.page?.page;
		if (!page?.add_action_icon) return;
		if (frappe.get_route()?.[0] === "document-scan" || page.wrapper.attr("id") === "page-document-scan") {
			page.wrapper.find(".ps-document-scan-entry").remove();
			page.menu?.find(".ps-document-scan-menu-item").parent().remove();
			return;
		}
		if (!page.wrapper.find(".ps-document-scan-entry").length) {
			page.add_action_icon("scan-barcode", open, "ps-document-scan-entry", "扫一扫")
				.attr("aria-label", __("扫一扫")).html(`${documentScanIcon()}<span>${__("扫一扫")}</span>`)
				// Keep the visible action outside Frappe's desktop-only icon group.
				.prependTo(page.standard_actions);
		}
		// Frappe hides page-icon-group on phones/tablets; its icons are not moved into the menu.
		if (page.menu?.length && page.add_custom_menu_item && !page.menu.find(".ps-document-scan-menu-item").length) {
			page.add_custom_menu_item(page.menu, __("扫一扫"), open, false, null, "scan-barcode")
				.addClass("ps-document-scan-menu-item");
		}
		if (!page.ps_scan_observer) {
			page.ps_scan_observer = new MutationObserver(installEntry);
			for (const target of [page.standard_actions?.[0], page.icon_group?.[0], page.menu?.[0]].filter(Boolean)) {
				page.ps_scan_observer.observe(target, { childList: true });
			}
		}
	}
	const change = () => {
		if (frappe.get_route()?.[0] !== "document-scan") api.currentPage?.deactivate();
		setTimeout(installEntry, 0);
		frappe.after_ajax?.(installEntry);
	};
	$(document).on("app_ready.document_scan page-change.document_scan", change);
	frappe.router.on("change", change);
	$(document).on("desktop_screen.document_scan", (_event, { desktop }) => {
		if (frappe.boot?.page_info?.["document-scan"]) desktop.add_menu_item({ label: __("扫一扫"), icon: "scan-barcode", order: 5, onClick: open });
	});
	$(installEntry);
}
