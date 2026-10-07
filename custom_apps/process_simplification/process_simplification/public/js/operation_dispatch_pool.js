/* A read view and first-dispatch client. Work reports and stock actions stay in their original flows. */
const OPERATION_DISPATCH_LIMIT = 20;
const OPERATION_DISPATCH_API = "process_simplification.api.production_reporting.";

function operationDispatchEscape(value) {
	return String(value ?? "").replace(/[&<>"']/g, (character) => ({
		"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
	})[character]);
}

function operationDispatchSelectable(row) {
	return Boolean(row?.job_card && row.can_assign_first && !row.has_assignment_history
		&& !row.has_work_history && !row.has_movement_history
		&& !(row.assignments || []).length && Number.isFinite(Number(row.for_quantity))
		&& Number(row.for_quantity) > 0);
}

function operationDispatchSelectionKey(rows) {
	return JSON.stringify(rows.map((row) => [row.job_card, row.work_order, row.operation_id, row.company, Number(row.for_quantity)]));
}

function operationDispatchPlainDescription(value) {
	return String(value || "").replace(/<[^>]*>/g, " ").replace(/&nbsp;/gi, " ").replace(/\s+/g, " ").trim();
}

function operationDispatchPlanError(plan, quantity) {
	if (!Array.isArray(plan) || !plan.length) return "请至少选择一名工人。";
	const employees = new Set();
	let total = 0;
	for (const allocation of plan) {
		if (!String(allocation.employee || "").trim()) return "每行必须选择工人。";
		if (employees.has(allocation.employee)) return "同一名工人不能重复添加。";
		employees.add(allocation.employee);
		const qty = Number(allocation.assigned_qty);
		if (!Number.isFinite(qty) || qty <= 0) return "每名工人的数量必须大于 0。";
		total += qty;
	}
	return Math.abs(total - Number(quantity)) > 1e-6 ? "逐人的派工数量合计必须等于这张任务的任务量。" : "";
}

function operationDispatchErrorText(error) {
	const data = error?.responseJSON || error || {};
	try {
		const messages = typeof data._server_messages === "string" ? JSON.parse(data._server_messages) : data._server_messages;
		if (messages?.length) {
			const entry = typeof messages[0] === "string" ? JSON.parse(messages[0]) : messages[0];
			if (entry.message) return String(entry.message);
		}
	} catch (_error) { /* Fall back to a plain error string. */ }
	return String(data.message || error?.statusText || "操作失败，请打开原派工入口核对。");
}

function operationDispatchUncertainError(error) {
	const status = Number(error?.status);
	return error?.statusText === "timeout" || error?.name === "TimeoutError"
		|| !Number.isFinite(status) || status === 0 || status >= 500;
}

function operationDispatchBounded(promise, timeout = 60000, { schedule = setTimeout, cancel = clearTimeout } = {}) {
	// Some Frappe versions do not forward frappe.call's timeout to the XHR.
	// This bounds the UI wait only; the server may still commit after we time out.
	return new Promise((resolve, reject) => {
		const timer = schedule(() => {
			const error = new Error("请求等待超时，服务器仍可能继续处理。");
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

function operationDispatchReadback(row, context) {
	const card = (context?.job_cards || []).find((item) => item.name === row.job_card);
	if (card && (card.assignments || []).length) {
		return { status: "exists", message: "服务器已有派工，请核对人员；无法确认是否来自本次请求。" };
	}
	return { status: "uncertain", message: "暂未确认结果，原请求可能仍在处理。请稍后核实，不要立即重复提交。" };
}

function createOperationDispatchController({ read, assign, recheck, render = () => {} }) {
	let revision = 0;
	const state = {
		rows: [], companies: [], filters: { company: "", operation: "", search: "", assignment_state: "all" },
		pagination: { page: 1, page_length: OPERATION_DISPATCH_LIMIT, total_count: 0, total_pages: 1 },
		selected: new Set(), results: [], loading: false, running: false, stopped: false, error: "",
	};
	const publish = () => render(state);
	const selectedRows = () => state.rows.filter((row) => state.selected.has(row.job_card));
	const api = {
		state, selectedRows,
		async load({ filters = state.filters, page = state.pagination.page } = {}) {
			if (state.running) return false;
			const token = ++revision;
			state.filters = { ...filters };
			state.selected.clear();
			state.loading = true;
			state.error = "";
			publish();
			try {
				const response = await read({ filters: { ...filters }, page, page_length: OPERATION_DISPATCH_LIMIT });
				if (token !== revision) return false;
				state.rows = (response.rows || []).map((row) => {
					const unresolved = state.results.find((result) => result.job_card === row.job_card && ["uncertain", "checking"].includes(result.status));
					return unresolved ? { ...row, can_assign_first: false, block_message: "上次请求的结果尚未核实，请先核对原派工记录。" } : row;
				});
				state.companies = response.companies || [];
				state.pagination = response.pagination || { ...state.pagination, page };
				return true;
			} catch (error) {
				if (token !== revision) return false;
				state.rows = [];
				state.error = operationDispatchErrorText(error);
				return false;
			} finally {
				if (token === revision) { state.loading = false; publish(); }
			}
		},
		select(jobCard, checked) {
			if (state.loading || state.running) return "";
			const row = state.rows.find((item) => item.job_card === jobCard);
			if (!checked) { state.selected.delete(jobCard); publish(); return ""; }
			if (!operationDispatchSelectable(row)) return "这张任务当前不能首次派工。";
			if (state.selected.has(jobCard)) return "";
			if (state.selected.size >= OPERATION_DISPATCH_LIMIT) return "每批最多选择 20 张任务。";
			if (selectedRows().some((item) => item.company !== row.company)) return "一次请选择同一公司的任务。";
			state.selected.add(jobCard);
			publish();
			return "";
		},
		selectPage(checked) {
			if (state.loading || state.running) return;
			state.selected.clear();
			if (checked) {
				const eligible = state.rows.filter(operationDispatchSelectable);
				const company = state.filters.company || eligible[0]?.company;
				for (const row of eligible.filter((item) => item.company === company).slice(0, OPERATION_DISPATCH_LIMIT)) {
					state.selected.add(row.job_card);
				}
			}
			publish();
		},
		stop() { state.stopped = true; publish(); },
		invalidate() {
			if (state.running) return;
			revision++;
			state.selected.clear();
			state.loading = true;
			publish();
		},
		deactivate() {
			revision++;
			state.loading = false;
			state.selected.clear();
			if (state.running) state.stopped = true;
			publish();
		},
		async execute(plans) {
			if (state.running || state.loading) return false;
			const rows = selectedRows();
			if (!rows.length || rows.length > OPERATION_DISPATCH_LIMIT) throw new Error("请重新选择当前页的任务。");
			const batch = rows.map((row) => {
				if (!operationDispatchSelectable(row)) throw new Error("任务状态已经变化，请刷新后重新选择。");
				const allocations = (plans[row.job_card] || []).map((entry) => ({
					employee: String(entry.employee || "").trim(), assigned_qty: Number(entry.assigned_qty), notes: String(entry.notes || ""),
				}));
				const error = operationDispatchPlanError(allocations, row.for_quantity);
				if (error) throw new Error(error);
				return { ...row, allocations, status: "queued", message: "" };
			});
			const unresolved = state.results.filter((row) => ["uncertain", "checking", "exists"].includes(row.status));
			state.results = [...unresolved, ...batch];
			state.running = true;
			state.stopped = false;
			state.selected.clear();
			publish();
			try {
				for (const row of batch) {
					if (state.stopped) { row.status = "skipped"; row.message = "已停止，尚未提交。"; publish(); continue; }
					row.status = "running";
					publish();
					try {
						const result = await assign({
							job_card: row.job_card, assignments: row.allocations, expected_qty: Number(row.for_quantity),
							expected_work_order: row.work_order, expected_operation_id: row.operation_id,
							supervisor: row.assignment_supervisor || undefined,
						});
						if (result?.status !== "assigned" || result.job_card !== row.job_card) throw new Error("服务器未返回可确认的派工结果。");
						row.status = "assigned";
						row.message = "首次派工已完成。";
						const assignedState = {
							can_assign_first: false, has_assignment_history: true,
							assignments: Array.isArray(result.assignments) ? result.assignments : [],
							block_code: "EXISTING_HISTORY",
							block_message: "已完成派工，请通过原派工入口查看或调整。",
						};
						Object.assign(row, assignedState);
						const assignedSource = state.rows.find((item) => item.job_card === row.job_card);
						if (assignedSource) Object.assign(assignedSource, assignedState);
					} catch (error) {
						if (operationDispatchUncertainError(error)) {
							row.status = "checking";
							row.message = "请求结果不明，正在读取服务器记录…";
							publish();
							try { Object.assign(row, operationDispatchReadback(row, await recheck(row.work_order))); }
							catch (_error) { row.status = "uncertain"; row.message = "结果读取失败，请稍后核实原派工记录。"; }
							// An unknown in-flight reservation can change the next card's material boundary.
							if (row.status === "uncertain") state.stopped = true;
						} else {
							row.status = "failed";
							row.message = operationDispatchErrorText(error);
						}
					}
					row.can_assign_first = false;
					const source = state.rows.find((item) => item.job_card === row.job_card);
					if (source) source.can_assign_first = false;
					publish();
				}
			} finally { state.running = false; publish(); }
			return true;
		},
		async recheckResult(jobCard) {
			if (state.running) return;
			const row = state.results.find((item) => item.job_card === jobCard);
			if (!row || !["uncertain", "exists"].includes(row.status)) return;
			row.status = "checking";
			publish();
			try { Object.assign(row, operationDispatchReadback(row, await recheck(row.work_order))); }
			catch (_error) { row.status = "uncertain"; row.message = "仍无法核实，请打开原派工记录。"; }
			publish();
		},
	};
	return api;
}

function operationDispatchRowHtml(row, { selected = false, disabled = false } = {}) {
	const esc = operationDispatchEscape;
	const link = (doctype, name) => name ? `<a href="/app/${doctype}/${encodeURIComponent(name)}">${esc(name)}</a>` : "—";
	const number = (value) => Number(value || 0).toLocaleString("zh-CN", { maximumFractionDigits: 6 });
	const people = (row.assignments || []).map((entry) => entry.employee_name || entry.employee).filter(Boolean).join("、");
	return `<article class="operation-pool-row${operationDispatchSelectable(row) ? " is-ready" : ""}">
		<label class="operation-pool-check"><input type="checkbox" data-pool-select="${esc(row.job_card)}" ${selected ? "checked" : ""} ${disabled || !operationDispatchSelectable(row) ? "disabled" : ""} aria-label="选择 ${esc(row.job_card)}"></label>
		<div class="operation-pool-identity"><strong>${esc(row.item_name || row.production_item)}</strong><small>${esc(row.production_item)}</small>${row.description ? `<p class="operation-pool-spec">${esc(operationDispatchPlainDescription(row.description))}</p>` : ""}<small>${esc(row.company)} · ${esc(row.workstation || "未设置工作站")}</small></div>
		<div class="operation-pool-source"><span>订单 ${link("sales-order", row.sales_order)}</span><span>计划 ${link("production-plan", row.production_plan)}</span><span>工单 ${link("work-order", row.work_order)}</span><small>${esc(row.job_card)} · 交期 ${esc(row.delivery_date || "未设置")}</small></div>
		<div class="operation-pool-quantity"><strong>${number(row.for_quantity)} ${esc(row.stock_uom)}</strong><span>任务量</span><small>已完成 ${number(row.completed_qty)} · 剩余 ${number(row.remaining_qty)}</small></div>
		<div class="operation-pool-state"><span class="indicator-pill ${operationDispatchSelectable(row) ? "green" : "gray"}">${operationDispatchSelectable(row) ? "可首次派工" : row.has_assignment_history ? "已有派工历史" : "当前不可派工"}</span>${people ? `<small>${esc(people)}</small>` : ""}${row.block_message ? `<p>${esc(row.block_message)}</p>` : ""}<button type="button" class="btn btn-default btn-xs" data-pool-history="${esc(row.work_order)}">原派工入口</button></div>
	</article>`;
}

function operationDispatchResultsHtml(results) {
	const labels = { queued: "尚未处理", running: "正在提交", checking: "正在核实", assigned: "派工成功", failed: "未能派工", exists: "已有记录，待核对", uncertain: "结果待核实", skipped: "尚未处理" };
	const esc = operationDispatchEscape;
	return `<h4>派工结果与待核实任务</h4><p class="text-muted">保留本次结果和此前待核实的记录。停止只影响尚未提交的任务。</p>${results.map((row) => `<div class="operation-pool-result" data-result-status="${esc(row.status)}"><div><strong>${esc(row.job_card)}</strong> · ${esc(row.operation)}<small>${esc(row.item_name || row.production_item)} · ${esc(row.work_order)}</small></div><div><strong>${labels[row.status] || "待核实"}</strong><p>${esc(row.message)}</p>${["uncertain", "exists"].includes(row.status) ? `<button type="button" class="btn btn-default btn-xs" data-pool-recheck="${esc(row.job_card)}">重新核实</button>` : ""} <button type="button" class="btn btn-default btn-xs" data-pool-history="${esc(row.work_order)}">查看原派工</button></div></div>`).join("")}`;
}

function operationDispatchPreviewHtml(rows, plans, editable = false) {
	const esc = operationDispatchEscape;
	return `<p>本次共 ${rows.length} 张任务，逐张派工；请核对来源、单位与每人数量。</p>${rows.map((row) => `<article class="operation-pool-preview-row"><strong>${esc(row.operation)} · ${esc(row.item_name || row.production_item)}</strong><small>${esc(row.company)} · ${esc(row.stock_uom)} · ${esc(row.job_card)}</small><span>订单 ${esc(row.sales_order || "无")} · 工单 ${esc(row.work_order)}</span><span>任务量 ${esc(row.for_quantity)} ${esc(row.stock_uom)} · 审核 ${esc(row.assignment_supervisor || "当前主管")}</span>${(plans[row.job_card] || []).map((entry) => `<span>${esc(entry.employee)}：${esc(entry.assigned_qty)} ${esc(row.stock_uom)}${entry.notes ? ` · ${esc(entry.notes)}` : ""}</span>`).join("")}${editable ? `<button type="button" class="btn btn-default btn-xs" data-pool-edit="${esc(row.job_card)}">调整这张任务的人数和数量</button>` : ""}</article>`).join("")}`;
}

function mountOperationDispatchPool({ host, openAssignment }) {
	const $host = $(host).addClass("operation-dispatch-pool");
	const esc = operationDispatchEscape;
	let active = false;
	let searchTimer;
	let planDialog = null;
	let planningOpen = false;
	$host.html(`<p class="text-muted">按工序集中查看已计划的任务。只有满足原派工条件且没有保留的派工或生产记录的任务可批量选择。</p>
		<div class="operation-pool-filters"><label>公司<select class="form-control" data-pool-filter="company"><option value="">全部公司</option></select></label><label>工序<input class="form-control" data-pool-filter="operation" placeholder="完整工序名称"></label><label>搜索<input class="form-control" data-pool-filter="search" placeholder="订单、工单、产品或任务"></label><label>派工情况<select class="form-control" data-pool-filter="assignment_state"><option value="all">全部</option><option value="unassigned">未派工</option><option value="assigned">已有派工</option></select></label><button type="button" class="btn btn-default" data-pool-refresh>刷新</button></div>
		<div class="operation-pool-toolbar"><label><input type="checkbox" data-pool-select-page> 选择当前页可派任务（同一公司，最多 20 张）</label><span class="operation-pool-selected" role="status"></span><button type="button" class="btn btn-primary" data-pool-plan>选择人员并预览</button><button type="button" class="btn btn-default" data-pool-stop hidden>停止后续任务</button></div>
		<div class="operation-pool-notice" role="status" aria-live="polite"></div><div class="operation-pool-list"></div><div class="operation-pool-pagination"></div><section class="operation-pool-results" aria-live="polite"></section>`);
	const call = async (method, args) => (await operationDispatchBounded(frappe.call({ method: OPERATION_DISPATCH_API + method, args, type: "POST", silent: true }))).message;
	const controller = createOperationDispatchController({
		read: (args) => call("get_operation_dispatch_pool", { ...args, filters: JSON.stringify(args.filters) }),
		assign: (args) => call("assign_workers_first", { ...args, assignments: JSON.stringify(args.assignments) }),
		recheck: (work_order) => call("get_work_order_assignment_context", { work_order }),
		render(state) {
			const blocked = state.loading || state.running;
			$host.find("[data-pool-filter], [data-pool-refresh]").prop("disabled", state.running);
			const $company = $host.find('[data-pool-filter="company"]');
			$company.html('<option value="">全部公司</option>' + state.companies.map((company) => {
				const value = typeof company === "string" ? company : company.name || company.value;
				return `<option value="${esc(value)}">${esc(typeof company === "string" ? company : company.label || value)}</option>`;
			}).join("")).val(state.filters.company);
			$host.find("[data-pool-select-page]").prop("disabled", blocked).prop("checked", state.selected.size > 0);
			$host.find("[data-pool-plan]").prop("disabled", blocked || !state.selected.size);
			$host.find("[data-pool-stop]").prop("hidden", !state.running).prop("disabled", state.stopped);
			$host.find(".operation-pool-selected").text(`已选 ${state.selected.size} 张`);
			$host.find(".operation-pool-notice").text(state.loading ? "正在读取任务…" : state.error || (state.running ? state.stopped ? "正在完成或核实当前请求，后续任务已停止。" : "正在逐张派工…" : ""));
			const groups = new Map();
			for (const row of state.rows) {
				const operation = row.operation || "未设置工序";
				if (!groups.has(operation)) groups.set(operation, []);
				groups.get(operation).push(row);
			}
			$host.find(".operation-pool-list").html([...groups].map(([operation, rows]) => `<section><h4>${esc(operation)} <small>${rows.length} 张任务</small></h4>${rows.map((row) => operationDispatchRowHtml(row, { selected: state.selected.has(row.job_card), disabled: blocked })).join("")}</section>`).join("") || (state.loading ? "" : '<p class="text-muted">没有符合当前筛选的任务。</p>'));
			const page = state.pagination;
			$host.find(".operation-pool-pagination").html(`<button type="button" class="btn btn-default" data-pool-page="${Math.max(1, Number(page.page) - 1)}" ${blocked || Number(page.page) <= 1 ? "disabled" : ""}>上一页</button><span>第 ${Number(page.page) || 1} / ${Number(page.total_pages) || 1} 页 · 共 ${Number(page.total_count) || 0} 张</span><button type="button" class="btn btn-default" data-pool-page="${Number(page.page) + 1}" ${blocked || !page.has_more ? "disabled" : ""}>下一页</button>`);
			$host.find(".operation-pool-results").html(state.results.length ? operationDispatchResultsHtml(state.results) : "");
			$host.find("[data-pool-recheck], [data-pool-history]").prop("disabled", state.running);
		},
	});
	function refresh(options = {}) {
		if (controller.state.running || (options.background && (controller.state.selected.size || planningOpen))) return Promise.resolve(false);
		active = true;
		planDialog?.hide();
		return controller.load();
	}
	function readFilters() {
		const filters = {};
		$host.find("[data-pool-filter]").each((_, element) => { filters[element.dataset.poolFilter] = $(element).val(); });
		planDialog?.hide();
		return controller.load({ filters, page: 1 });
	}
	$host.on("input change", "[data-pool-filter]", (event) => {
		// A select emits input before change; rendering here would restore its old value.
		if (event.type === "input" && event.currentTarget.tagName === "SELECT") return;
		clearTimeout(searchTimer);
		if (event.type === "input") {
			// Invalidate old selections immediately, before the debounced read.
			controller.invalidate();
			planDialog?.hide();
			searchTimer = setTimeout(readFilters, 250);
		} else readFilters();
	});
	$host.on("click", "[data-pool-refresh]", refresh);
	$host.on("click", "[data-pool-page]", (event) => { planDialog?.hide(); controller.load({ page: Number(event.currentTarget.dataset.poolPage) }); });
	$host.on("change", "[data-pool-select]", (event) => {
		const error = controller.select(event.currentTarget.dataset.poolSelect, event.currentTarget.checked);
		if (error) { event.currentTarget.checked = false; frappe.msgprint(__(error)); }
	});
	$host.on("change", "[data-pool-select-page]", (event) => controller.selectPage(event.currentTarget.checked));
	$host.on("click", "[data-pool-stop]", () => controller.stop());
	$host.on("click", "[data-pool-recheck]", (event) => controller.recheckResult(event.currentTarget.dataset.poolRecheck));
	$host.on("click", "[data-pool-history]", (event) => openAssignment(event.currentTarget.dataset.poolHistory, refresh));
	const deactivate = () => {
		active = false;
		clearTimeout(searchTimer);
		planDialog?.hide();
		controller.deactivate();
	};
	// Frappe retains page instances when navigating; stop unsent work on route leave.
	frappe.router?.on("change", () => {
		if (active && frappe.get_route()[0] !== "production-workbench") deactivate();
	});
	$host.on("click", "[data-pool-plan]", () => {
		const rows = controller.selectedRows().map((row) => ({ ...row }));
		if (!rows.length || controller.state.running) return;
		let plans = {};
		let preview = false;
		let previewEmployee = null;
		const selectionKey = operationDispatchSelectionKey(rows);
		const showPreview = () => {
			if (!planDialog) return;
			planDialog.fields_dict.preview.$wrapper.html(operationDispatchPreviewHtml(rows, plans, preview));
			planDialog.set_primary_action(preview ? __(`确认派工 ${rows.length} 张任务`) : __("预览逐任务分配"), submit);
		};
		const submit = (values) => {
			if (!active || selectionKey !== operationDispatchSelectionKey(controller.selectedRows())) {
				planDialog.hide(); frappe.msgprint(__("选中范围已变化，请重新选择。")); return;
			}
			if (!preview || values.employee !== previewEmployee) {
				if (!values.employee) return;
				previewEmployee = values.employee;
				plans = Object.fromEntries(rows.map((row) => [row.job_card, [{ employee: values.employee, assigned_qty: Number(row.for_quantity), notes: "" }]]));
				preview = true; showPreview(); return;
			}
			planDialog.get_primary_btn().prop("disabled", true);
			planDialog.hide();
			controller.execute(plans).catch((error) => frappe.msgprint(esc(operationDispatchErrorText(error))));
		};
		planDialog = null;
		planDialog = new frappe.ui.Dialog({
			title: __("集中首次派工"), size: "extra-large",
			fields: [
				{ fieldname: "employee", fieldtype: "Link", options: "Employee", label: __("本轮共用工人"), reqd: 1,
					description: __("先给每张任务安排这名工人；预览中可逐张调整多人数量。"),
					get_query: () => ({ query: OPERATION_DISPATCH_API + "search_workers", filters: { job_card: rows[0].job_card } }),
					onchange() {
						// Link validation can finish after the preview click. Only a different
						// employee invalidates the quantities the user has just reviewed.
						if (!preview || planDialog?.get_value("employee") === previewEmployee) return;
						preview = false; plans = {}; showPreview();
					} },
				{ fieldname: "preview", fieldtype: "HTML" },
			],
			primary_action_label: __("预览逐任务分配"), primary_action: submit,
		});
		planDialog.fields_dict.preview.$wrapper.on("click", "[data-pool-edit]", (event) => {
			const row = rows.find((item) => item.job_card === event.currentTarget.dataset.poolEdit);
			if (!row || !preview) return;
			const editor = new frappe.ui.Dialog({
				title: __("本次人员分配 · {0}", [row.job_card]), size: "large",
				fields: [{ fieldname: "allocations", fieldtype: "Table", label: __("逐人分配任务量"), in_place_edit: true, data: plans[row.job_card].map((entry) => ({ ...entry })),
					description: __("合计须等于 {0} {1}；这里只调整本次预览。", [row.for_quantity, row.stock_uom]),
					fields: [
						{ fieldname: "employee", fieldtype: "Link", options: "Employee", label: __("工人"), in_list_view: 1, columns: 4, reqd: 1, get_query: () => ({ query: OPERATION_DISPATCH_API + "search_workers", filters: { job_card: row.job_card } }) },
						{ fieldname: "assigned_qty", fieldtype: "Float", label: __("派工数量"), in_list_view: 1, columns: 2, reqd: 1 },
						{ fieldname: "notes", fieldtype: "Data", label: __("备注"), in_list_view: 1, columns: 4 },
					] }],
				primary_action_label: __("更新本次预览"), primary_action(values) {
					const allocations = (values.allocations || []).filter((entry) => entry.employee || entry.assigned_qty || entry.notes).map((entry) => ({ employee: entry.employee, assigned_qty: Number(entry.assigned_qty), notes: entry.notes || "" }));
					const error = operationDispatchPlanError(allocations, row.for_quantity);
					if (error) { frappe.msgprint(__(error)); return; }
					plans[row.job_card] = allocations; editor.hide(); showPreview();
				},
			});
			editor.show();
		});
		showPreview();
		planDialog.$wrapper.on("hide.bs.modal.operation-pool", () => { planningOpen = false; });
		planningOpen = true;
		planDialog.show();
	});
	return {
		controller,
		activate() { active = true; return refresh(); },
		deactivate,
		refresh,
	};
}

const operationDispatchPoolApi = { OPERATION_DISPATCH_LIMIT, operationDispatchEscape, operationDispatchSelectable,
	operationDispatchSelectionKey, operationDispatchPlainDescription,
	operationDispatchPlanError, operationDispatchErrorText, operationDispatchUncertainError, operationDispatchReadback,
	operationDispatchBounded,
	createOperationDispatchController, operationDispatchRowHtml, operationDispatchResultsHtml, operationDispatchPreviewHtml,
	mountOperationDispatchPool };
if (typeof module !== "undefined" && module.exports) module.exports = operationDispatchPoolApi;
if (typeof window !== "undefined") {
	window.process_simplification = window.process_simplification || {};
	window.process_simplification.mount_operation_dispatch_pool = mountOperationDispatchPool;
}
