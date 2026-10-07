/* Arrange people once; actual starts retain the production service's checks. */
const PRODUCTION_PLAN_API = "process_simplification.api.production_prearrangement.";
const PRODUCTION_PLAN_POOL_API = "process_simplification.api.production_planning.get_planning_pool";

function productionPlanSelectable(row) {
	return Boolean(row?.planning?.can_plan && row.job_card && Number(row.for_quantity) > 0);
}

function productionPlanInitialAllocations(row) {
	const plan = row.planning?.plan;
	if (plan?.status === "Planned") return (plan.allocations || []).map((entry) => ({ ...entry }));
	const workers = row.planning?.default_roster?.workers || [];
	return workers.map((worker) => ({ employee: worker.employee, employee_name: worker.employee_name,
		assigned_qty: workers.length === 1 ? Number(row.for_quantity) : 0, notes: "" }));
}

function productionPlanState(row) {
	const context = row.planning || {};
	const plan = context.plan;
	if (row.has_assignment_history || plan?.status === "Activated") return { label: "已进入生产", color: "blue" };
	if (!context.can_plan) return { label: "需处理", color: "orange" };
	if (plan?.status !== "Planned") return { label: "未安排", color: "orange" };
	if (context.can_start) return { label: "已安排 · 可开工", color: "green" };
	return { label: context.start_block_code === "PREVIOUS_OPERATION_PENDING" ? "已安排 · 待前序" : ["MATERIAL_NOT_FULLY_ISSUED", "DIRECT_MATERIAL_SHORTAGE", "DIRECT_PRIORITY_CONFLICT"].includes(context.start_block_code) ? "已安排 · 待料" : "已安排 · 待就绪", color: "gray" };
}

function productionPlanCategory(row) {
	const context = row.planning || {};
	if (row.has_assignment_history || context.plan?.status === "Activated") return "scheduled";
	if (!context.can_plan) return "attention";
	if (context.plan?.status !== "Planned") return "unscheduled";
	const waiting = ["PREVIOUS_OPERATION_PENDING", "MATERIAL_NOT_FULLY_ISSUED", "MATERIAL_NOT_TRANSFERRED", "DIRECT_MATERIAL_SHORTAGE"];
	if (!context.can_start && context.start_block_code && !waiting.includes(context.start_block_code)) return "attention";
	return "scheduled";
}

function productionPlanPageCounts(rows = []) {
	return rows.reduce((counts, row) => { counts[productionPlanCategory(row)]++; return counts; },
		{ all: rows.length, unscheduled: 0, scheduled: 0, attention: 0 });
}

function productionPlanActivationRequest(state, options = {}) {
	return Object.prototype.hasOwnProperty.call(options, "search")
		? { page: 1, statusFilter: "all", filters: { ...state.filters, company: String(options.company ?? "").trim(), operation: "", saved_only: 0, search: String(options.search ?? "").trim() } }
		: null;
}

function productionPlanValidate(rows, allocations) {
	for (const row of rows) {
		if (!productionPlanSelectable(row)) return "任务状态已经变化，请刷新后重新选择。";
		const error = operationDispatchPlanError(allocations[row.job_card], row.for_quantity);
		if (error) return `${row.operation} · ${row.job_card}：${error}`;
	}
	return "";
}

function createProductionPlanController({ read, save, recheck, render = () => {} }) {
	let revision = 0;
	const state = { rows: [], companies: [], filters: { company: "", operation: "", search: "", saved_only: 0 },
		pagination: { page: 1, total_pages: 1, total_count: 0 }, selected: new Set(),
		results: [], statusFilter: "all", loading: false, running: false, stopped: false, error: "" };
	const publish = () => render(state);
	const visibleRows = () => state.rows.filter((row) => state.statusFilter === "all" || productionPlanCategory(row) === state.statusFilter);
	const selectedRows = () => state.rows.filter((row) => state.selected.has(row.job_card));
	const api = {
		state, selectedRows, visibleRows,
		setStatusFilter(value) {
			if (state.running || state.loading || !["all", "unscheduled", "scheduled", "attention"].includes(value)) return;
			state.statusFilter = value; state.selected.clear(); publish();
		},
		async load({ page = state.pagination.page, filters = state.filters, statusFilter = state.statusFilter } = {}) {
			if (state.running) return false;
			const token = ++revision;
			state.loading = true; state.error = ""; state.selected.clear(); state.filters = { ...filters }; state.statusFilter = statusFilter; publish();
			try {
				const result = await read({ page, page_length: 20, filters });
				if (token !== revision) return false;
				state.rows = result.rows || []; state.companies = result.companies || []; state.pagination = result.pagination;
				for (const row of state.rows) {
					if (state.results.some((result) => result.job_card === row.job_card && result.status === "uncertain")) {
						row.planning = { ...row.planning, can_plan: false, block_message: "上次保存结果尚未核实，请先核实结果。" };
					}
				}
				return true;
			} catch (error) {
				if (token === revision) { state.rows = []; state.error = operationDispatchErrorText(error); }
				return false;
			} finally { if (token === revision) { state.loading = false; publish(); } }
		},
		select(name, checked) {
			if (state.running || state.loading) return;
			if (!checked) state.selected.delete(name);
			else {
				const row = visibleRows().find((item) => item.job_card === name);
				if (!productionPlanSelectable(row)) return;
				if (selectedRows().some((item) => item.company !== row.company)) throw new Error("一次请选择同一公司的任务。");
				state.selected.add(name);
			}
			publish();
		},
		selectPage(checked) {
			if (state.running || state.loading) return;
			state.selected.clear();
			const eligible = visibleRows().filter(productionPlanSelectable);
			const company = state.filters.company || eligible[0]?.company;
			if (checked) eligible.filter((row) => row.company === company).slice(0, 20).forEach((row) => state.selected.add(row.job_card));
			publish();
		},
		deactivate() { revision++; state.loading = false; state.selected.clear(); state.stopped = true; publish(); },
		stop() { state.stopped = true; publish(); },
		async verify(result) {
			try {
				const context = await recheck([result.job_card]);
				const plan = context.cards?.[result.job_card]?.plan;
				if (plan && Number(plan.revision) > Number(result.expected_revision)) {
					result.status = "verified";
					result.message = "服务器已有新安排，请核对当前人员。";
				} else { result.status = "uncertain"; result.message = "暂未确认保存结果，原请求可能仍在处理，请稍后核实。"; }
			} catch (_error) { result.status = "uncertain"; result.message = "暂时无法核实，请稍后重试核实。"; }
			publish();
		},
		async execute(allocations) {
			if (state.loading || state.running) return false;
			const rows = selectedRows();
			if (!rows.length || rows.length > 20) throw new Error("每次请选择 1 至 20 张任务。");
			const error = productionPlanValidate(rows, allocations);
			if (error) throw new Error(error);
			const batch = rows.map((row) => ({ ...row, allocations: allocations[row.job_card].map((entry) => ({ ...entry })),
				expected_revision: Number(row.planning?.plan?.revision || 0), status: "queued", message: "尚未保存" }));
			state.results = [...state.results.filter((row) => row.status === "uncertain"), ...batch];
			state.running = true; state.stopped = false; state.selected.clear(); publish();
			try {
				for (const row of batch) {
					if (state.stopped) { row.status = "skipped"; row.message = "尚未保存"; publish(); continue; }
					row.status = "running"; publish();
					try {
						const plan = await save({ job_card: row.job_card, allocations: row.allocations,
							expected_qty: Number(row.for_quantity), expected_revision: row.expected_revision,
							expected_work_order: row.work_order, expected_operation_id: row.operation_id,
							supervisor: row.planning?.plan?.supervisor || undefined });
						if (!plan?.name || plan.job_card !== row.job_card || plan.status !== "Planned") throw new Error("未收到可确认的保存结果。");
						row.status = "saved"; row.message = "已安排，工人可查看任务；就绪后直接开工。";
					} catch (error) {
						if (operationDispatchUncertainError(error)) {
							await api.verify(row);
							if (row.status === "uncertain") state.stopped = true;
						} else { row.status = "failed"; row.message = operationDispatchErrorText(error); }
					}
					publish();
				}
			} finally { state.running = false; publish(); }
			await api.load();
			return true;
		},
	};
	return api;
}

function productionPlanRowHtml(row, selected, disabled) {
	const esc = operationDispatchEscape;
	const state = productionPlanState(row);
	const context = row.planning || {};
	const category = productionPlanCategory(row);
	const planned = context.plan?.status === "Planned";
	const allocations = context.plan?.status === "Planned" ? context.plan.allocations : row.assignments || [];
	const people = (allocations || []).map((entry) => `${entry.employee_name || entry.employee} ${entry.assigned_qty}`).join("、");
	const reason = context.can_plan ? ({
		MATERIAL_NOT_FULLY_ISSUED: "等待领料出库，人员无需再次安排。",
		DIRECT_MATERIAL_SHORTAGE: "现场物料不足，补料后可开工。",
		DIRECT_PRIORITY_CONFLICT: "物料已优先分配给其他工单，请主管核对。",
		PREVIOUS_OPERATION_PENDING: "前序报工审核后，工人可直接开工。",
	}[context.start_block_code] || context.start_block_message) : context.block_message;
	return `<article class="operation-pool-row production-plan-row ${selected ? "is-selected" : ""}" data-plan-category="${category}">
		<label class="operation-pool-check"><input type="checkbox" data-plan-select="${esc(row.job_card)}" ${selected ? "checked" : ""} ${disabled || !productionPlanSelectable(row) ? "disabled" : ""} aria-label="选择 ${esc(row.operation)} · ${esc(row.item_name || row.production_item)} · ${esc(row.job_card)}"></label>
		<div class="operation-pool-identity production-personnel-product"><strong>${esc(row.item_name || row.production_item)}</strong><span class="production-personnel-task-meta">${esc(row.workstation || "工作站未设置")} · ${row.delivery_date ? `交期 ${esc(row.delivery_date)}` : "交期未设置"}</span>
			<details class="production-personnel-source"><summary>任务来源</summary><div><span>订单 ${esc(row.sales_order || "无销售订单")}</span><span>工单 ${esc(row.work_order)}</span><span>任务 ${esc(row.job_card)}</span><span>产品编码 ${esc(row.production_item)}</span><button type="button" class="btn btn-link" data-plan-history="${esc(row.work_order)}">查看工单派工记录</button></div></details>
		</div>
		<div class="operation-pool-quantity production-personnel-quantity"><small>任务量</small><strong>${esc(row.for_quantity)} <span>${esc(row.stock_uom || "")}</span></strong>${Number(row.completed_qty) > 0 ? `<small>已完成 ${esc(row.completed_qty)}</small>` : ""}</div>
		<div class="operation-pool-state production-personnel-assignment"><span class="indicator-pill ${category === "attention" ? "orange" : state.color}">${category === "attention" ? "需处理" : state.label}</span><strong class="production-personnel-people ${people ? "" : "is-unassigned"}">${people ? esc(people) : category === "unscheduled" ? "尚未选择工人" : "暂无人员安排"}</strong>${reason && (planned || category === "attention") ? `<small class="production-personnel-reason">${esc(reason)}</small>` : ""}
			<div class="production-personnel-row-actions">${productionPlanSelectable(row) ? `<button type="button" class="btn ${planned ? "btn-default" : "btn-primary"}" data-plan-open="${esc(row.job_card)}" ${disabled ? "disabled" : ""}>${planned ? "修改安排" : "安排人员"}</button>` : ""}${planned ? `<button type="button" class="btn btn-link production-personnel-cancel" data-plan-cancel="${esc(row.job_card)}" ${disabled ? "disabled" : ""}>撤销安排</button>` : ""}</div>
		</div>
	</article>`;
}

function mountProductionPrearrangement({ host, openAssignment }) {
	const $host = $(host).addClass("operation-dispatch-pool production-prearrangement");
	const esc = operationDispatchEscape;
	const call = async (method, args = {}, write = false) => {
		const response = await operationDispatchBounded(frappe.call({ method, args, type: write ? "POST" : "GET", silent: true }));
		return response.message;
	};
	let active = false;
	let dialog = null;
	let loadingEditor = false;
	let renderedFilterKey = null;
	$host.html(`<header class="production-personnel-header"><div><h3>安排人员</h3><p>先把各道工序安排好，料到、前序完成后，工人直接开工。</p></div><button type="button" class="btn btn-default" data-plan-refresh>刷新任务</button></header>
		<div class="production-personnel-search"><label><span class="sr-only">搜索订单、产品或工单</span><input class="form-control" data-plan-filter="search" placeholder="搜索订单、产品或工单"></label><button type="button" class="btn btn-default" data-plan-search>搜索</button></div>
		<details class="production-personnel-filters"><summary>筛选范围 <span data-plan-filter-summary></span></summary><div class="operation-pool-filters"><label>公司<select class="form-control" data-plan-filter="company"><option value="">全部公司</option></select></label><label>工序<input class="form-control" data-plan-filter="operation" placeholder="完整工序名称"></label><label class="production-personnel-saved-filter"><input type="checkbox" data-plan-filter="saved_only"> 仅看已保存安排（含已停工）</label><button type="button" class="btn btn-default" data-plan-search>应用筛选</button><button type="button" class="btn btn-link" data-plan-clear-filters>清除筛选</button></div></details>
		<div class="production-personnel-page-states" aria-label="本页任务状态"></div>
		<div class="operation-pool-toolbar production-personnel-selection"><div><label><input type="checkbox" data-plan-all> 全选当前显示的同公司任务</label><span data-plan-count role="status"></span></div><button type="button" class="btn btn-primary" data-plan-arrange>批量安排人员</button><button type="button" class="btn btn-default" data-plan-stop hidden>停止后续保存</button></div>
		<div class="production-plan-feedback" role="status"></div><div class="production-personnel-list"></div><div class="operation-pool-pagination"></div><div class="production-plan-results" aria-live="polite"></div>`);
	const controller = createProductionPlanController({
		read: (args) => call(PRODUCTION_PLAN_POOL_API, args),
		save: (args) => call(PRODUCTION_PLAN_API + "save_plan", args, true),
		recheck: (job_cards) => call(PRODUCTION_PLAN_API + "get_plan_context", { job_cards }),
		render(state) {
			const busy = state.loading || state.running;
			const filterKey = JSON.stringify([state.filters, state.companies]);
			if (filterKey !== renderedFilterKey) {
				$host.find("[data-plan-filter=company]").html('<option value="">全部公司</option>' + state.companies.map((company) => `<option value="${esc(company)}">${esc(company)}</option>`).join("")).val(state.filters.company);
				$host.find("[data-plan-filter=search]").val(state.filters.search || "");
				$host.find("[data-plan-filter=operation]").val(state.filters.operation || "");
				$host.find("[data-plan-filter=saved_only]").prop("checked", Boolean(Number(state.filters.saved_only)));
				renderedFilterKey = filterKey;
			}
			const filterLabels = [state.filters.company, state.filters.operation, Number(state.filters.saved_only) ? "已保存安排" : ""].filter(Boolean);
			$host.find("[data-plan-filter-summary]").text(filterLabels.length ? `· ${filterLabels.join(" / ")}` : "· 全部公司与工序");
			const visible = controller.visibleRows();
			const counts = productionPlanPageCounts(state.rows);
			const labels = { all: "全部", unscheduled: "未安排", scheduled: "已安排", attention: "需处理" };
			$host.find(".production-personnel-page-states").html(`<span class="production-personnel-page-label">本页状态</span>${Object.entries(labels).map(([value, label]) => `<button type="button" class="production-personnel-state ${value === state.statusFilter ? "is-active" : ""}" data-plan-status="${value}" aria-pressed="${value === state.statusFilter}" ${busy ? "disabled" : ""}>${label}<span>${counts[value]}</span></button>`).join("")}`);
			const chosen = controller.selectedRows();
			$host.find("[data-plan-count]").text(chosen.length ? `已选 ${chosen.length} 项任务 · ${new Set(chosen.map((row) => row.operation)).size} 道工序` : "选择任务后，可一次安排多道工序");
			$host.find("[data-plan-arrange]").prop("disabled", busy || !state.selected.size).text(state.selected.size ? `安排所选 ${state.selected.size} 项任务` : "批量安排人员");
			$host.find("[data-plan-filter], [data-plan-search], [data-plan-clear-filters], [data-plan-refresh], [data-plan-all]").prop("disabled", busy);
			const selectable = visible.filter(productionPlanSelectable);
			$host.find("[data-plan-all]").prop("checked", state.selected.size > 0 && selectable.every((row) => state.selected.has(row.job_card))).prop("indeterminate", state.selected.size > 0 && !selectable.every((row) => state.selected.has(row.job_card))).prop("disabled", busy || !selectable.length);
			$host.find("[data-plan-stop]").prop("hidden", !state.running);
			$host.find(".production-plan-feedback").text(state.loading ? "正在加载…" : state.error);
			const groups = new Map();
			for (const row of visible) { if (!groups.has(row.operation)) groups.set(row.operation, []); groups.get(row.operation).push(row); }
			$host.find(".production-personnel-list").html([...groups].map(([operation, rows]) => `<section class="production-personnel-operation"><h4>${esc(operation)} <small>${rows.length} 项任务${rows.some((row) => productionPlanCategory(row) === "unscheduled") ? ` · ${rows.filter((row) => productionPlanCategory(row) === "unscheduled").length} 项未安排` : ""}</small></h4>${rows.map((row) => productionPlanRowHtml(row, state.selected.has(row.job_card), busy)).join("")}</section>`).join("") || (!state.loading ? `<div class="production-personnel-empty"><strong>${state.rows.length ? "本页没有这类任务" : "当前没有符合条件的任务"}</strong><p>${state.rows.length ? "可切换本页状态，或查看下一页。" : "可调整搜索或筛选范围；新订单生成生产任务后会显示在这里。"}</p></div>` : ""));
			const page = state.pagination;
			$host.find(".operation-pool-pagination").html(`<button class="btn btn-default" data-plan-page="${Number(page.page) - 1}" ${busy || page.page <= 1 ? "disabled" : ""}>上一页</button><span>第 ${page.page || 1} / ${page.total_pages || 1} 页 · 共 ${page.total_count || 0} 张</span><button class="btn btn-default" data-plan-page="${Number(page.page) + 1}" ${busy || !page.has_more ? "disabled" : ""}>下一页</button>`);
			$host.find(".production-plan-results").html(state.results.length ? `<details class="production-personnel-results" ${state.results.some((row) => ["uncertain", "failed", "running"].includes(row.status)) ? "open" : ""}><summary>安排结果 <span>${state.results.filter((row) => row.status === "saved").length} 项已保存 · 共 ${state.results.length} 项</span></summary>${state.results.map((row) => `<div class="operation-pool-result"><strong>${esc(row.operation)} · ${esc(row.job_card)}</strong><span>${esc(row.message)}${row.status === "uncertain" ? ` <button type="button" class="btn btn-default" data-plan-verify="${esc(row.job_card)}">核实保存结果</button>` : ""}</span></div>`).join("")}</details>` : "");
			$host.find("[data-plan-open], [data-plan-cancel], [data-plan-history], [data-plan-verify]").prop("disabled", busy);
		},
	});
	const refresh = (options = {}) => {
		if (controller.state.running || loadingEditor || dialog || (options.background && controller.state.selected.size)) return Promise.resolve(false);
		return controller.load();
	};
	const onError = (error) => frappe.msgprint(esc(operationDispatchErrorText(error)));
	$host.on("change", "[data-plan-select]", (event) => { try { controller.select(event.currentTarget.dataset.planSelect, event.currentTarget.checked); } catch (error) { event.currentTarget.checked = false; onError(error); } });
	$host.on("change", "[data-plan-all]", (event) => controller.selectPage(event.currentTarget.checked));
	$host.on("click", "[data-plan-refresh]", () => refresh());
	const search = () => {
		const filters = {}; $host.find("[data-plan-filter]").each((_, field) => { filters[field.dataset.planFilter] = field.type === "checkbox" ? Number(field.checked) : $(field).val(); });
		controller.load({ filters, page: 1 });
	};
	$host.on("click", "[data-plan-search]", search);
	$host.on("keydown", "[data-plan-filter=search], [data-plan-filter=operation]", (event) => { if (event.key === "Enter") { event.preventDefault(); search(); } });
	$host.on("click", "[data-plan-clear-filters]", () => {
		controller.setStatusFilter("all");
		controller.load({ page: 1, filters: { company: "", operation: "", search: "", saved_only: 0 } });
	});
	$host.on("click", "[data-plan-status]", (event) => controller.setStatusFilter(event.currentTarget.dataset.planStatus));
	$host.on("click", "[data-plan-page]", (event) => controller.load({ page: Number(event.currentTarget.dataset.planPage) }));
	$host.on("click", "[data-plan-stop]", () => controller.stop());
	$host.on("click", "[data-plan-history]", (event) => openAssignment(event.currentTarget.dataset.planHistory, refresh));
	$host.on("click", "[data-plan-verify]", async (event) => { const result = controller.state.results.find((row) => row.job_card === event.currentTarget.dataset.planVerify); if (result) { await controller.verify(result); await refresh(); } });
	$host.on("click", "[data-plan-cancel]", async (event) => {
		const row = controller.state.rows.find((item) => item.job_card === event.currentTarget.dataset.planCancel);
		if (!row?.planning?.plan || controller.state.running) return;
		const button = $(event.currentTarget).prop("disabled", true);
		try { await call(PRODUCTION_PLAN_API + "cancel_plan", { job_card: row.job_card, expected_revision: row.planning.plan.revision }, true); await refresh(); }
		catch (error) { onError(error); button.prop("disabled", false); }
	});
	const arrangeSelected = async () => {
		if (loadingEditor || dialog || controller.state.running) return;
		const selected = controller.selectedRows();
		if (!selected.length) return;
		loadingEditor = true;
		try {
			const current = await call(PRODUCTION_PLAN_API + "get_plan_context", { job_cards: selected.map((row) => row.job_card) });
			if (!active) return;
			const rows = selected.map((row) => ({ ...row, planning: current.cards[row.job_card] || {} }));
			if (rows.some((row) => !productionPlanSelectable(row))) { await controller.load(); throw new Error("部分任务状态已变化，请重新选择。"); }
			// Keep the save revision aligned with the fresh preview, never with the stale list.
			for (const row of rows) Object.assign(controller.state.rows.find((item) => item.job_card === row.job_card), row);
			openPlanDialog(rows);
		} catch (error) { onError(error); } finally { loadingEditor = false; }
	};
	$host.on("click", "[data-plan-arrange]", arrangeSelected);
	$host.on("click", "[data-plan-open]", async (event) => {
		if (loadingEditor || dialog || controller.state.loading || controller.state.running) return;
		controller.selectPage(false);
		controller.select(event.currentTarget.dataset.planOpen, true);
		await arrangeSelected();
	});
	function openPlanDialog(rows) {
		const allocations = Object.fromEntries(rows.map((row) => [row.job_card, productionPlanInitialAllocations(row)]));
		const key = operationDispatchSelectionKey(rows);
		const operations = [...new Set(rows.map((row) => row.operation))];
		const preview = () => {
			if (!dialog) return;
			dialog.fields_dict.preview.$wrapper.html(`<p>共 ${rows.length} 张任务。保存后工人即可看到安排，开工仍按实际就绪情况控制。</p>
				<div class="production-plan-shortcuts"><button class="btn btn-default btn-sm" data-plan-people="all">全部使用同一名工人</button>${operations.map((operation, index) => `<button class="btn btn-default btn-sm" data-plan-people="${index}">${esc(operation)}：批量选人</button><button class="btn btn-link btn-sm" data-plan-default="${index}">设置常用人员</button>`).join("")}</div>
				${rows.map((row) => `<article class="operation-pool-preview-row"><strong>${esc(row.operation)} · ${esc(row.item_name || row.production_item)}</strong><small>订单 ${esc(row.sales_order || "无")} · ${esc(row.work_order)} · ${esc(row.job_card)}</small><span>任务量 ${esc(row.for_quantity)} ${esc(row.stock_uom)}</span><span>${(allocations[row.job_card] || []).map((entry) => `${esc(entry.employee_name || entry.employee)}：${esc(entry.assigned_qty)} ${esc(row.stock_uom)}`).join("；") || '<strong class="text-warning">待选择工人</strong>'}</span><button class="btn btn-default btn-xs" data-plan-edit="${esc(row.job_card)}">调整人员与数量</button></article>`).join("")}`);
		};
		dialog = new frappe.ui.Dialog({ title: "安排生产人员", size: "extra-large", fields: [{ fieldname: "preview", fieldtype: "HTML" }],
			primary_action_label: `确认安排 ${rows.length} 张任务`, primary_action() {
				if (!active || key !== operationDispatchSelectionKey(controller.selectedRows())) { dialog.hide(); onError(new Error("选择范围已变化，请重新选择。")); return; }
				const error = productionPlanValidate(rows, allocations);
				if (error) { frappe.msgprint(esc(error)); return; }
				dialog.hide(); controller.execute(allocations).catch(onError);
			} });
		dialog.$wrapper.addClass("production-personnel-dialog");
		dialog.$wrapper.on("hidden.bs.modal.production-plan", () => { dialog = null; });
		const $preview = dialog.fields_dict.preview.$wrapper;
		const workerQuery = (row) => ({ query: "process_simplification.api.production_reporting.search_workers", filters: { job_card: row.job_card } });
		$preview.on("click", "[data-plan-people]", (event) => {
			const value = event.currentTarget.dataset.planPeople;
			const targets = value === "all" ? rows : rows.filter((row) => row.operation === operations[Number(value)]);
			const picker = new frappe.ui.Dialog({ title: value === "all" ? "为全部任务选择工人" : targets[0].operation, fields: [
				{ fieldname: "employee", fieldtype: "Link", options: "Employee", label: "工人", reqd: 1, get_query: () => workerQuery(targets[0]) },
			], primary_action_label: "应用到本次预览", primary_action(values) {
				for (const row of targets) allocations[row.job_card] = [{ employee: values.employee, assigned_qty: Number(row.for_quantity), notes: "" }];
				picker.hide(); preview();
			} }); picker.$wrapper.addClass("production-personnel-dialog"); picker.show();
		});
		$preview.on("click", "[data-plan-edit]", (event) => {
			const row = rows.find((item) => item.job_card === event.currentTarget.dataset.planEdit);
			const editor = new frappe.ui.Dialog({ title: `${row.operation} · 分配 ${row.for_quantity} ${row.stock_uom}`, size: "large", fields: [
				{ fieldname: "allocations", fieldtype: "Table", label: "人员与数量", in_place_edit: true, data: allocations[row.job_card].map((entry) => ({ ...entry })), fields: [
					{ fieldname: "employee", fieldtype: "Link", options: "Employee", label: "工人", in_list_view: 1, columns: 4, reqd: 1, get_query: () => workerQuery(row) },
					{ fieldname: "assigned_qty", fieldtype: "Float", label: "数量", in_list_view: 1, columns: 2, reqd: 1 },
					{ fieldname: "notes", fieldtype: "Data", label: "备注", in_list_view: 1, columns: 4 },
				] } ], primary_action_label: "应用到本次预览", primary_action(values) {
				const plan = (values.allocations || []).filter((entry) => entry.employee || entry.assigned_qty).map((entry) => ({ employee: entry.employee, assigned_qty: Number(entry.assigned_qty), notes: entry.notes || "" }));
				const error = operationDispatchPlanError(plan, row.for_quantity); if (error) { frappe.msgprint(esc(error)); return; }
				allocations[row.job_card] = plan; editor.hide(); preview();
			} }); editor.$wrapper.addClass("production-personnel-dialog"); editor.show();
		});
		$preview.on("click", "[data-plan-default]", (event) => {
			const operation = operations[Number(event.currentTarget.dataset.planDefault)];
			const row = rows.find((item) => item.operation === operation);
			const roster = row.planning.default_roster;
			const editor = new frappe.ui.Dialog({ title: `${operation} · 常用人员`, size: "large", fields: [
				{ fieldname: "workers", fieldtype: "Table", label: "默认带出的人员", in_place_edit: true, data: (roster?.workers || []).map((entry) => ({ ...entry })),
					description: "一人时自动带入整张任务量；多人时请在安排预览中分配数量。保存只影响之后的新安排。清空名单可取消默认设置。",
					fields: [{ fieldname: "employee", fieldtype: "Link", options: "Employee", label: "工人", in_list_view: 1, columns: 8, reqd: 1, get_query: () => workerQuery(row) }] },
			], primary_action_label: "保存常用人员", async primary_action(values) {
				editor.get_primary_btn().prop("disabled", true);
				try {
					const saved = await call(PRODUCTION_PLAN_API + "save_worker_defaults", { company: row.company, operation,
						workers: (values.workers || []).filter((entry) => entry.employee).map((entry) => entry.employee), expected_revision: roster?.revision || 0, supervisor: roster?.supervisor || row.planning.plan?.supervisor || undefined }, true);
					for (const target of rows.filter((item) => item.operation === operation && (item.planning.plan?.supervisor || frappe.session.user) === (row.planning.plan?.supervisor || frappe.session.user))) {
						target.planning.default_roster = saved;
						if (!allocations[target.job_card].length) allocations[target.job_card] = productionPlanInitialAllocations(target);
					}
					editor.hide(); preview();
				} catch (error) { onError(error); editor.get_primary_btn().prop("disabled", false); }
			} }); editor.$wrapper.addClass("production-personnel-dialog"); editor.show();
		});
		preview(); dialog.show();
	}
	const deactivate = () => { active = false; dialog?.hide(); controller.deactivate(); };
	frappe.router?.on("change", () => { if (active && frappe.get_route()[0] !== "production-workbench") deactivate(); });
	return { controller, activate(options = {}) {
		active = true;
		const request = productionPlanActivationRequest(controller.state, options);
		if (!request) return refresh();
		if (controller.state.running || loadingEditor || dialog) return Promise.resolve(false);
		return controller.load(request);
	}, deactivate, refresh };
}

const productionPrearrangementApi = { productionPlanSelectable, productionPlanInitialAllocations, productionPlanState,
	productionPlanCategory, productionPlanPageCounts, productionPlanActivationRequest,
	productionPlanValidate, createProductionPlanController, productionPlanRowHtml, mountProductionPrearrangement };
if (typeof module !== "undefined" && module.exports) module.exports = productionPrearrangementApi;
if (typeof window !== "undefined") {
	window.process_simplification = window.process_simplification || {};
	window.process_simplification.mount_production_prearrangement = mountProductionPrearrangement;
}
