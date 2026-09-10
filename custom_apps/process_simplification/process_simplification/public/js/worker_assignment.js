const WORKER_ASSIGNMENT_GRID_COLUMNS = Object.freeze({
	employee: 4,
	assigned_qty: 2,
	notes: 4,
});

function workerAssignmentStatusMeta(status, translate = (message) => message) {
	const statuses = {
		"In Progress": { label: translate("计时中"), indicator: "blue" },
		"Pending Approval": { label: translate("待审核"), indicator: "orange" },
		Approved: { label: translate("已通过"), indicator: "green" },
		Rejected: { label: translate("已驳回"), indicator: "red" },
		Completed: { label: translate("已完成"), indicator: "green" },
		Cancelled: { label: translate("已取消"), indicator: "gray" },
	};
	return statuses[status] || { label: translate("已派工"), indicator: "gray" };
}

function workerAssignmentMaterialStatusMeta(status, translate = (message) => message) {
	const statuses = {
		READY_TO_REPORT: { label: translate("可报工"), indicator: "green" },
		MATERIAL_NOT_TRANSFERRED: { label: translate("等待发料"), indicator: "orange" },
		COMPLETED: { label: translate("已完成"), indicator: "gray" },
	};
	return statuses[status] || { label: translate("待确认"), indicator: "gray" };
}

function workerAssignmentWorkOrderStatusLabel(status, translate = (message) => message) {
	const statuses = {
		Draft: "草稿",
		"Not Started": "未开始",
		"In Process": "生产中",
		Stopped: "已停止",
		Completed: "已完成",
		Closed: "已关闭",
		Cancelled: "已取消",
	};
	return translate(statuses[status] || status || "待确认");
}

function canManageWorkerAssignments(user, roles = []) {
	return user === "Administrator" || ["System Manager", "Process Simplification Production Manager"].some((role) => roles.includes(role));
}

function defaultAssignmentJobCard(context = {}) {
	return (context.job_cards || []).find((row) => row.can_assign)?.name || "";
}

function workerAssignmentPlanRows(row = {}) {
	return ((row || {}).assignments || [])
		.filter((assignment) => assignment.assignment_status === "Active")
		.map((assignment) => ({
			employee: assignment.employee,
			assigned_qty: Number(assignment.assigned_qty || 0),
			notes: assignment.notes || "",
		}));
}

function normalizeWorkerAssignmentPlan(rows = []) {
	return (rows || [])
		.filter((row) => row?.employee || Number(row?.assigned_qty || 0) || row?.notes)
		.map((row) => ({
			employee: String(row.employee || "").trim(),
			assigned_qty: Number(row.assigned_qty || 0),
			notes: String(row.notes || "").trim(),
		}));
}

function workerAssignmentPlanTotal(rows = []) {
	return normalizeWorkerAssignmentPlan(rows).reduce(
		(total, row) => total + Number(row.assigned_qty || 0),
		0
	);
}

function workerAssignmentPlanError(rows, targetQty, translate = (message) => message) {
	const plan = normalizeWorkerAssignmentPlan(rows);
	if (!plan.length) return translate("请至少添加一名工人并填写派工数量。");
	const employees = new Set();
	for (const row of plan) {
		if (!row.employee) return translate("每一行都必须选择工人。");
		if (employees.has(row.employee)) return translate("同一名工人不能重复添加。");
		if (!(row.assigned_qty > 0)) return translate("每名工人的派工数量必须大于 0。");
		employees.add(row.employee);
	}
	const total = workerAssignmentPlanTotal(plan);
	if (Math.abs(total - Number(targetQty || 0)) > 1e-6) {
		return translate("所有工人的派工总数必须等于任务量：{0} ≠ {1}")
			.replace("{0}", String(total))
			.replace("{1}", String(Number(targetQty || 0)));
	}
	return "";
}

function workerRedispatchPlanError(rows, maxQty, translate = (message) => message) {
	const plan = normalizeWorkerAssignmentPlan(rows);
	if (!plan.length) return translate("请至少添加一名承接工人并填写数量。");
	const employees = new Set();
	for (const row of plan) {
		if (!row.employee) return translate("每一行都必须选择承接工人。");
		if (employees.has(row.employee)) return translate("同一名工人不能重复添加。");
		if (!(row.assigned_qty > 0)) return translate("二次派工数量必须大于 0。");
		employees.add(row.employee);
	}
	const total = workerAssignmentPlanTotal(plan);
	if (total - Number(maxQty || 0) > 1e-6) {
		return translate("本次重新派工最多只能分配 {0} 件，当前合计 {1} 件。")
			.replace("{0}", String(Number(maxQty || 0)))
			.replace("{1}", String(total));
	}
	return "";
}

function workerAssignmentBlockMessage(row = {}, translate = (message) => message) {
	const messages = {
		JOB_CARD_NOT_DRAFT: "生产任务单已提交或完成，不能再派工。",
		WORK_ORDER_UNAVAILABLE: "生产工单已完成、停止、关闭或取消，不能再派工。",
		NO_REMAINING_QTY: "生产任务单已没有剩余可报数量。",
		RATE_MISSING: "请先为该工序配置当前有效的计价规则。",
		RATE_CONFLICT: "该工序当前存在多条有效计价规则，请先处理冲突。",
		MATERIAL_NOT_FULLY_ISSUED: "物料尚未全部发到在制品仓，当前只能查看派工记录。",
		MATERIAL_NOT_READY: "物料尚未满足正式派工条件，当前只能查看派工记录。",
		DIRECT_MATERIAL_SHORTAGE: "直耗料现场库存不足，补料或锁料完成前不能新增派工。",
		DIRECT_PRIORITY_CONFLICT: "直耗料已优先分配给其他工单，当前不能新增派工。",
		ASSIGNMENT_PLAN_LOCKED: "已有报工记录，不能重做整单派工；退料释放的未完成数量需由主管重新派工。",
		SUPERVISOR_CONFLICT: "现有派工使用了不同审核主管，请先处理主管冲突。",
		OTHER_SUPERVISOR: "该生产任务单已由其他生产主管负责。",
		TIME_LOG_SETTING: "当前制造设置与简化报工计时方式不兼容。",
		CORRECTIVE_JOB_CARD: "简化报工暂不支持返工生产任务单。",
		SUB_OPERATIONS: "简化报工暂不支持包含子工序的生产任务单。",
		SPECIAL_JOB_CARD: "简化报工暂不支持半成品跟踪或委外生产任务单。",
		PROCESS_LOSS: "简化报工暂不支持含制程损耗的生产任务单。",
	};
	if (row.block_code === "ASSIGNMENT_PLAN_LOCKED" && row.block_message) {
		return row.block_message;
	}
	return messages[row.block_code] ? translate(messages[row.block_code]) : row.block_message || "";
}

function assignmentDialogCanSubmit(context, mode = "assign") {
	return mode !== "history" && (!context || Boolean(context.can_assign));
}

function workerLoadPreviewHtml(model = {}, helpers) {
	const { escapeHtml: esc, translate: t, formatNumber: number } = helpers;
	const stateLabel = (job) => job.state === "active" ? t(job.paused ? "计时已暂停" : "正在做")
		: t(job.state === "pending_review" ? "报工待审核" : "待做");
	const rows = (model.employees || []).map((employee) => {
		const jobs = (employee.jobs || []).map((job) => `<li>
			<div><strong>${esc(job.operation || job.job_card)}</strong><span class="indicator-pill ${job.state === "active" ? "blue" : "orange"}">${esc(stateLabel(job))}</span>${job.is_current ? `<span>${esc(t("本次工序"))}</span>` : ""}</div>
			<span>${esc(job.item_name || job.production_item || "")}</span>
			${job.requires_attention ? `<span class="text-warning">${esc(t("工单或派工当前不可继续，请先核对未结束的计时。"))}</span>` : ""}
			<small>${esc(job.job_card)} · ${esc(job.work_order)}</small>
			<span>${esc(t("剩余待做"))} ${number(job.remaining_qty)} ${esc(job.stock_uom || "")}${job.pending_qty ? ` · ${esc(t("待审核"))} ${number(job.pending_qty)}` : ""}</span>
		</li>`).join("");
		return `<article class="worker-load-person">
			<div class="worker-load-heading"><strong>${esc(employee.employee_name || employee.employee)}</strong><small>${esc(employee.employee)}</small></div>
			<div class="worker-load-counts"><span class="${employee.active_count ? "has-active" : ""}">${esc(t("进行中"))} <b>${number(employee.active_count)}</b></span><span>${esc(t("待做"))} <b>${number(employee.queued_count)}</b></span><span>${esc(t("待审核"))} <b>${number(employee.pending_review_count)}</b></span></div>
			${jobs ? `<details><summary>${esc(t("查看已有任务"))}</summary><ul>${jobs}</ul></details>` : `<p class="text-muted">${esc(t("当前没有未完成派工"))}</p>`}
		</article>`;
	}).join("");
	return `<div class="worker-load-heading"><strong>${esc(t("已选工人的任务情况"))}</strong><button type="button" class="btn btn-default btn-xs worker-load-refresh">${esc(t("更新任务情况"))}</button></div>
		<p class="worker-load-note">${esc(t("进行中含暂停，待做含待料任务。已有任务可继续派工，同一时间只能报工一项。"))}</p>
		${rows}${(model.unavailable || []).map((employee) => `<p class="text-warning">${esc(employee)} · ${esc(t("暂时无法读取任务情况，请重新选人或刷新。"))}</p>`).join("")}
		<small class="text-muted">${esc(t("查询时间"))} ${esc(model.checked_at || "")}</small>`;
}

function createWorkerLoadController({ read, render }) {
	let revision = 0;
	let lastKey = "";
	let loadedAt = 0;
	return {
		async load(selection, force = false) {
			const employees = [...new Set(selection.employees.filter(Boolean))].sort();
			const key = JSON.stringify([selection.jobCard, employees]);
			if (!force && key === lastKey && Date.now() - loadedAt < 30000) return;
			lastKey = key;
			loadedAt = Date.now();
			const token = ++revision;
			if (!selection.jobCard || !employees.length) { render("empty"); return; }
			render("loading");
			try {
				const results = [];
				for (let start = 0; start < employees.length; start += 50) {
					results.push(await read(selection.jobCard, employees.slice(start, start + 50)));
					if (token !== revision) return;
				}
				const rows = results.flatMap((result) => result.employees || []);
				const known = new Set(rows.map((row) => row.employee));
				render("ready", { employees: rows, checked_at: results.at(-1)?.checked_at,
					unavailable: employees.filter((employee) => !known.has(employee)) });
			} catch (error) {
				if (token !== revision) return;
				lastKey = "";
				render("error");
			}
		},
		dispose() { revision++; },
	};
}

function bindWorkerLoadPreview(dialog, getJobCard) {
	const host = dialog.fields_dict.worker_load.$wrapper.addClass("worker-load-preview");
	let timer;
	const controller = createWorkerLoadController({
		read: async (jobCard, employees) => (await frappe.call({
			method: "process_simplification.api.production_reporting.get_worker_loads",
			args: { job_card: jobCard, employees: JSON.stringify(employees) },
		})).message,
		render(state, model) {
			host.attr("aria-busy", state === "loading" ? "true" : "false");
			if (state === "empty") { host.html(`<p class="text-muted">${__("选择工人后，在这里查看姓名及已有任务。")}</p>`); return; }
			if (state === "loading") {
				dialog.fields_dict.allocations.$wrapper.find(".worker-load-inline").remove();
				host.html(`<p role="status">${__("正在读取工人任务情况…")}</p>`); return;
			}
			if (state === "error") { host.html(`<p class="text-warning" role="status">${__("任务情况读取失败，不能据此判断工人是否空闲。")}</p><button type="button" class="btn btn-default worker-load-refresh">${__("重试")}</button>`); return; }
			host.html(workerLoadPreviewHtml(model, {
				escapeHtml: frappe.utils.escape_html, translate: __,
				formatNumber: (value) => Number(value || 0).toLocaleString("zh-CN", { maximumFractionDigits: 6 }),
			}));
			const loads = new Map(model.employees.map((row) => [row.employee, row]));
			for (const row of dialog.fields_dict.allocations.grid.grid_rows || []) {
				const load = loads.get(row.doc.employee);
				row.doc.employee_name = load?.employee_name || "";
				row.refresh_field("employee");
				const cell = row.columns.employee;
				cell?.find(".worker-load-inline").remove();
				if (cell && load) {
					const inline = $('<div class="worker-load-inline">').appendTo(cell);
					$('<strong class="worker-load-inline-name">').text(load.employee_name || load.employee).appendTo(inline);
					$('<span>').text(__("进行中 {0} · 待做 {1} · 待审核 {2}", [load.active_count, load.queued_count, load.pending_review_count])).appendTo(inline);
				}
			}
		},
	});
	const load = (force) => controller.load({ jobCard: getJobCard(),
		employees: (dialog.fields_dict.allocations.grid.get_data() || []).map((row) => row.employee) }, force);
	dialog.worker_load = { schedule() { clearTimeout(timer); timer = setTimeout(() => load(false), 180); } };
	host.on("click", ".worker-load-refresh", () => load(true));
	dialog.$wrapper.on("click.worker-load", ".grid-remove-rows, .grid-delete-row", () => dialog.worker_load.schedule());
	dialog.$wrapper.on("hide.bs.modal.worker-load", () => { clearTimeout(timer); controller.dispose(); });
	load(false);
}

function workerAssignmentEmployeeFormatter(value, df, options, doc) {
	return frappe.utils.escape_html(doc?.employee_name || value || "");
}

function workOrderAssignmentContextHtml(context = {}, helpers) {
	const esc = helpers.escapeHtml;
	const number = helpers.formatNumber;
	const t = helpers.translate;
	const workOrder = context.work_order || {};
	const cards = (context.job_cards || []).map((row) => {
		const material = workerAssignmentMaterialStatusMeta(row.material_status, t);
		const assignments = (row.assignments || []).length
			? (row.assignments || [])
					.map((assignment) => {
						const status = workerAssignmentStatusMeta(
							assignment.report_status || assignment.assignment_status,
							t
						);
						const movementFacts = Number(assignment.released_qty || 0) || Number(assignment.redispatched_qty || 0)
							? ` · ${esc(t("原派"))} ${number(assignment.original_assigned_qty)}${Number(assignment.released_qty || 0) ? ` · ${esc(t("释放"))} ${number(assignment.released_qty)}` : ""}${Number(assignment.redispatched_qty || 0) ? ` · ${esc(t("转入"))} ${number(assignment.redispatched_qty)}` : ""}`
							: "";
						return `<span class="worker-assignment-person">${esc(assignment.employee_name || assignment.employee || "")} <span class="text-muted">· ${esc(t("当前分配"))} ${number(assignment.effective_assigned_qty ?? assignment.assigned_qty)}${movementFacts} · ${esc(t("已报"))} ${number(assignment.completed_qty)}${Number(assignment.pending_qty || 0) > 0 ? ` · ${esc(t("待审"))} ${number(assignment.pending_qty)}` : ""} · ${esc(t("剩余"))} ${number(assignment.remaining_qty)}</span> <span class="indicator-pill ${esc(status.indicator)}">${esc(status.label)}</span>${assignment.supervisor ? ` <span class="text-muted">· ${esc(t("审核"))}：${esc(assignment.supervisor)}</span>` : ""}</span>`;
					})
					.join("")
			: `<span class="text-muted">${esc(t("未派工"))}</span>`;
		const blockMessage = workerAssignmentBlockMessage(row, t);
		const blockClass = row.block_code === "ASSIGNMENT_PLAN_LOCKED" ? "text-info" : "text-danger";
		const redispatchAction = row.can_redispatch
			? `<button class="btn btn-xs btn-primary worker-assignment-redispatch" data-job-card="${esc(row.name || "")}">${esc(t("重新派工剩余 {0} 件")).replace("{0}", number(row.redispatchable_qty))}</button>`
			: "";
		return `<article class="worker-assignment-job-card${row.can_assign ? "" : " is-blocked"}">
			<div class="worker-assignment-job-heading"><a href="/app/job-card/${encodeURIComponent(row.name || "")}" target="_blank"><strong>${esc(row.name || "")}</strong></a><span>${esc(row.operation || "")}</span><span class="indicator-pill ${esc(material.indicator)}">${esc(material.label)}</span></div>
			<div class="worker-assignment-job-facts"><span>${esc(t("工作站"))}：${esc(row.workstation || t("未设置"))}</span><span>${esc(t("任务量"))}：${number(row.for_quantity)}</span><span>${esc(t("原派合计"))}：${number(row.assigned_total_qty)}</span><span>${esc(t("当前分配合计"))}：${number(row.effective_assigned_total_qty ?? row.assigned_total_qty)}</span><span>${esc(t("已完成"))}：${number(row.completed_qty)}</span><span>${esc(t("剩余"))}：${number(row.remaining_qty)}</span><span>${esc(t("当前可报"))}：${number(row.available_reportable_qty)}</span>${Number(row.released_pool_qty || 0) ? `<span>${esc(t("待重新派工"))}：${number(row.released_pool_qty)}</span>` : ""}<span>${esc(t("审核主管"))}：${esc(row.display_supervisor || row.assignment_supervisor || "")}</span></div>
			<div class="worker-assignment-people">${assignments}</div>
			${blockMessage ? `<p class="${blockClass} worker-assignment-block">${esc(blockMessage)}</p>` : ""}
			${redispatchAction ? `<div class="worker-assignment-actions">${redispatchAction}</div>` : ""}
		</article>`;
	}).join("");
	return `<div class="worker-assignment-context">
		<div class="worker-assignment-work-order"><strong>${esc(workOrder.name || "")}</strong><span>${esc(workOrder.production_item || "")}</span><span>${esc(workerAssignmentWorkOrderStatusLabel(workOrder.status, t))}</span><span>${esc(t("已生产 / 计划"))}：${number(workOrder.produced_qty)} / ${number(workOrder.qty)}</span></div>
		<div class="worker-assignment-job-list">${cards || `<div class="text-muted">${esc(t("该生产工单没有生产任务单。"))}</div>`}</div>
		${context.can_assign === false ? `<p class="text-muted worker-assignment-read-only">${esc(t(context.can_redispatch ? "原派工历史保持不变；只对已释放且补料覆盖的未完成数量重新派工。" : "当前不能新增或重做整单派工；已有有效分配仍可继续报工。"))}</p>` : ""}
	</div>`;
}

function openWorkerRedispatchDialog(row, options = {}) {
	const maxQty = Number(row?.redispatchable_qty || 0);
	let dialog;
	dialog = new frappe.ui.Dialog({
		title: __("重新派工未完成数量 · {0}", [row?.name || ""]),
		size: "large",
		fields: [
			{
				fieldtype: "HTML",
				options: `<p>${__("原派工与报工历史不会修改。本次最多重新分配 {0} 件；补料本身不会自动分回原工人。", [format_number(flt(maxQty), null, 2)])}</p>`,
			},
			{
				fieldname: "allocations",
				fieldtype: "Table",
				label: __("承接工人及数量"),
				reqd: 1,
				in_place_edit: true,
				data: [{ employee: "", assigned_qty: maxQty }],
				fields: [
					{
						fieldname: "employee",
						fieldtype: "Link",
						options: "Employee",
						label: __("承接工人"),
						reqd: 1,
						in_list_view: 1,
						columns: WORKER_ASSIGNMENT_GRID_COLUMNS.employee,
						formatter: workerAssignmentEmployeeFormatter,
						onchange() {
							if (this.grid_row?.doc) this.grid_row.doc.employee_name = "";
							this.grid_row?.columns?.employee?.find(".worker-load-inline").remove();
							dialog?.worker_load?.schedule();
						},
						get_query: () => ({
							query: "process_simplification.api.production_reporting.search_workers",
							filters: { job_card: row.name, include_assigned: 1 },
						}),
					},
					{
						fieldname: "assigned_qty",
						fieldtype: "Float",
						label: __("二次派工数量"),
						reqd: 1,
						in_list_view: 1,
						columns: WORKER_ASSIGNMENT_GRID_COLUMNS.assigned_qty,
					},
				],
			},
			{ fieldname: "worker_load", fieldtype: "HTML" },
			{
				fieldname: "reason",
				fieldtype: "Small Text",
				label: __("重新派工说明"),
				default: __("退料释放后的未完成数量重新派工"),
			},
		],
		primary_action_label: __("确认重新派工"),
		primary_action: async (values) => {
			const allocations = normalizeWorkerAssignmentPlan(values.allocations);
			const error = workerRedispatchPlanError(allocations, maxQty, __);
			if (error) {
				frappe.msgprint(error);
				return;
			}
			const requestId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
			const $button = dialog.get_primary_btn();
			$button.prop("disabled", true).text(__("处理中..."));
			try {
				await frappe.call({
					method: "process_simplification.api.production_reporting.redispatch_remaining",
					type: "POST",
					args: {
						job_card: row.name,
						allocations: JSON.stringify(allocations),
						request_id: requestId,
						reason: values.reason,
					},
				});
				dialog.hide();
				options.parent_dialog?.hide();
				frappe.show_alert({ message: __("未完成数量已重新派工。"), indicator: "green" });
				await options.on_success?.();
			} finally {
				$button.prop("disabled", false).text(__("确认重新派工"));
			}
		},
	});
	dialog.$wrapper.addClass("ps-worker-assignment-dialog");
	dialog.show();
	bindWorkerLoadPreview(dialog, () => row.name);
	return dialog;
}

function openWorkerAssignmentDialog(options = {}) {
	const loadContext = options.work_order
		? frappe.call({
				method: "process_simplification.api.production_reporting.get_work_order_assignment_context",
				args: { work_order: options.work_order },
				freeze: true,
				freeze_message: __("正在读取工序与派工状态..."),
			})
		: Promise.resolve({ message: null });

	return loadContext.then((response) => {
		let context = response.message || null;
		let dialog;
		const canSubmit = assignmentDialogCanSubmit(context, options.mode);
		const displayContext = options.mode === "history" && context
			? { ...context, can_assign: false }
			: context;
		const defaultJobCard = canSubmit && context ? defaultAssignmentJobCard(context) : "";
		const defaultRow = context
			? (context.job_cards || []).find((row) => row.name === defaultJobCard)
			: null;
		const fields = [];
		if (context) {
			fields.push({
				fieldtype: "HTML",
				options: workOrderAssignmentContextHtml(displayContext, {
					translate: __,
					escapeHtml: frappe.utils.escape_html,
					formatNumber: (value) => format_number(flt(value), null, 2),
				}),
			});
		}
		if (canSubmit) fields.push(
			{
				fieldname: "job_card",
				fieldtype: "Link",
				options: "Job Card",
				label: __("生产任务单（工序）"),
				reqd: 1,
				default: defaultJobCard,
				onchange: async () => {
					const jobCard = dialog.get_value("job_card");
					let row = (context?.job_cards || []).find((item) => item.name === jobCard);
					if (!row && jobCard) {
						const values = await frappe.db.get_value(
							"Job Card",
							jobCard,
							["work_order", "for_quantity"]
						);
						const workOrder = values?.message?.work_order;
						if (workOrder) {
							const result = await frappe.call({
								method: "process_simplification.api.production_reporting.get_work_order_assignment_context",
								args: { work_order: workOrder },
							});
							context = result.message || context;
							row = (context?.job_cards || []).find((item) => item.name === jobCard);
						}
						if (!row) row = { name: jobCard, for_quantity: values?.message?.for_quantity };
					}
					dialog.set_value("task_qty", Number(row?.for_quantity || 0));
					const allocationField = dialog.fields_dict.allocations;
					allocationField.df.data = workerAssignmentPlanRows(row);
					allocationField.grid.refresh();
					dialog?.worker_load?.schedule();
					if (!context?.can_choose_supervisor || !dialog.fields_dict.supervisor) return;
					dialog.set_value("supervisor", row?.assignment_supervisor || "");
					dialog.set_df_property("supervisor", "read_only", !row?.can_choose_supervisor);
				},
				get_query: () => ({
					query: "process_simplification.api.production_reporting.search_draft_job_cards",
					filters: context ? { work_order: context.work_order.name } : {},
				}),
			},
			...(context?.can_choose_supervisor
				? [{
					fieldname: "supervisor",
					fieldtype: "Link",
					options: "User",
					label: __("审核主管"),
					reqd: 1,
					default: defaultRow?.assignment_supervisor || context.assignment_supervisor,
					read_only: !defaultRow?.can_choose_supervisor,
					get_query: () => ({
						query: "process_simplification.api.production_reporting.search_assignment_supervisors",
						filters: { work_order: context.work_order.name },
					}),
				}]
				: []),
			{
				fieldname: "task_qty",
				fieldtype: "Float",
				label: __("任务量"),
				read_only: 1,
				default: Number(defaultRow?.for_quantity || 0),
			},
			{
				fieldname: "allocations",
				fieldtype: "Table",
				label: __("工人及派工数量"),
				reqd: 1,
				in_place_edit: true,
				cannot_add_rows: false,
				cannot_delete_rows: false,
				description: __("逐人填写派工数量；所有行合计必须等于任务量。"),
				data: workerAssignmentPlanRows(defaultRow),
				fields: [
					{
						fieldname: "employee",
						fieldtype: "Link",
						options: "Employee",
						label: __("工人"),
						reqd: 1,
						in_list_view: 1,
						columns: WORKER_ASSIGNMENT_GRID_COLUMNS.employee,
						formatter: workerAssignmentEmployeeFormatter,
						onchange() {
							if (this.grid_row?.doc) this.grid_row.doc.employee_name = "";
							this.grid_row?.columns?.employee?.find(".worker-load-inline").remove();
							dialog?.worker_load?.schedule();
						},
						get_query: () => ({
							query: "process_simplification.api.production_reporting.search_workers",
							filters: { job_card: dialog.get_value("job_card") },
						}),
					},
					{
						fieldname: "assigned_qty",
						fieldtype: "Float",
						label: __("派工数量"),
						reqd: 1,
						in_list_view: 1,
						columns: WORKER_ASSIGNMENT_GRID_COLUMNS.assigned_qty,
					},
					{
						fieldname: "notes",
						fieldtype: "Data",
						label: __("派工备注"),
						in_list_view: 1,
						columns: WORKER_ASSIGNMENT_GRID_COLUMNS.notes,
					},
				],
			},
			{ fieldname: "worker_load", fieldtype: "HTML" }
		);

		const dialogOptions = {
			title: context
				? __(
					options.mode === "history"
						? "派工记录 · {0}"
						: canSubmit
							? "工单派工 · {0}"
							: "工单派工状态 · {0}",
					[context.work_order.name]
				)
				: __("新增多人派工"),
			size: "large",
			fields,
		};
		if (canSubmit) {
			dialogOptions.primary_action_label = __("确认派工");
			dialogOptions.primary_action = async (values) => {
				const selected = context
					? (context.job_cards || []).find((row) => row.name === values.job_card)
					: null;
				if (selected && !selected.can_assign) {
					frappe.msgprint(workerAssignmentBlockMessage(selected, __) || __("当前生产任务单不可派工。"));
					return;
				}
				const allocations = normalizeWorkerAssignmentPlan(values.allocations);
				const planError = workerAssignmentPlanError(
					allocations,
					selected?.for_quantity || values.task_qty,
					__
				);
				if (planError) {
					frappe.msgprint(planError);
					return;
				}
				dialog.get_primary_btn().prop("disabled", true).text(__("处理中..."));
				try {
					await frappe.call({
						method: "process_simplification.api.production_reporting.assign_workers",
						type: "POST",
						args: {
							job_card: values.job_card,
							assignments: JSON.stringify(allocations),
							supervisor: context
								? values.supervisor || selected?.assignment_supervisor
								: undefined,
						},
					});
					dialog.hide();
					frappe.show_alert({ message: __("多人派工方案已保存。"), indicator: "green" });
					await options.on_success?.();
				} finally {
					dialog.get_primary_btn().prop("disabled", false).text(__("确认派工"));
				}
			};
		}
		dialog = new frappe.ui.Dialog(dialogOptions);
		dialog.$wrapper.addClass("ps-worker-assignment-dialog");
		dialog.$wrapper.on("click", ".worker-assignment-redispatch", (event) => {
			const jobCard = String($(event.currentTarget).data("job-card") || "");
			const row = (context?.job_cards || []).find((item) => item.name === jobCard);
			if (row?.can_redispatch) {
				openWorkerRedispatchDialog(row, {
					parent_dialog: dialog,
					on_success: options.on_success,
				});
			}
		});
		dialog.show();
		if (canSubmit) bindWorkerLoadPreview(dialog, () => dialog.get_value("job_card"));
		return dialog;
	});
}

const workerAssignmentApi = {
	WORKER_ASSIGNMENT_GRID_COLUMNS,
	workerAssignmentStatusMeta,
	workerAssignmentMaterialStatusMeta,
	workerAssignmentWorkOrderStatusLabel,
	canManageWorkerAssignments,
	defaultAssignmentJobCard,
	workerAssignmentPlanRows,
	normalizeWorkerAssignmentPlan,
	workerAssignmentPlanTotal,
	workerAssignmentPlanError,
	workerRedispatchPlanError,
	workerAssignmentBlockMessage,
	assignmentDialogCanSubmit,
	createWorkerLoadController,
	workerLoadPreviewHtml,
	workOrderAssignmentContextHtml,
	openWorkerRedispatchDialog,
	openWorkerAssignmentDialog,
};

if (typeof module !== "undefined" && module.exports) module.exports = workerAssignmentApi;
if (typeof window !== "undefined") {
	window.process_simplification = window.process_simplification || {};
	window.process_simplification.open_worker_assignment_dialog = openWorkerAssignmentDialog;
	window.process_simplification.can_manage_worker_assignments = () =>
		canManageWorkerAssignments(frappe.session.user, frappe.user_roles || []);
}
