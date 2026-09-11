const test = require("node:test");
const assert = require("node:assert/strict");
const scan = require("../../public/js/document_scan.js");
const worker = require("../../public/js/worker_reporting.js");
const origin = "https://factory.example";
const siteId = "a".repeat(32);
const internalCode = (kind, name) => `HSERP|1|${siteId}|${kind}|${scan.encodeScanName(name)}`;

test("inventory QR codes keep exact document type and require a click for manual search", async () => {
	for (const [kind, doctype] of [["purchase-receipt", "Purchase Receipt"], ["stock-entry", "Stock Entry"], ["delivery-note", "Delivery Note"]]) {
		assert.equal(scan.parseDocumentScan(internalCode(kind, "SAME-1"), siteId).doctype, doctype);
		const page = mountedPage();
		try {
			page.submit("SAME"); await tick();
			page.pending[0].resolve({ message: { results: [{ doctype, name: "SAME-1", title: "库存单据" }] } });
			await tick();
			assert.match(page.results(), /SAME-1/);
			assert.deepEqual(page.routes, []);
			page.openResult(0);
			assert.deepEqual(page.calls[1].args, { doctype, name: "SAME-1" });
			page.pending[1].resolve({ message: { found: true, route: ["Form", doctype, "SAME-1"] } });
			await tick();
			assert.deepEqual(page.routes, [["Form", doctype, "SAME-1"]]);
		} finally { page.restore(); }
	}
});

test("internal codes round-trip unicode and separators without an access address", () => {
	for (const name of ["PO-2026-1", "任务/工序 01?#%", "工单-零件😀", "a".repeat(140)]) {
		const result = scan.parseDocumentScan(internalCode("job-card", name), siteId);
		assert.equal(result.name, name);
		assert.equal(result.doctype, "Job Card");
		assert.equal(result.code, internalCode("job-card", name));
		assert.equal(result.code.includes(":"), false);
		assert.equal(scan.parseDocumentScan(` ${internalCode("purchase-order", name)}\r\n`, siteId).name, name);
	}
});

test("foreign systems, old URLs, unsupported versions and malformed codes cannot navigate", () => {
	for (const value of [
		internalCode("job-card", "JC1").replace(siteId, "b".repeat(32)),
		internalCode("job-card", "JC1").replace("|1|", "|2|"),
		internalCode("job-card", "JC1") + "|extra", internalCode("job-card", "JC1") + "#extra",
		internalCode("Employee", "EMP1"), "javascript:alert(1)", "//factory.example/desk/document-scan/job-card/eA",
		"https://user:pass@factory.example/desk/document-scan/job-card/eA", "/desk/purchase-order/PO1",
		"/desk/document-scan/job-card/eB", "/desk/document-scan/job-card/_w", "/desk/document-scan/job-card/eA==",
		`HSERP|1|${siteId}|job-card|eB`, `HSERP|1|${siteId}|job-card|_w`,
		`HSERP|1|${siteId}|job-card|eA==`, internalCode("__proto__", "x"), "a".repeat(1025),
	]) assert.throws(() => scan.parseDocumentScan(value, siteId), undefined, value);
	for (const value of ["", " ", "a".repeat(141), "PO\n1", null]) assert.throws(() => scan.encodeScanName(value));
	assert.throws(() => scan.parseDocumentScan(internalCode("job-card", "JC1"), null), /尚未初始化/);
});

test("scan focus is consumed once, belongs to one user and expires", () => {
	const store = scan.createScanFocusStore();
	store.set({ user: "a", mode: "active", jobCard: "JC1" });
	assert.equal(store.take("a", "queue"), null);
	assert.equal(store.take("a", "active").jobCard, "JC1");
	assert.equal(store.take("a", "active"), null);
	store.set({ user: "a", mode: "queue", jobCard: "JC2" });
	assert.equal(store.take("b", "queue"), null);
	assert.equal(store.take("a", "queue"), null);
	store.set({ user: "a", mode: "queue", jobCard: "JC3" });
	const now = Date.now;
	try { const later = now() + 61000; Date.now = () => later; assert.equal(store.peek("a", "queue"), null); }
	finally { Date.now = now; }
});

test("production targets keep active and blocked tasks in their existing lists", () => {
	const rows = [
		{ name: "A1", job_card: "JC1", active_report: "R1", timer_paused_at: "today" },
		{ name: "A2", job_card: "JC2", can_start: true },
		{ name: "A3", job_card: "JC3", block_code: "MATERIAL_NOT_TRANSFERRED" },
		{ name: "A4", job_card: "JC4", block_code: "PENDING_REPORT" },
	];
	assert.equal(worker.workerScanTarget(rows, "JC1").mode, "active");
	for (const name of ["JC2", "JC3", "JC4"]) assert.equal(worker.workerScanTarget(rows, name).mode, "queue");
	assert.equal(worker.workerScanTarget(rows, "someone-elses-task"), null);
});

function deferred() {
	let resolve, reject;
	const promise = new Promise((done, failed) => { resolve = done; reject = failed; });
	return { promise, resolve, reject };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));
function fakeLibrary({ permission, startup, dimensions = () => [320, 320], enumerationFails = false, playFails = false } = {}) {
	const calls = [], tracks = [], videos = [], drawings = [];
	const parent = { appendChild(video) { video.attached = true; } };
	const documentRef = new EventTarget();
	documentRef.hidden = false;
	documentRef.getElementById = () => parent;
	documentRef.createElement = (tag) => {
		if (tag === "canvas") return { getContext: () => ({ drawImage: (...args) => drawings.push(args), getImageData: (_x, _y, w, h) => ({ data: new Uint8ClampedArray(w * h * 4) }) }) };
		const video = new EventTarget();
		Object.assign(video, { videoWidth: 1280, videoHeight: 720, currentTime: 1, paused: true, readyState: 4,
			setAttribute() {}, remove() { this.attached = false; }, pause() { this.paused = true; },
			async play() { if (playFails) throw new Error("play denied"); if (startup) await startup.promise; this.paused = false; this.dispatchEvent(new Event("playing")); },
		});
		Object.defineProperties(video, { clientWidth: { get: () => dimensions()[0] }, clientHeight: { get: () => dimensions()[1] } });
		videos.push(video);
		return video;
	};
	const mediaDevices = {
		async getUserMedia(constraints) {
			calls.push(["start", constraints.video]);
			if (permission) await permission.promise;
			const track = new EventTarget();
			Object.assign(track, { readyState: "live", getSettings: () => ({ deviceId: constraints.video.deviceId?.exact || "back" }), stop() { calls.push(["stop"]); this.readyState = "ended"; } });
			tracks.push(track);
			return { getTracks: () => [track], getVideoTracks: () => [track] };
		},
		async enumerateDevices() { if (enumerationFails) throw new Error("enumeration failed"); return [{ kind: "videoinput", deviceId: "back", label: "Rear camera" }]; },
	};
	const Library = {
		mediaDevices, documentRef,
		QRCodeReader: class { decode() { throw new Error("No code"); } reset() {} },
		BinaryBitmap: class {}, HybridBinarizer: class {}, RGBLuminanceSource: class {},
	};
	const createCamera = (options = {}) => new scan.DocumentScanCamera({ readerId: "test", loadLibrary: async () => Library, secure: () => true, onScan() {}, mediaDevices, documentRef, ...options });
	return { Library, calls, tracks, videos, drawings, createCamera };
}

test("camera uses selected or rear device and stops exactly once", async () => {
	const { calls, tracks, createCamera } = fakeLibrary();
	const camera = createCamera();
	await Promise.all([camera.start(), camera.start()]);
	assert.equal(calls.length, 1, "one request, no permission-probe camera");
	assert.equal(calls[0][1].facingMode.ideal, "environment");
	await Promise.all([camera.stop(), camera.stop()]);
	assert.equal(calls.filter(([call]) => call === "stop").length, 1);
	assert.equal(tracks[0].readyState, "ended");
	await camera.start("front");
	assert.equal(calls.at(-1)[1].deviceId.exact, "front");
	await camera.stop();
});

test("the visible guide matches the decoder region after the video dimensions are known", async () => {
	const { createCamera, drawings } = fakeLibrary();
	const regions = [];
	const camera = createCamera({ onRegion: (region) => regions.push(region) });
	await camera.start();
	assert.deepEqual(regions, [{ width: 230, height: 230, left: 45, top: 45 }]);
	assert.equal(drawings[0][3], 517.5, "source crop follows object-fit:cover on a 1280x720 video");
	await camera.stop();
});

function fakeAudio({ blocked = false } = {}) {
	const calls = [], tones = [], levels = [];
	const instances = [];
	class AudioContext {
		constructor() { this.state = "suspended"; this.currentTime = 1; calls.push("create"); instances.push(this); }
		resume() { calls.push("resume"); if (blocked) return Promise.reject(new Error("denied")); this.state = "running"; return Promise.resolve(); }
		createOscillator() {
			const record = {}; tones.push(record);
			return { frequency: { setValueAtTime(value) { record.frequency = value; } }, connect() {}, disconnect() {}, start(time) { calls.push("tone"); record.start = time; }, stop(time) { if (time === undefined) record.cancelled = true; else record.end = time; } };
		}
		createGain() { return { gain: { setValueAtTime(value) { levels.push(value); }, exponentialRampToValueAtTime(value) { levels.push(value); } }, connect() {}, disconnect() {} }; }
		close() { this.state = "closed"; calls.push("close"); return Promise.resolve(); }
	}
	return { AudioContext, calls, instances, tones, levels };
}

test("iOS interrupted audio resumes after camera closure and restores its previous audio session", async () => {
	const audio = fakeAudio();
	audio.navigator = { audioSession: { type: "auto" } };
	const feedback = scan.createScanFeedback({ windowRef: audio });
	feedback.unlock();
	assert.equal(audio.navigator.audioSession.type, "playback");
	audio.instances[0].state = "interrupted";
	assert.equal(await feedback.success(), true);
	assert.equal(audio.calls.filter((call) => call === "resume").length, 2);
	feedback.release();
	await new Promise((done) => setTimeout(done, 550));
	assert.equal(audio.navigator.audioSession.type, "auto");
});

test("workshop cues are louder, sustained and audibly distinct, and error release preserves all three notes", async () => {
	const audio = fakeAudio(), feedback = scan.createScanFeedback({ windowRef: audio });
	feedback.unlock();
	assert.equal(await feedback.success(), true);
	const success = audio.tones.slice();
	assert.equal(success.length, 2);
	assert.ok(success[1].frequency > success[0].frequency);
	assert.ok(success.at(-1).end - success[0].start >= 0.44);
	assert.equal(await feedback.error(), true);
	const error = audio.tones.slice(2);
	assert.equal(error.length, 3);
	assert.ok(error[0].frequency > error[1].frequency && error[1].frequency > error[2].frequency);
	assert.ok(error.at(-1).end - error[0].start >= 0.73);
	assert.equal(Math.max(...audio.levels), 0.36);
	assert.ok(success.every((tone) => tone.cancelled), "new cues must not overlap or sum their volumes");
	feedback.release();
	await new Promise((done) => setTimeout(done, 550));
	assert.equal(audio.instances[0].state, "running", "do not close at the old success cue duration");
	await new Promise((done) => setTimeout(done, 300));
	assert.equal(audio.instances[0].state, "closed");
});

test("muting immediately stops a cue that was retained for route completion", async () => {
	const audio = fakeAudio(), feedback = scan.createScanFeedback({ windowRef: audio });
	feedback.unlock(); await feedback.error(); feedback.release();
	feedback.release({ immediate: true });
	assert.ok(audio.tones.every((tone) => tone.cancelled));
	assert.equal(audio.instances[0].state, "closed");
});

test("pending or denied audio never blocks lookup indefinitely or sounds after leaving", async () => {
	for (const cancel of [false, true]) {
		const audio = fakeAudio(), pending = deferred();
		const feedback = scan.createScanFeedback({ windowRef: audio, resumeTimeout: 10 });
		feedback.unlock();
		audio.instances[0].state = "interrupted";
		audio.instances[0].resume = () => pending.promise;
		const result = feedback.success();
		if (cancel) feedback.release();
		assert.equal(await result, false);
		audio.instances[0].state = "running";
		pending.resolve();
		await tick();
		assert.equal(audio.calls.includes("tone"), false);
		feedback.release();
	}
});

test("manual search lists even one exact match and only opens after a separate permission-checked click", async () => {
	const page = mountedPage();
	try {
		page.submit(" PUR-ORD-2026-00010 ");
		await tick();
		assert.equal(page.calls[0].method, "process_simplification.api.document_scan.search_documents");
		assert.deepEqual(page.calls[0].args, { query: "PUR-ORD-2026-00010", doctype: "" });
		page.pending[0].resolve({ message: { results: [{ doctype: "Purchase Order", name: "PUR-ORD-2026-00010", title: "供应商" }] } });
		await tick();
		assert.deepEqual(page.routes, []);
		assert.match(page.results(), /PUR-ORD-2026-00010/);
		assert.equal(page.preview(), "closed", "search collapses the camera region");
		page.openResult(0); page.openResult(0);
		assert.equal(page.calls.length, 2, "double-click opens only once");
		assert.equal(page.calls[1].method, "process_simplification.api.document_scan.open_result");
		assert.deepEqual(page.calls[1].args, { doctype: "Purchase Order", name: "PUR-ORD-2026-00010" });
		page.pending[1].resolve({ message: { found: true, route: ["Form", "Purchase Order", "PUR-ORD-2026-00010"] } });
		await tick();
		assert.deepEqual(page.routes, [["Form", "Purchase Order", "PUR-ORD-2026-00010"]]);
		page.submit(internalCode("purchase-order", "PO1").replace(siteId, "b".repeat(32)));
		await tick();
		assert.equal(page.calls.length, 2);
	} finally { page.restore(); }
});

test("multiple search results stay visible after an open is denied and search never plays a scan sound", async () => {
	const audio = fakeAudio(), page = mountedPage({ audio });
	try {
		page.submit("JOB002"); await tick();
		page.pending[0].resolve({ message: { results: [{ doctype: "Job Card", name: "PO-JOB00208" }, { doctype: "Job Card", name: "PO-JOB00209" }], has_more: true } });
		await tick();
		assert.match(page.results(), /PO-JOB00208/); assert.match(page.results(), /PO-JOB00209/);
		assert.match(page.results(), /结果较多/);
		assert.deepEqual(page.routes, []);
		assert.equal(audio.calls.length, 0);
		page.openResult(1);
		page.pending[1].resolve({ message: { found: false, message: "派单已撤回" } });
		await tick();
		assert.equal(page.status(), "派单已撤回");
		assert.match(page.results(), /PO-JOB00208/);
		assert.deepEqual(page.routes, []);
	} finally { page.restore(); }
});

test("search results and pending clicks cannot survive a user change or page departure", async () => {
	for (const phase of ["search", "click", "owner"]) {
		const page = mountedPage();
		try {
			page.submit("JOB"); await tick();
			if (phase === "search") page.controller.deactivate();
			page.pending[0].resolve({ message: { results: [{ doctype: "Job Card", name: "JC1" }] } });
			await tick();
			if (phase === "owner") { global.frappe.session.user = "someone-else"; page.openResult(0); }
			if (phase === "click") {
				page.openResult(0); page.controller.deactivate();
				page.pending[1].resolve({ message: { found: true, job_card: "JC1", route: ["my-production-reporting"] } });
				await tick();
			}
			assert.equal(page.results(), "");
			assert.deepEqual(page.routes, []);
			assert.equal(page.focusStore.take("worker-a", "queue"), null);
		} finally { page.restore(); }
	}
});

test("test sound is a user gesture and never reads or navigates a business document", async () => {
	const audio = fakeAudio();
	const page = mountedPage({ audio });
	try {
		await page.click(".ps-scan-test-sound");
		assert.equal(audio.calls.filter((call) => call === "tone").length, 2);
		assert.equal(page.calls.length, 0);
		assert.equal(page.routes.length, 0);
		assert.match(page.status(), /媒体音量/);
	} finally { page.restore(); }
});

test("success feedback unlocks in the gesture, reuses one audio context and respects mute", async () => {
	const audio = fakeAudio();
	let muted = false;
	const feedback = scan.createScanFeedback({ windowRef: audio, muted: () => muted });
	assert.equal(await feedback.success(), false, "never create audio from an asynchronous scan callback");
	feedback.unlock();
	feedback.unlock();
	assert.deepEqual(audio.calls, ["create", "resume"]);
	assert.equal(await feedback.success(), true);
	assert.deepEqual(audio.calls, ["create", "resume", "tone", "tone"]);
	muted = true;
	assert.equal(await feedback.success(), false);
	assert.equal(await feedback.error(), false);
	const blocked = scan.createScanFeedback({ windowRef: fakeAudio({ blocked: true }) });
	blocked.unlock();
	await tick();
	assert.equal(await blocked.success(), false, "blocked audio fails quietly");
	assert.equal(await blocked.error(), false, "blocked rejection audio also fails quietly");
});

test("leaving during camera permission never starts a late camera", async () => {
	const permission = deferred();
	const { tracks, videos, createCamera } = fakeLibrary({ permission });
	const camera = createCamera({ onScan: () => assert.fail("late decode") });
	const start = camera.start();
	await tick();
	await camera.stop();
	await start;
	assert.equal(videos[0].attached, false, "cancel does not wait for the permission prompt");
	permission.resolve();
	await tick();
	assert.equal(tracks[0].readyState, "ended", "late permission results are immediately stopped");
});

test("leaving during camera startup closes the stream when startup resolves", async () => {
	const startup = deferred();
	const { tracks, videos, createCamera } = fakeLibrary({ startup });
	const camera = createCamera({ onScan: () => assert.fail("late decode") });
	const start = camera.start();
	await tick();
	await camera.stop();
	await start;
	assert.equal(tracks[0].readyState, "ended", "release before playing, without waiting for play()");
	assert.equal(videos[0].srcObject, null);
	startup.resolve();
	await tick();
	assert.equal(camera.session, null);
});

test("insecure camera use fails with a useful message before opening a device", async () => {
	const camera = new scan.DocumentScanCamera({ readerId: "test", loadLibrary: () => assert.fail("must not load"), secure: () => false, onScan: () => {} });
	await assert.rejects(camera.start(), /HTTPS/);
	assert.match(scan.cameraErrorMessage({ name: "NotAllowedError" }), /权限/);
	assert.match(scan.cameraErrorMessage({ name: "NotFoundError" }), /没有找到/);
});

test("failed native release retains ownership for retry instead of reporting closed", async () => {
	const { tracks, createCamera } = fakeLibrary();
	const camera = createCamera();
	await camera.start();
	const stop = tracks[0].stop;
	tracks[0].stop = () => { throw new Error("release failure"); };
	await assert.rejects(camera.stop(), /未能完全关闭/);
	assert.ok(camera.session);
	tracks[0].stop = stop;
	await camera.stop();
	assert.equal(camera.session, null);
});

test("optional device enumeration failure neither opens a probe stream nor prevents cleanup", async () => {
	const { calls, tracks, createCamera } = fakeLibrary({ enumerationFails: true });
	const camera = createCamera();
	await camera.start();
	await tick();
	assert.equal(calls.length, 1);
	await camera.stop();
	assert.equal(tracks[0].readyState, "ended");
});

test("play rejection and no-frame timeout release camera and allow a retry", async () => {
	for (const options of [{ playFails: true }, { startup: deferred() }]) {
		const { tracks, videos, createCamera } = fakeLibrary(options);
		const camera = createCamera({ startupTimeout: 20 });
		await assert.rejects(camera.start(), /没有传回画面/);
		assert.equal(tracks[0].readyState, "ended");
		assert.equal(videos[0].srcObject, null);
		assert.equal(camera.session, null);
	}
});

test("late permission from a cancelled session cannot stop the replacement camera", async () => {
	const permission = deferred();
	const env = fakeLibrary();
	const original = env.Library.mediaDevices.getUserMedia;
	let requests = 0;
	env.Library.mediaDevices.getUserMedia = async (constraints) => { if (++requests === 1) await permission.promise; return original(constraints); };
	const camera = env.createCamera();
	const first = camera.start();
	await camera.stop();
	await first;
	await camera.start();
	const replacement = camera.session;
	permission.resolve();
	await tick();
	assert.equal(camera.session, replacement);
	assert.equal(env.tracks[0].readyState, "live");
	assert.equal(env.tracks[1].readyState, "ended");
	await camera.stop();
});

test("capture failure after playing releases resources and reports a recoverable error", async () => {
	const { tracks, createCamera } = fakeLibrary();
	const errors = [];
	const camera = createCamera({ onError: (error) => errors.push(error) });
	await camera.start();
	tracks[0].readyState = "ended";
	tracks[0].dispatchEvent(new Event("ended"));
	assert.equal(errors.length, 1);
	assert.equal(camera.session, null);
});

test("permission timeout cannot leak a later stream", async () => {
	const permission = deferred();
	const { tracks, createCamera } = fakeLibrary({ permission });
	const camera = createCamera({ startupTimeout: 20 });
	await assert.rejects(camera.start(), /没有传回画面/);
	permission.resolve();
	await tick();
	assert.equal(tracks[0].readyState, "ended");
	assert.equal(camera.session, null);
});

test("decoder load failure releases the already-playing camera", async () => {
	const { tracks, createCamera } = fakeLibrary();
	const camera = createCamera({ loadLibrary: async () => { throw new Error("asset unavailable"); } });
	await assert.rejects(camera.start(), /asset unavailable/);
	assert.equal(tracks[0].readyState, "ended");
});

test("a stream that stays live but stops producing frames times out and releases", async () => {
	const { tracks, createCamera } = fakeLibrary();
	const errors = [];
	const camera = createCamera({ startupTimeout: 20, onError: (error) => errors.push(error) });
	await camera.start();
	await new Promise((resolve) => setTimeout(resolve, 230));
	assert.equal(errors.length, 1);
	assert.equal(tracks[0].readyState, "ended");
	assert.equal(camera.session, null);
});

test("focusing a task opens outer material groups and details without running actions", () => {
	const details = { open: false };
	const group = { tagName: "DETAILS", open: false };
	const classes = new Set();
	let focused = false, scrolled = false;
	const card = {
		parentElement: group, getAttribute: () => 'A"[1]',
		querySelectorAll: () => [details], classList: { add: (value) => classes.add(value), remove: (value) => classes.delete(value) },
		setAttribute: () => {}, focus: () => { focused = true; }, scrollIntoView: () => { scrolled = true; },
	};
	const root = { querySelectorAll: (selector) => selector === ".worker-assignment-card" ? [card] : [] };
	group.parentElement = root;
	const previousTimeout = global.setTimeout;
	try {
		global.setTimeout = () => 0;
		assert.equal(worker.focusWorkerAssignment(root, "missing"), false);
		assert.equal(group.open, false);
		assert.equal(worker.focusWorkerAssignment(root, 'A"[1]'), true);
		assert.equal(group.open && details.open && focused && scrolled, true);
		assert.equal(classes.has("ps-scan-focused"), true);
	} finally { global.setTimeout = previousTimeout; }
});

// Exercise the mounted page's real submit/deactivation handlers with a small UI adapter.
function mountedPage({ route = ["document-scan"], scanSite = siteId, audio = {}, library, accountMuted = false, resize } = {}) {
	const previous = { window: global.window, frappe: global.frappe, $: global.$, __: global.__ };
	const nodes = new Map(), handlers = new Map(), calls = [], routes = [], pending = [];
	const node = (selector) => {
		if (!nodes.has(selector)) nodes.set(selector, {
			0: { clientWidth: 320 }, value: "", props: {}, attrs: {}, content: "",
			prop(key, value) { this.props[key] = value; return this; },
			attr(key, value) { this.attrs[key] = value; return this; },
			css(values) { this.styles = values; return this; },
			empty() { this.content = ""; return this; },
			html(value) { this.content = value; return this; },
			text(value) { this.content = value; return this; },
			val(value) { if (arguments.length) { this.value = value; return this; } return this.value; },
			toggleClass() { return this; },
		});
		return nodes.get(selector);
	};
	const root = { appendTo() { return this; }, html() {}, text() { return this; }, val() { return this; }, find: node, on(event, selector, callback) { handlers.set(`${event}:${selector}`, callback); } };
	const focusStore = scan.createScanFocusStore();
	const windowEvents = new EventTarget();
	global.window = { ...audio, isSecureContext: true, ZXing: library, document: library?.documentRef, navigator: { mediaDevices: library?.mediaDevices },
		addEventListener: windowEvents.addEventListener.bind(windowEvents), location: { origin }, process_simplification: { document_scan: { focusStore } } };
	if (resize) global.window.ResizeObserver = class {
		constructor(callback) { resize.fire = (width) => { node(".ps-scan-reader")[0].clientWidth = width; callback(); }; }
		observe() { resize.observed = true; }
		disconnect() { resize.disconnected = true; }
	};
	global.__ = (value) => value;
	global.$ = () => root;
	global.frappe = {
		utils: { escape_html: (value) => value }, dom: { set_unique_id: () => "reader" }, session: { user: "worker-a" },
		boot: { process_document_scan: { site_id: scanSite }, user: { mute_sounds: accountMuted } }, require: async () => {},
		get_route: () => route, route_options: { unrelated_filter: "old" },
		call(args) { calls.push(args); const result = deferred(); pending.push(result); return result.promise; },
		async set_route(route) { routes.push(route); },
	};
	const controller = scan.mountScanPage({ main: {} });
	controller.refresh();
	return {
		controller, calls, routes, pending, focusStore, windowEvents,
		click(selector) { return handlers.get(`click:${selector}`)(); },
		openResult(index) { handlers.get("click:.ps-scan-result")({ currentTarget: { dataset: { resultIndex: String(index) } } }); },
		results: () => node(".ps-scan-results").content,
		preview: () => node(".ps-scan-viewport").attrs["data-state"],
		guide: () => node(".ps-scan-guide").styles,
		submit(value) { node("input").val(value); handlers.get("submit:.ps-scan-form")({ preventDefault() {} }); },
		status: () => node(".ps-scan-status").content,
		cameraDisabled: () => node(".ps-scan-start").props.disabled,
		restore() { controller.deactivate(); Object.assign(global, previous); },
	};
}

test("mounted scanner submits one read for repeated Enter, clears old filters and focuses once", async () => {
	const audio = fakeAudio();
	const page = mountedPage({ audio });
	try {
		page.submit(internalCode("job-card", "JC1"));
		page.submit(internalCode("job-card", "JC1"));
		await tick();
		assert.equal(page.calls.length, 1);
		assert.equal(page.calls[0].type, "GET");
		assert.equal(page.calls[0].method, "process_simplification.api.document_scan.resolve");
		assert.deepEqual(page.calls[0].args, { code: internalCode("job-card", "JC1") });
		page.pending[0].resolve({ message: { found: true, job_card: "JC1", route: ["my-production-reporting"] } });
		await tick();
		assert.deepEqual(page.routes, [["my-production-reporting"]]);
		assert.equal(global.frappe.route_options, null);
		assert.equal(page.focusStore.take("worker-a", "queue").jobCard, "JC1");
		assert.equal(page.focusStore.take("worker-a", "queue"), null);
		assert.equal(audio.calls.filter((call) => call === "tone").length, 2, "one short two-tone sound per successful lookup");
	} finally { page.restore(); }
});

test("leaving or changing users during a scan cannot navigate or transfer task focus", async () => {
	for (const changeUser of [false, true]) {
		const audio = fakeAudio();
		const page = mountedPage({ audio });
		try {
			page.submit(internalCode("job-card", "JC1"));
			await tick();
			if (changeUser) global.frappe.session.user = "worker-b";
			else page.controller.deactivate();
			page.pending[0].resolve({ message: { found: true, job_card: "JC1", route: ["my-production-reporting"] } });
			await tick();
			assert.deepEqual(page.routes, []);
			assert.equal(page.focusStore.take("worker-a", "queue"), null);
			assert.equal(audio.calls.includes("tone"), false);
		} finally { page.restore(); }
	}
});

test("unknown or inaccessible codes play one rejection cue, stay on the scanner and allow the next scan", async () => {
	const audio = fakeAudio();
	const page = mountedPage({ audio });
	try {
		page.submit("https://elsewhere.example/anything");
		await tick();
		assert.equal(page.calls.length, 0);
		page.submit(internalCode("purchase-order", "PO1"));
		await tick();
		page.pending[0].resolve({ message: { found: false, message: "没有查看权限" } });
		await tick();
		assert.equal(page.status(), "没有查看权限");
		assert.deepEqual(page.routes, []);
		assert.equal(audio.tones.length, 6, "one three-note cue for the invalid URL, one for permission rejection");
		assert.ok(audio.tones[0].frequency > audio.tones[1].frequency && audio.tones[1].frequency > audio.tones[2].frequency);
		assert.equal(page.preview(), "closed");
		page.submit(internalCode("purchase-order", "PO2"));
		await tick();
		page.pending[1].resolve({ message: { found: true, route: ["Form", "Purchase Order", "PO2"] } });
		await tick();
		assert.deepEqual(page.routes, [["Form", "Purchase Order", "PO2"]]);
		assert.equal(audio.tones.length, 8, "the next successful scan plays only its two-note cue");
	} finally { page.restore(); }
});

test("a decoded invalid camera code stops capture and repeated frames cannot keep sounding", async () => {
	const audio = fakeAudio(), { Library, tracks } = fakeLibrary();
	Library.QRCodeReader = class { decode() { return { getText: () => "NOT-A-SYSTEM-CODE" }; } reset() {} };
	const page = mountedPage({ audio, library: Library });
	try {
		page.click(".ps-scan-start"); await tick(); await tick();
		assert.equal(tracks[0].readyState, "ended");
		assert.equal(page.preview(), "closed");
		assert.equal(page.calls.length, 0);
		assert.equal(audio.tones.length, 3);
		await new Promise((done) => setTimeout(done, 260));
		assert.equal(audio.tones.length, 3);
	} finally { page.restore(); }
});

test("scan network failure sounds once, while stale or muted rejection results remain silent", async () => {
	for (const scenario of ["network", "leave", "user", "mute"]) {
		const audio = fakeAudio(), page = mountedPage({ audio, accountMuted: scenario === "mute" });
		try {
			page.submit(internalCode("job-card", "JC1")); await tick();
			if (scenario === "leave") page.controller.deactivate();
			if (scenario === "user") global.frappe.session.user = "another-user";
			if (scenario === "network") page.pending[0].reject(new Error("network failed"));
			else page.pending[0].resolve({ message: { found: false, message: "未派工" } });
			await tick();
			assert.equal(audio.tones.length, scenario === "network" ? 3 : 0);
			assert.deepEqual(page.routes, []);
		} finally { page.restore(); }
	}
});

test("error trial plays three notes without reading or navigating a document", async () => {
	const audio = fakeAudio(), page = mountedPage({ audio });
	try {
		await page.click(".ps-scan-test-error");
		assert.equal(audio.tones.length, 3);
		assert.match(page.status(), /错误提示音（三声）/);
		assert.equal(page.calls.length, 0); assert.deepEqual(page.routes, []);
	} finally { page.restore(); }
});

test("camera readiness displays the live guide and closing restores the idle preview", async () => {
	const { Library } = fakeLibrary();
	const page = mountedPage({ library: Library });
	try {
		page.click(".ps-scan-start");
		assert.equal(page.preview(), "starting");
		await tick();
		assert.equal(page.preview(), "live");
		assert.deepEqual(page.guide(), { width: "230px", height: "230px", left: "45px", top: "45px" });
		await page.click(".ps-scan-stop");
		assert.equal(page.preview(), "closed");
		assert.equal(page.cameraDisabled(), false);
	} finally { page.restore(); }
});

test("resizing updates the decoder crop without reopening hardware and leaving disconnects resize handling", async () => {
	let width = 320;
	const resize = {};
	const { Library, calls } = fakeLibrary({ dimensions: () => [width, width] });
	const page = mountedPage({ library: Library, resize });
	try {
		page.click(".ps-scan-start");
		await tick();
		width = 260;
		resize.fire(width);
		assert.equal(calls.filter(([call]) => call === "stop").length, 0);
		assert.equal(calls.filter(([call]) => call === "start").length, 1);
		assert.deepEqual(page.guide(), { width: "187px", height: "187px", left: "36.5px", top: "36.5px" });
		page.controller.deactivate();
		assert.equal(resize.disconnected, true);
		assert.equal(page.calls.length, 0);
	} finally { page.restore(); }
});

test("backgrounding and pagehide release camera synchronously and never auto-restart", async () => {
	for (const event of ["visibilitychange", "pagehide"]) {
		const { Library, tracks, calls } = fakeLibrary();
		const page = mountedPage({ library: Library });
		try {
			page.click(".ps-scan-start");
			await tick();
			assert.equal(tracks[0].readyState, "live");
			if (event === "pagehide") page.windowEvents.dispatchEvent(new Event(event));
			else { Library.documentRef.hidden = true; Library.documentRef.dispatchEvent(new Event(event)); }
			assert.equal(tracks[0].readyState, "ended", "must stop before page suspension, without awaiting promises");
			await tick();
			assert.equal(page.preview(), "closed");
			Library.documentRef.hidden = false;
			Library.documentRef.dispatchEvent(new Event("visibilitychange"));
			page.windowEvents.dispatchEvent(new Event("pageshow"));
			await tick();
			assert.equal(calls.filter(([call]) => call === "start").length, 1);
			page.click(".ps-scan-start");
			await tick();
			assert.equal(tracks[1].readyState, "live");
		} finally { page.restore(); }
	}
});

test("muted or unavailable audio never blocks successful document navigation", async () => {
	for (const options of [{ accountMuted: true, audio: fakeAudio() }, { audio: fakeAudio({ blocked: true }) }]) {
		const page = mountedPage(options);
		try {
			page.submit(internalCode("purchase-order", "PO1"));
			await tick();
			page.pending[0].resolve({ message: { found: true, route: ["Form", "Purchase Order", "PO1"] } });
			await tick();
			assert.equal(page.routes.length, 1);
			assert.equal(options.audio.calls.includes("tone"), false);
		} finally { page.restore(); }
	}
});

test("opening an old printed URL never auto-resolves and explains reprinting", async () => {
	const page = mountedPage({ route: ["document-scan", "job-card", scan.encodeScanName("JC1")] });
	try {
		await tick();
		assert.equal(page.calls.length, 0);
		assert.deepEqual(page.routes, []);
		assert.match(page.status(), /旧版网址二维码已停用/);
		page.submit(internalCode("job-card", "JC1"));
		await tick();
		assert.equal(page.calls.length, 1, "a deliberate internal scan still works from this page");
		page.pending[0].resolve({ message: { found: false } });
		await tick();
	} finally { page.restore(); }
});

test("the same internal code works after the system's access domain changes", async () => {
	const page = mountedPage();
	try {
		global.window.location.origin = "https://new-factory.example:9443";
		page.submit(internalCode("purchase-order", "PO1"));
		await tick();
		assert.deepEqual(page.calls[0].args, { code: internalCode("purchase-order", "PO1") });
		page.pending[0].resolve({ message: { found: true, route: ["Form", "Purchase Order", "PO1"] } });
		await tick();
		assert.deepEqual(page.routes, [["Form", "Purchase Order", "PO1"]]);
	} finally { page.restore(); }
});

test("uninitialized systems explain the problem without opening a camera or calling the resolver", async () => {
	const page = mountedPage({ scanSite: null });
	try {
		assert.equal(page.cameraDisabled(), true);
		assert.match(page.status(), /尚未初始化/);
		page.submit(internalCode("purchase-order", "PO1"));
		await tick();
		assert.equal(page.calls.length, 0);
	} finally { page.restore(); }
});

function mountedWorker({ mode = "queue", rows = [], afterApply = () => {} } = {}) {
	const previous = { window: global.window, frappe: global.frappe, __: global.__, flt: global.flt, format_number: global.format_number, setTimeout: global.setTimeout };
	const focusStore = scan.createScanFocusStore(), routes = [], messages = [];
	const detail = { open: false }, group = { open: false, tagName: "DETAILS" };
	const focused = [];
	const cards = rows.map((row) => ({
		parentElement: group, getAttribute: () => row.name, querySelectorAll: () => [detail],
		classList: { add() {}, remove() {} }, setAttribute() {}, scrollIntoView() {}, focus: () => focused.push(row.name),
	}));
	const domRoot = { querySelectorAll: (selector) => selector === ".worker-assignment-card" ? cards : [] };
	group.parentElement = domRoot;
	const element = { addClass() { return this; }, attr() { return this; }, find() { return this; }, html() { return this; } };
	const root = { 0: domRoot, find: () => element, on() {} };
	const page = { main: root, menu_btn_group: element, add_custom_menu_item: () => element };
	global.__ = (value) => value;
	global.flt = (value) => Number(value || 0);
	global.format_number = (value) => String(value);
	global.setTimeout = () => 0;
	global.window = { process_simplification: { document_scan: { focusStore } } };
	global.frappe = {
		session: { user: "worker-a" }, container: { page: { page } }, utils: { escape_html: (value) => value },
		msgprint: (value) => messages.push(value), set_route: (value) => routes.push(value),
		call: () => assert.fail("focusing a task must not trigger a business action"),
		async ps_read_page(_page, options) { await options.apply({ message: { assignments: rows } }); afterApply({ group, detail }); return true; },
	};
	const reporting = worker.mountWorkerReportingPage({ page, root, mode });
	return { reporting, focusStore, routes, messages, focused, group, detail, restore: () => Object.assign(global, previous) };
}

test("task focus happens after the shared loader restores background expansion", async () => {
	const page = mountedWorker({
		rows: [{ name: "WAITING", job_card: "JC1", block_code: "MATERIAL_NOT_TRANSFERRED" }],
		afterApply: ({ group, detail }) => { group.open = false; detail.open = false; },
	});
	try {
		page.focusStore.set({ user: "worker-a", mode: "queue", jobCard: "JC1" });
		await page.reporting.load({ background: true });
		assert.equal(page.group.open && page.detail.open, true);
		assert.deepEqual(page.focused, ["WAITING"]);
		await page.reporting.load();
		assert.deepEqual(page.focused, ["WAITING"], "normal refresh must not refocus a consumed scan");
	} finally { page.restore(); }
});

test("a task that starts during lookup redirects once, then explains another state change", async () => {
	const page = mountedWorker({ rows: [{ name: "ACTIVE", job_card: "JC1", active_report: "R1" }] });
	try {
		page.focusStore.set({ user: "worker-a", mode: "queue", jobCard: "JC1" });
		await page.reporting.load();
		assert.deepEqual(page.routes, ["active-production-work"]);
		assert.equal(page.focusStore.peek("worker-a", "active").redirected, true);
		page.focusStore.set({ user: "worker-a", mode: "queue", jobCard: "JC1", redirected: true });
		await page.reporting.load();
		assert.equal(page.routes.length, 1);
		assert.match(page.messages[0], /状态刚刚发生变化/);
		assert.deepEqual(page.focused, []);
	} finally { page.restore(); }
});
