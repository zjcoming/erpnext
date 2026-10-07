/* Concentrated material hand-off: selection and preview are read-only. */
const PRODUCTION_MATERIALS_API = "process_simplification.api.production_materials.";
const PRODUCTION_MATERIALS_LIMIT = 20;

function materialEscape(value) {
	return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
}
function materialError(error) {
	const data = error?.responseJSON || error || {};
	try {
		const messages = typeof data._server_messages === "string" ? JSON.parse(data._server_messages) : data._server_messages;
		const message = typeof messages?.[0] === "string" ? JSON.parse(messages[0]) : messages?.[0];
		if (message?.message) return String(message.message);
	} catch (_) { /* use the ordinary message */ }
	return String(data.message || error?.statusText || "操作失败，请刷新或打开原单核对。");
}
function materialUncertain(error) {
	const status = Number(error?.status);
	return error?.name === "TimeoutError" || error?.statusText === "timeout" || !Number.isFinite(status) || status === 0 || status >= 500;
}
function materialBounded(promise, timeout = 60000) {
	return new Promise((resolve, reject) => {
		const timer = setTimeout(() => { const error = new Error("等待超时，服务器仍可能继续处理。"); error.name = "TimeoutError"; reject(error); }, timeout);
		Promise.resolve(promise).then((value) => { clearTimeout(timer); resolve(value); }, (error) => { clearTimeout(timer); reject(error); });
	});
}
function materialSelectionKey(rows) {
	return JSON.stringify(rows.map((row) => [row.name, row.company]).sort((a, b) => a[0].localeCompare(b[0])));
}
function materialSummary(rows) {
	const groups = new Map();
	for (const row of rows) for (const item of row.items || []) {
		const key = JSON.stringify([item.item_code, item.source_warehouse, item.target_warehouse, item.uom]);
		if (!groups.has(key)) groups.set(key, { ...item, qty: 0 });
		groups.get(key).qty += Number(item.qty) || 0;
	}
	return [...groups.values()];
}
function createProductionMaterialsController({ read, preview, request, submit, outcome, render = () => {} }) {
	let revision = 0;
	const state = { rows: [], companies: [], capabilities: {}, view: "", company: "", search: "", start: 0, next_start: null,
		selected: new Set(), results: [], loading: false, running: false, stopped: false, error: "" };
	const publish = () => render(state);
	const selectedRows = () => state.rows.filter((row) => state.selected.has(row.name));
	const unresolved = (name) => state.results.some((result) => result.name === name && result.view === state.view && result.status === "uncertain");
	const selectable = (row) => Boolean(row?.can_select && !unresolved(row.name));
	const api = {
		state, selectedRows, selectable,
		async load(options = {}) {
			if (state.running) return false;
			const token = ++revision;
			for (const key of ["view", "company", "search", "start"]) if (key in options) state[key] = options[key];
			state.selected.clear(); state.loading = true; state.error = ""; publish();
			try {
				const response = await read({ view: state.view || undefined, company: state.company, search: state.search, start: state.start });
				if (token !== revision) return false;
				Object.assign(state, { rows: response.rows || [], companies: response.companies || [], capabilities: response.capabilities || {},
					view: response.view, next_start: response.next_start ?? null });
				return true;
			} catch (error) {
				if (token !== revision) return false;
				state.rows = []; state.error = materialError(error); return false;
			} finally { if (token === revision) { state.loading = false; publish(); } }
		},
		select(name, checked) {
			if (state.loading || state.running) return "";
			if (!checked) { state.selected.delete(name); publish(); return ""; }
			const row = state.rows.find((item) => item.name === name);
			if (!selectable(row)) return "这张单据需先核对状态，不能加入本次处理。";
			if (state.selected.size >= PRODUCTION_MATERIALS_LIMIT && !state.selected.has(name)) return "每批最多选择 20 张。";
			if (selectedRows().some((item) => item.company !== row.company)) return "一次请选择同一公司的单据。";
			state.selected.add(name); publish(); return "";
		},
		selectPage(checked) {
			if (state.loading || state.running) return;
			if (!checked) state.selected.clear();
			else for (const row of state.rows) if (selectable(row) && state.selected.size < PRODUCTION_MATERIALS_LIMIT
				&& !selectedRows().some((item) => item.company !== row.company)) state.selected.add(row.name);
			publish();
		},
		invalidate() { ++revision; state.loading = false; state.selected.clear(); publish(); },
		async prepare() {
			if (state.running || state.loading || !state.selected.size) return null;
			const key = materialSelectionKey(selectedRows()), view = state.view;
			const response = await preview(view, selectedRows().map((row) => row.name));
			if (state.running || state.loading || view !== state.view || key !== materialSelectionKey(selectedRows())) throw new Error("选中范围已变化，请重新预览。");
			if (materialSelectionKey(response.rows || []) !== key) throw new Error("预览与选中单据不一致，请刷新重试。");
			return { view, key, rows: response.rows };
		},
		async execute(plan, accepted = false) {
			if (state.running || state.loading || !plan || plan.view !== state.view || plan.key !== materialSelectionKey(selectedRows())) return false;
			if (!plan.rows.length || plan.rows.length > PRODUCTION_MATERIALS_LIMIT || !plan.rows.some((row) => row.can_select)
				|| new Set(plan.rows.map((row) => row.company)).size !== 1) throw new Error("部分单据已不可处理，请刷新并重新选择。");
			if (!accepted && (plan.view === "confirm" || plan.rows.some((row) => row.can_select && row.partial))) throw new Error("请先确认预览中的领料数量和现场核对事项。");
			state.running = true; state.stopped = false; state.error = ""; publish();
			try {
				for (const row of plan.rows) {
					if (state.stopped) break;
					if (!row.can_select) { state.results.push({ name: row.name, work_order: row.work_order, view: plan.view, status: "skipped", message: `本轮未处理：${row.block_message || "条件未满足"}` }); publish(); continue; }
					const result = { name: row.name, work_order: row.work_order, view: plan.view, status: "running", message: "正在处理…", stock_entry: row.stock_entry };
					state.results.push(result); publish();
					try {
						const value = plan.view === "request" ? await request({ work_order: row.work_order, expected_state: row.expected_state,
							qty: row.request_qty, allow_partial: row.partial && accepted ? 1 : 0 }) : await submit({ stock_entry: row.stock_entry, expected_state: row.expected_state });
						Object.assign(result, { status: "success", stock_entry: value.stock_entry,
							message: plan.view === "request" ? (value.reused ? "已复用原领料申请" : "申请已生成，待库房确认") : (value.reused ? "此前已确认出库" : "已确认出库") });
					} catch (error) {
						if (materialUncertain(error)) {
							result.status = "uncertain"; result.message = "结果尚未确认，后续单据已停止。请先核实，勿重复提交。"; state.stopped = true;
							try { api.applyOutcome(result, await outcome(result)); } catch (_) { /* retain uncertainty */ }
						} else { result.status = "failed"; result.message = materialError(error); }
					}
					publish();
				}
			} finally { state.running = false; state.selected.clear(); publish(); }
			return true;
		},
		applyOutcome(result, value) {
			if (value.status === "submitted") Object.assign(result, { status: "success", stock_entry: value.stock_entry, message: "已核实：单据已出库" });
			else if (value.status === "exists") Object.assign(result, { status: "exists", stock_entry: value.stock_entry, message: "已找到领料申请，请核对原单；无法判断是否来自本次请求" });
		},
		async recheck(index) {
			const result = state.results[index];
			if (!result || state.running || result.status !== "uncertain") return;
			try { api.applyOutcome(result, await outcome(result)); } catch (error) { result.message = `仍未确认：${materialError(error)}`; }
			publish();
		},
		stop() { state.stopped = true; publish(); },
		deactivate() { state.stopped = true; ++revision; state.loading = false; state.selected.clear(); publish(); },
	};
	return api;
}

function materialItemsHtml(items) {
	const esc = materialEscape;
	return `<div class="production-materials-scroll"><table class="table table-bordered"><thead><tr><th>物料</th><th>数量</th><th>领出仓库 → 领入仓库</th></tr></thead><tbody>${items.map((item) => `<tr><td>${esc(item.item_name || item.item_code)}<div class="text-muted small">${esc(item.item_code)}${item.batch_no ? ` · 批次 ${esc(item.batch_no)}` : ""}${item.serial_and_batch_bundle ? ` · 批次/序列号包 ${esc(item.serial_and_batch_bundle)}` : ""}</div></td><td>${esc(Number(item.qty).toFixed(6).replace(/\.?0+$/, ""))} ${esc(item.uom)}</td><td>${esc(item.source_warehouse)} → ${esc(item.target_warehouse)}</td></tr>`).join("")}</tbody></table></div>`;
}
function materialPreviewHtml(rows, view) {
	const esc = materialEscape;
	return `<p>${view === "request" ? "请核对每张工单的领料范围；申请生成后还需库房确认出库。" : "请核对实物与以下物料、数量、仓库一致；确认后按每张原单分别出库。"}</p><h5>本轮合计（仅可处理单据）</h5>${materialItemsHtml(materialSummary(rows.filter((row) => row.can_select)))}${rows.map((row) => `<section class="production-materials-preview"><h5>${esc(row.work_order)}${row.stock_entry ? ` · ${esc(row.stock_entry)}` : ""}${!row.can_select ? " · 本轮不处理" : ""}</h5><p>覆盖生产数量：${esc(row.request_qty)} ${esc(row.stock_uom || "")}${row.can_select && row.partial ? ` · <strong>部分领料，剩余 ${esc(Math.max(0, Number(row.remaining_qty) - Number(row.request_qty)))}</strong>` : ""}</p>${row.block_message ? `<p class="text-warning">${esc(row.block_message)}</p>` : ""}${materialItemsHtml(row.items || [])}</section>`).join("")}`;
}
function materialQuantity(value) {
	return value === null || value === undefined || !Number.isFinite(Number(value)) ? "—"
		: Number(value).toFixed(6).replace(/\.?0+$/, "");
}
function materialViewCopy(view) {
	return view === "confirm" ? {
		title: "库房确认出库", role: "库房办理", action: "核对并预览出库", search: "查找工单或领料申请",
		description: "选中待出库的申请，核对实物、数量和仓库后统一确认。确认成功，原料才会转入车间仓。",
		selection: "先勾选本次实际发出的领料申请，再核对明细。", emptyTitle: "当前没有待确认的领料申请",
		empty: "主管还未申请，或已有申请已完成出库。收到领料申请后，刷新这里办理；完工入库请到“库房工作台”办理。",
	} : {
		title: "集中申请领料", role: "主管办理", action: "预览领料申请", search: "查找订单、工单或产品",
		description: "勾选本次要领料的工单，核对物料后统一申请。库房确认出库后，才算完成领料。",
		selection: "先勾选要领料的工单；一次最多处理同一公司的 20 张。", emptyTitle: "当前没有待领料的工单",
		empty: "可能尚未创建生产计划，或工单已经领齐、完成。请回到订单与进度确认计划；按工序领料的特殊工单仍从原单办理。",
	};
}
function materialRowStatus(row, view, unresolved = false) {
	if (unresolved) return { tone: "warning", label: "上次结果待核实" };
	if (row.requires_native) return { tone: "warning", label: "需在原单核对批次" };
	if (!row.can_select) return { tone: "muted", label: view === "confirm" ? "待核对" : "暂不可领料" };
	if (view === "request" && row.stock_entry) return { tone: "waiting", label: "已申请 · 待库房确认" };
	if (view === "request" && row.partial) return { tone: "warning", label: "只能部分领料" };
	return { tone: "ready", label: view === "confirm" ? "待确认出库" : "可申请领料" };
}
function materialRowHtml(row, { view = "request", selected = false, selectable = Boolean(row.can_select), disabled = false, showCompany = false } = {}) {
	const esc = materialEscape, status = materialRowStatus(row, view, Boolean(row.can_select && !selectable));
	const names = [...new Set((row.items || []).map((item) => item.item_name || item.item_code).filter(Boolean))];
	const title = row.item_name || row.production_item || (view === "confirm" ? (names.length > 1 ? `${names[0]}等 ${names.length} 项物料` : names[0] || "生产领料") : "生产工单");
	const documentType = view === "request" ? "work-order" : "stock-entry";
	const canChoose = selectable && !disabled;
	const detailLink = row.stock_entry ? `/app/stock-entry/${encodeURIComponent(row.stock_entry)}` : `/app/${documentType}/${encodeURIComponent(row.name)}`;
	const detailLabel = row.requires_native ? "打开原单核对批次" : row.stock_entry ? "查看领料申请" : "查看工单";
	const notice = row.can_select && !selectable ? "上次请求的结果还未核实，请先到下方处理结果中核实。" : row.block_message;
	return `<article class="production-material-card${selected ? " is-selected" : ""}${!selectable ? " is-unavailable" : ""}">
		<div class="production-materials-card-top"><label class="production-materials-pick"><input type="checkbox" data-material-select="${esc(row.name)}" aria-label="${esc(`选择 ${title}，${row.name}`)}" ${selected ? "checked" : ""} ${canChoose ? "" : "disabled"}><span><strong class="production-materials-product">${esc(title)}</strong><span class="production-materials-source">${row.sales_order ? `订单 ${esc(row.sales_order)} · ` : ""}工单 ${esc(row.work_order || row.name)}${showCompany ? ` · ${esc(row.company)}` : ""}</span></span></label><span class="production-materials-status is-${status.tone}">${status.label}</span></div>
		<div class="production-materials-card-main"><div class="production-materials-quantity"><span>${view === "confirm" ? "本次领料对应生产" : row.stock_entry ? "已申请覆盖生产" : "本次可领料支持生产"}</span><strong>${esc(materialQuantity(row.request_qty))}<small>${esc(row.stock_uom || "")}</small></strong>${view === "request" ? `<span>工单剩余待领 ${esc(materialQuantity(row.remaining_qty))} ${esc(row.stock_uom || "")}</span>` : `<span>申请 ${esc(row.stock_entry || row.name)}</span>`}</div><div class="production-materials-card-next"><span>${view === "confirm" ? "核对后确认，完成原料出库" : row.stock_entry ? "请库房核对并确认出库" : "选中后，在下方统一预览申请"}</span><a class="production-materials-document-link" href="${detailLink}">${detailLabel} <span aria-hidden="true">↗</span></a></div></div>
		${notice ? `<p class="production-materials-row-notice is-${status.tone}">${esc(notice)}</p>` : ""}
		${(row.items || []).length ? `<details class="production-materials-details"><summary>核对 ${row.items.length} 项物料与仓库</summary>${materialItemsHtml(row.items)}</details>` : ""}
	</article>`;
}
function materialEmptyHtml(state) {
	const esc = materialEscape, copy = materialViewCopy(state.view);
	if (state.loading) return '<div class="production-materials-empty is-loading"><strong>正在读取领料任务…</strong><p>正在核对当前工单和领料申请。</p></div>';
	if (state.error) return `<div class="production-materials-empty"><strong>领料任务暂时没有读取成功</strong><p>请刷新重试；当前已完成的操作可在下方处理结果中核对。</p><button type="button" class="btn btn-default" data-material-refresh>重新读取</button></div>`;
	if (state.search || (state.company && state.companies.length > 1)) return `<div class="production-materials-empty"><strong>没有找到符合条件的单据</strong><p>${state.search ? `当前查找：${esc(state.search)}。` : ""}试试其他关键词，或查看全部待办。</p><button type="button" class="btn btn-default" data-material-clear>查看全部待办</button></div>`;
	return `<div class="production-materials-empty"><span class="production-materials-empty-icon" aria-hidden="true">✓</span><strong>${copy.emptyTitle}</strong><p>${copy.empty}</p><button type="button" class="btn btn-default" data-material-refresh>刷新待办</button></div>`;
}
function materialResultsHtml(results, running) {
	const esc = materialEscape;
	if (!results.length) return "";
	const counts = { success: 0, failed: 0, uncertain: 0 };
	results.forEach((row) => { if (row.status in counts) counts[row.status] += 1; });
	return `<div class="production-materials-results-heading"><h3>处理结果</h3><span>成功 ${counts.success} · 失败 ${counts.failed} · 待核实 ${counts.uncertain}</span></div>${results.map((result, index) => `<div class="production-materials-result is-${result.status}"><div><strong>${esc(result.message)}</strong><small>${esc(result.name)}</small></div><div class="production-materials-result-actions">${result.stock_entry ? `<a class="production-materials-document-link" href="/app/stock-entry/${encodeURIComponent(result.stock_entry)}">查看库存单 ↗</a>` : ""}${result.status === "uncertain" ? `<button type="button" class="btn btn-default" data-material-recheck="${index}" ${running ? "disabled" : ""}>核实结果</button>` : ""}</div></div>`).join("")}`;
}
function mountProductionMaterials($host, options = {}) {
	const esc = materialEscape;
	const rpc = (method, args) => materialBounded(frappe.call({ method: PRODUCTION_MATERIALS_API + method, args })).then((response) => response.message);
	const ownerRoute = options.ownerRoute || frappe.get_route?.()?.[0];
	let initialPreferredView = ["request", "confirm"].includes(options.preferredView) ? options.preferredView : null;
	let active = false, dialog = null, preparing = false, timer = null, activationRevision = 0;
	$host.addClass("production-materials").html(`<header class="production-materials-heading"><div><div class="production-materials-eyebrow"><span data-material-role></span><span data-material-single-company></span></div><h2 data-material-title>集中申请领料</h2><p data-material-description></p></div><div class="production-materials-tabs" role="group" aria-label="领料办理阶段"></div></header><div class="production-materials-toolbar"><label class="production-materials-company-filter">公司<select class="form-control" data-material-company></select></label><label class="production-materials-search"><span data-material-search-label>查找订单、工单或产品</span><input class="form-control" type="search" data-material-search placeholder="输入单号或产品名称"></label><button type="button" class="btn btn-default" data-material-refresh>刷新待办</button></div><div class="production-materials-list-heading"><strong data-material-list-count></strong><label><input type="checkbox" data-material-all> 选择本页可处理单据</label></div><p class="production-materials-notice" role="status"></p><div class="production-materials-list"></div><div class="production-materials-pagination"></div><div class="production-materials-actions"><div class="production-materials-selection"><strong data-material-count>尚未选择工单</strong><span data-material-selection-hint></span></div><div class="production-materials-primary-actions"><button type="button" class="btn btn-default" data-material-stop hidden>停止后续处理</button><button type="button" class="btn btn-primary" data-material-preview>预览领料申请</button></div></div><div class="production-materials-results" aria-live="polite"></div>`);
	const controller = createProductionMaterialsController({
		read: async (args) => {
			const revision = activationRevision;
			const response = await rpc("get_material_workbench", args);
			// Resolve an initial preference only after learning this user's capabilities.
			// Subsequent reads retain the current stage, including a user's own switch.
			if (active && revision === activationRevision && !args.view && initialPreferredView &&
				response.capabilities?.[initialPreferredView] && response.view !== initialPreferredView) {
				return rpc("get_material_workbench", { ...args, view: initialPreferredView });
			}
			return response;
		},
		preview: (view, names) => rpc(view === "request" ? "preview_material_requests" : "preview_material_submissions", view === "request" ? { work_orders: JSON.stringify(names) } : { stock_entries: JSON.stringify(names) }),
		request: (args) => rpc("request_material", { ...args, expected_state: JSON.stringify(args.expected_state) }),
		submit: (args) => rpc("submit_material", args),
		outcome: (result) => rpc("get_material_outcome", result.view === "request" ? { work_order: result.work_order } : { stock_entry: result.stock_entry }),
		render(state) {
			const blocked = state.loading || state.running || preparing;
			const copy = materialViewCopy(state.view), available = state.rows.filter(controller.selectable).length;
			const views = Object.entries(state.capabilities).filter(([, enabled]) => enabled);
			$host.find("[data-material-role]").text(copy.role);
			$host.find("[data-material-title]").text(copy.title);
			$host.find("[data-material-description]").text(copy.description);
			$host.find("[data-material-search-label]").text(copy.search);
			$host.find("[data-material-search]").attr("placeholder", state.view === "confirm" ? "输入工单或领料申请单号" : "输入订单、工单或产品名称");
			$host.find(".production-materials-tabs").prop("hidden", views.length < 2).html(views.map(([view]) => `<button type="button" class="btn ${view === state.view ? "btn-primary" : "btn-default"}" data-material-view="${view}" aria-pressed="${view === state.view}" ${blocked ? "disabled" : ""}>${view === "request" ? "申请领料" : "库房确认出库"}</button>`).join(""));
			$host.find("[data-material-company]").html(`<option value="">全部可访问公司</option>${state.companies.map((company) => `<option value="${esc(company)}">${esc(company)}</option>`).join("")}`).val(state.company);
			$host.find(".production-materials-company-filter").prop("hidden", state.companies.length <= 1);
			$host.find("[data-material-single-company]").text(state.companies.length === 1 ? state.companies[0] : "").prop("hidden", state.companies.length !== 1);
			$host.find("[data-material-company], [data-material-search], [data-material-refresh]").prop("disabled", blocked);
			$host.find("[data-material-list-count]").text(state.loading ? "正在核对待办" : `本页 ${state.rows.length} 张 · ${available} 张可处理`);
			$host.find("[data-material-all]").prop("disabled", blocked || !available).prop("checked", state.selected.size > 0).prop("indeterminate", state.selected.size > 0 && state.selected.size < available);
			$host.find("[data-material-count]").text(state.selected.size ? `已选择 ${state.selected.size} 张${state.view === "confirm" ? "领料申请" : "工单"}` : state.view === "confirm" ? "尚未选择领料申请" : "尚未选择工单");
			$host.find("[data-material-selection-hint]").text(state.selected.size ? (state.view === "confirm" ? "下一步核对实物、数量和仓库，再确认出库。" : "下一步核对各工单用料；申请后仍需库房确认。") : copy.selection);
			$host.find("[data-material-preview]").text(`${copy.action}${state.selected.size ? ` · ${state.selected.size} 张` : ""}`).prop("disabled", blocked || !state.selected.size);
			$host.find(".production-materials-actions").prop("hidden", !state.rows.length);
			$host.find("[data-material-stop]").prop("hidden", !state.running).prop("disabled", state.stopped);
			$host.find(".production-materials-notice").text(state.loading ? "正在读取领料任务…" : state.error || (state.running ? (state.stopped ? "后续处理已停止，正在核实当前单据。" : "正在逐张处理…") : ""));
			$host.find(".production-materials-list").html(state.rows.length ? state.rows.map((row) => materialRowHtml(row, { view: state.view, selected: state.selected.has(row.name), selectable: controller.selectable(row), disabled: blocked, showCompany: state.companies.length > 1 })).join("") : materialEmptyHtml(state));
			$host.find(".production-materials-pagination").prop("hidden", !state.start && state.next_start === null).html(`<button type="button" class="btn btn-default" data-material-page="0" ${blocked || !state.start ? "disabled" : ""}>回到首页</button><span>每页最多 20 张</span><button type="button" class="btn btn-default" data-material-page="${state.next_start}" ${blocked || state.next_start === null ? "disabled" : ""}>下一页</button>`);
			$host.find(".production-materials-results").prop("hidden", !state.results.length).html(materialResultsHtml(state.results, state.running));
		},
	});
	const refresh = (options = {}) => {
		if (controller.state.running || preparing || (options.background && (dialog || controller.state.selected.size))) return Promise.resolve(false);
		return controller.load();
	};
	const changeFilter = () => { dialog?.hide(); dialog = null; controller.load({ company: $host.find("[data-material-company]").val(), search: $host.find("[data-material-search]").val(), start: 0 }); };
	$host.on("click", "[data-material-view]", (event) => { dialog?.hide(); dialog = null; controller.load({ view: event.currentTarget.dataset.materialView, start: 0 }); });
	$host.on("change", "[data-material-company]", changeFilter);
	$host.on("input", "[data-material-search]", () => { clearTimeout(timer); controller.invalidate(); timer = setTimeout(changeFilter, 300); });
	$host.on("click", "[data-material-refresh]", () => refresh());
	$host.on("click", "[data-material-clear]", () => { $host.find("[data-material-search]").val(""); controller.load({ company: "", search: "", start: 0 }); });
	$host.on("click", "[data-material-page]", (event) => controller.load({ start: Number(event.currentTarget.dataset.materialPage) }));
	$host.on("change", "[data-material-all]", (event) => controller.selectPage(event.currentTarget.checked));
	$host.on("change", "[data-material-select]", (event) => { const error = controller.select(event.currentTarget.dataset.materialSelect, event.currentTarget.checked); if (error) { event.currentTarget.checked = false; frappe.msgprint(__(error)); } });
	$host.on("click", "[data-material-stop]", () => controller.stop());
	$host.on("click", "[data-material-recheck]", (event) => controller.recheck(Number(event.currentTarget.dataset.materialRecheck)));
	$host.on("click", "[data-material-preview]", async () => {
		if (preparing || controller.state.running) return;
		const revision = activationRevision;
		preparing = true; $host.find("[data-material-preview]").prop("disabled", true);
		try {
			const plan = await controller.prepare();
			if (!active || revision !== activationRevision || !plan) return;
			const eligibleCount = plan.rows.filter((row) => row.can_select).length;
			const blocked = !eligibleCount;
			const needsConsent = plan.view === "confirm" || plan.rows.some((row) => row.can_select && row.partial);
			dialog = new frappe.ui.Dialog({ title: __(plan.view === "confirm" ? "核对后集中出库" : "集中领料预览"), size: "extra-large", fields: [
				{ fieldname: "preview", fieldtype: "HTML", options: materialPreviewHtml(plan.rows, plan.view) },
				...(needsConsent ? [{ fieldname: "accepted", fieldtype: "Check", label: __(plan.view === "confirm" ? "已核对以上实物、数量及领出/领入仓库，确认出库" : "确认以上标记的工单先部分领料，余料后续再领") }] : []),
			], primary_action_label: __(plan.view === "confirm" ? `确认出库 ${eligibleCount} 张` : `生成或复用 ${eligibleCount} 张申请`),
				async primary_action(values) {
					if (!active || revision !== activationRevision || blocked) return;
					if (needsConsent && !values.accepted) { frappe.msgprint(__("请先勾选核对确认。")); return; }
					dialog.hide(); dialog = null;
					try { await controller.execute(plan, Boolean(values.accepted)); if (active) await refresh(); }
					catch (error) { if (active && revision === activationRevision) frappe.msgprint(esc(materialError(error))); }
				},
			});
			const shownDialog = dialog;
			dialog.$wrapper.on("hide.bs.modal.production-materials", () => { if (dialog === shownDialog) dialog = null; });
			dialog.show(); dialog.get_primary_btn().prop("disabled", blocked);
		} catch (error) { if (active && revision === activationRevision) frappe.msgprint(esc(materialError(error))); }
		finally {
			if (revision === activationRevision) {
				preparing = false; $host.find("[data-material-preview]").prop("disabled", !controller.state.selected.size);
			}
		}
	});
	const deactivate = () => { active = false; activationRevision++; preparing = false; clearTimeout(timer); dialog?.hide(); dialog = null; controller.deactivate(); };
	frappe.router?.on("change", () => { if (active && frappe.get_route?.()?.[0] !== ownerRoute) deactivate(); });
	return { controller, activate(options = {}) {
		active = true;
		if (Object.prototype.hasOwnProperty.call(options, "search")) {
			if (controller.state.running || preparing) return Promise.resolve(false);
			initialPreferredView = null;
			clearTimeout(timer); dialog?.hide(); dialog = null;
			const search = String(options.search || "").trim().slice(0, 140);
			$host.find("[data-material-search]").val(search);
			return controller.load({ search, company: "", start: 0, ...(controller.state.capabilities.request ? { view: "request" } : {}) });
		}
		return refresh();
	}, deactivate, refresh };
}

const productionMaterialsApi = { createProductionMaterialsController, materialSummary, materialSelectionKey, materialItemsHtml,
	materialPreviewHtml, materialUncertain, materialBounded, materialQuantity, materialViewCopy, materialRowStatus,
	materialRowHtml, materialEmptyHtml, materialResultsHtml, mountProductionMaterials };
if (typeof module !== "undefined" && module.exports) module.exports = productionMaterialsApi;
if (typeof frappe !== "undefined") {
	frappe.provide("process_simplification.production_materials");
	Object.assign(process_simplification.production_materials, productionMaterialsApi, { mount: mountProductionMaterials });
}
