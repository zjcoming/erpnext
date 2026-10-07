const WORKER_PREARRANGEMENT_API = "process_simplification.api.production_prearrangement.";

function workerPlannedEscape(value) {
	return String(value ?? "").replace(/[&<>"']/g, (character) => ({
		"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
	})[character]);
}

function workerPlannedStatus(task) {
	if (task.can_start) return { label: "可以开始", indicator: "green" };
	if (["MATERIAL_NOT_FULLY_ISSUED", "MATERIAL_NOT_TRANSFERRED", "DIRECT_MATERIAL_SHORTAGE"].includes(task.block_code)) {
		return { label: "等待发料", indicator: "gray" };
	}
	if (task.block_code === "PREVIOUS_OPERATION_PENDING") return { label: "等待前序", indicator: "gray" };
	if (task.block_code === "ACTIVE_WORK_SESSION") return { label: "正在做其他任务", indicator: "blue" };
	return { label: "需处理", indicator: "orange" };
}

function workerPlannedErrorText(error) {
	const data = error?.responseJSON || error || {};
	try {
		const messages = typeof data._server_messages === "string" ? JSON.parse(data._server_messages) : data._server_messages;
		const first = typeof messages?.[0] === "string" ? JSON.parse(messages[0]) : messages?.[0];
		if (first?.message) return String(first.message).replace(/<[^>]*>/g, " ");
	} catch (_error) { /* Keep a useful fallback if the response was interrupted. */ }
	return String(data.message || error?.statusText || "暂时无法读取任务，请刷新重试。");
}

function workerPlannedUncertain(error) {
	const status = Number(error?.status);
	return error?.name === "TimeoutError" || error?.statusText === "timeout"
		|| !Number.isFinite(status) || status === 0 || status >= 500;
}

function workerPlannedBounded(promise, timeout = 60000, { schedule = setTimeout, cancel = clearTimeout } = {}) {
	return new Promise((resolve, reject) => {
		const timer = schedule(() => {
			const error = new Error("请求等待超时，正在核实任务状态。");
			error.name = "TimeoutError";
			error.status = 0;
			reject(error);
		}, timeout);
		Promise.resolve(promise).then(
			(value) => { cancel(timer); resolve(value); },
			(error) => { cancel(timer); reject(error); }
		);
	});
}

function workerPlannedCardHtml(task, options = {}) {
	const translate = options.translate || ((text) => text);
	const escape = options.escapeHtml || workerPlannedEscape;
	const number = options.formatNumber || ((value) => escape(value));
	const action = options.action || {};
	const uncertain = action.status === "uncertain" || action.status === "checking";
	const meta = uncertain ? { label: "需处理", indicator: "orange" }
		: action.status === "starting" ? { label: "正在开始", indicator: "blue" } : workerPlannedStatus(task);
	const disabled = options.loading || options.busy || options.unresolved || !task.can_start || uncertain;
	const actionLabel = action.status === "starting" ? "正在开始…" : uncertain ? "正在核实开工结果" : "开始计时";
	const explanation = uncertain ? action.message : action.message || task.block_message;
	return `<article class="worker-assignment-card ${task.can_start && !uncertain ? "is-ready" : "is-blocked"}" data-plan="${escape(task.name)}">
		<div class="worker-assignment-heading"><div><strong>${escape(task.operation)}</strong><span>${escape(task.item_name || task.production_item || "")}</span></div><span class="indicator-pill ${meta.indicator}">${escape(translate(meta.label))}</span></div>
		<div class="worker-assignment-key-facts"><span><small>${translate("本人安排数量")}</small><strong>${number(task.planned_qty)}</strong></span><span><small>${translate("负责人")}</small><strong>${escape(task.supervisor_name || task.supervisor || "-")}</strong></span></div>
		${explanation ? `<p class="worker-assignment-block-message" role="status">${escape(explanation)}</p>` : ""}
		${task.notes ? `<p class="text-muted worker-assignment-note">${escape(task.notes)}</p>` : ""}
		<div class="worker-assignment-actions">
			${task.can_start || uncertain ? `<button type="button" class="btn btn-primary worker-planned-start" data-plan="${escape(task.name)}" ${disabled ? "disabled" : ""}>${escape(translate(actionLabel))}</button>` : ""}
			${action.status === "uncertain" ? `<button type="button" class="btn btn-default worker-planned-check" data-plan="${escape(task.name)}" ${options.busy ? "disabled" : ""}>${translate("核实结果")}</button>` : ""}
		</div>
		<details class="worker-assignment-more"><summary>${translate("任务详情")}</summary><div class="worker-assignment-facts"><span>${translate("生产任务单")}：${escape(task.job_card)}</span><span>${translate("生产工单")}：${escape(task.work_order)}</span><span>${translate("开始后才计时，等待期间不计时。")}</span></div></details>
	</article>`;
}

function createWorkerPrearrangementController({ read, start, dashboard, render = () => {}, onStarted = () => {}, requestId = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}` }) {
	const state = { tasks: [], actions: new Map(), loading: false, busy: false, error: "", notice: "", noticeType: "danger" };
	let pendingLoad = null;
	const publish = () => render(state);
	const unresolved = () => [...state.actions.values()].filter((action) => ["uncertain", "checking"].includes(action.status));
	const api = {
		state,
		accept(data) {
			state.tasks = Array.isArray(data?.tasks) ? [...data.tasks] : [];
			for (const action of unresolved()) {
				if (!state.tasks.some((task) => task.name === action.task.name)) state.tasks.push(action.task);
			}
			state.loading = false;
			state.error = "";
			publish();
		},
		loading() { state.loading = true; publish(); },
		failed(error) { state.loading = false; state.error = workerPlannedErrorText(error); publish(); },
		reconcile(data) {
			for (const action of unresolved()) {
				if (!(data.assignments || []).some((row) => row.job_card === action.task.job_card && row.active_report)) continue;
				action.status = "started";
				state.tasks = state.tasks.filter((task) => task.name !== action.task.name);
				state.notice = "任务已经开始计时，可在“正在做”中查看。";
				state.noticeType = "success";
			}
			publish();
		},
		load() {
			if (pendingLoad) return pendingLoad;
			state.loading = true;
			state.error = "";
			publish();
			pendingLoad = Promise.resolve().then(async () => {
				try {
					const data = await read();
					// An interrupted request may still be processing. Keep its card until
					// the actual worker dashboard confirms where the task went.
					api.accept(data);
					return true;
				} catch (error) {
					state.error = workerPlannedErrorText(error);
					return false;
				} finally { state.loading = false; pendingLoad = null; publish(); }
			});
			return pendingLoad;
		},
		async check(plan) {
			const action = state.actions.get(plan);
			if (!action || action.status !== "uncertain") return false;
			action.status = "checking";
			action.message = "正在读取实际任务和计时状态…";
			publish();
			try {
				const data = await dashboard();
				const assignment = (data.assignments || []).find((row) => row.job_card === action.task.job_card && row.active_report);
				if (assignment) {
					action.status = "started";
					state.tasks = state.tasks.filter((task) => task.name !== plan);
					await onStarted({ assignment: assignment.name, report: assignment.active_report, recovered: true });
					return true;
				}
				action.status = "uncertain";
				action.message = "尚未确认开始计时，原请求可能仍在处理。请稍后核实结果，暂勿重复开工。";
			} catch (_error) {
				action.status = "uncertain";
				action.message = "暂时无法核实开工结果。请稍后核实，暂勿重复开工。";
			}
			publish();
			return false;
		},
		async begin(plan) {
			const task = state.tasks.find((row) => row.name === plan);
			if (!task?.can_start || state.busy || state.loading || state.error || unresolved().length) return false;
			const action = { task: { ...task }, requestId: requestId(), status: "starting", message: "" };
			state.notice = "";
			state.actions.set(plan, action);
			state.busy = true;
			publish();
			try {
				const result = await start({ plan, expected_revision: task.revision, request_id: action.requestId });
				if (result?.status !== "started" || result.plan !== plan || !result.assignment || !result.report) {
					throw new Error("服务器未返回可确认的开工结果。");
				}
				action.status = "started";
				state.tasks = state.tasks.filter((row) => row.name !== plan);
				await onStarted(result);
				return true;
			} catch (error) {
				if (workerPlannedUncertain(error)) {
					action.status = "uncertain";
					await api.check(plan);
				} else {
					action.status = "failed";
					action.message = workerPlannedErrorText(error);
					state.notice = action.message;
					state.noticeType = "danger";
					await api.load();
				}
				return action.status === "started";
			} finally { state.busy = false; publish(); }
		},
	};
	return api;
}

function mountWorkerPrearrangement({ page, root }) {
	if (page.worker_prearrangement) return page.worker_prearrangement;
	const formal = page.worker_reporting;
	const translate = typeof __ === "function" ? __ : ((text) => text);
	const call = async (method, args = {}) => (await workerPlannedBounded(frappe.call({
		method: WORKER_PREARRANGEMENT_API + method, args, type: "POST", silent: true,
	}))).message || {};
	const controller = createWorkerPrearrangementController({
		read: () => call("get_my_planned_tasks"),
		start: (args) => call("start_planned_task", args),
		dashboard: async () => {
			if (await formal.load() === false) throw new Error("任务状态读取失败。");
			return formal.state.data;
		},
		onStarted: async () => {
			frappe.show_alert({ message: translate("已经开始计时。"), indicator: "green" });
			frappe.set_route("active-production-work");
		},
		// One renderer owns both task sources, so independent state updates cannot
		// clear another source's cards or put waiting work before ready work.
		render(state) {
			if (state.loading) formal.disableStarts();
			else formal.render();
		},
	});
	controller.query = { method: WORKER_PREARRANGEMENT_API + "get_my_planned_tasks", args: {} };
	root.on("click", ".worker-planned-start", async (event) => {
		const plan = event.currentTarget.getAttribute("data-plan");
		if (!formal.canStartTask("planned", plan)) return;
		if (!await controller.begin(plan)) await formal.load({ background: true });
	});
	root.on("click", ".worker-planned-check", (event) => controller.check(event.currentTarget.getAttribute("data-plan")));
	page.worker_prearrangement = controller;
	return controller;
}

const workerPrearrangementApi = { workerPlannedEscape, workerPlannedStatus, workerPlannedCardHtml, workerPlannedUncertain, workerPlannedBounded, createWorkerPrearrangementController, mountWorkerPrearrangement };
if (typeof module !== "undefined" && module.exports) module.exports = workerPrearrangementApi;
if (typeof window !== "undefined") {
	window.process_simplification = window.process_simplification || {};
	window.process_simplification.worker_prearrangement = workerPrearrangementApi;
}
