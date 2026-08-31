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

function canManageWorkerAssignments(user, roles = []) {
	return user === "Administrator" || ["System Manager", "Process Simplification Production Manager"].some((role) => roles.includes(role));
}

function defaultAssignmentJobCard(context = {}) {
	return (context.job_cards || []).find((row) => row.can_assign)?.name || "";
}

function workerAssignmentBlockMessage(row = {}, translate = (message) => message) {
	const messages = {
		JOB_CARD_NOT_DRAFT: "生产任务单已提交或完成，不能再派工。",
		WORK_ORDER_UNAVAILABLE: "生产工单已完成、停止、关闭或取消，不能再派工。",
		NO_REMAINING_QTY: "生产任务单已没有剩余可报数量。",
		RATE_MISSING: "请先为该工序配置当前有效的计价规则。",
		RATE_CONFLICT: "该工序当前存在多条有效计价规则，请先处理冲突。",
		SUPERVISOR_CONFLICT: "现有派工使用了不同审核主管，请先处理主管冲突。",
		OTHER_SUPERVISOR: "该生产任务单已由其他生产主管负责。",
		TIME_LOG_SETTING: "当前制造设置与简化报工计时方式不兼容。",
		CORRECTIVE_JOB_CARD: "简化报工暂不支持返工生产任务单。",
		SUB_OPERATIONS: "简化报工暂不支持包含子工序的生产任务单。",
		SPECIAL_JOB_CARD: "简化报工暂不支持半成品跟踪或委外生产任务单。",
		PROCESS_LOSS: "简化报工暂不支持含制程损耗的生产任务单。",
	};
	return messages[row.block_code] ? translate(messages[row.block_code]) : row.block_message || "";
}

function assignmentDialogCanSubmit(context) {
	return !context || Boolean(context.can_assign);
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
						return `<span class="worker-assignment-person">${esc(assignment.employee_name || assignment.employee || "")} <span class="indicator-pill ${esc(status.indicator)}">${esc(status.label)}</span>${assignment.supervisor ? ` <span class="text-muted">· ${esc(t("审核"))}：${esc(assignment.supervisor)}</span>` : ""}</span>`;
					})
					.join("")
			: `<span class="text-muted">${esc(t("未派工"))}</span>`;
		const blockMessage = workerAssignmentBlockMessage(row, t);
		return `<article class="worker-assignment-job-card${row.can_assign ? "" : " is-blocked"}">
			<div class="worker-assignment-job-heading"><a href="/app/job-card/${encodeURIComponent(row.name || "")}" target="_blank"><strong>${esc(row.name || "")}</strong></a><span>${esc(row.operation || "")}</span><span class="indicator-pill ${esc(material.indicator)}">${esc(material.label)}</span></div>
			<div class="worker-assignment-job-facts"><span>${esc(t("工作站"))}：${esc(row.workstation || t("未设置"))}</span><span>${esc(t("任务量"))}：${number(row.for_quantity)}</span><span>${esc(t("已完成"))}：${number(row.completed_qty)}</span><span>${esc(t("剩余"))}：${number(row.remaining_qty)}</span><span>${esc(t("当前可报"))}：${number(row.available_reportable_qty)}</span><span>${esc(t("审核主管"))}：${esc(row.display_supervisor || row.assignment_supervisor || "")}</span></div>
			<div class="worker-assignment-people">${assignments}</div>
			${blockMessage ? `<p class="text-danger worker-assignment-block">${esc(blockMessage)}</p>` : ""}
		</article>`;
	}).join("");
	return `<div class="worker-assignment-context">
		<div class="worker-assignment-work-order"><strong>${esc(workOrder.name || "")}</strong><span>${esc(workOrder.production_item || "")}</span><span>${esc(workOrder.status || "")}</span><span>${esc(t("已生产 / 计划"))}：${number(workOrder.produced_qty)} / ${number(workOrder.qty)}</span></div>
		<div class="worker-assignment-job-list">${cards || `<div class="text-muted">${esc(t("该生产工单没有生产任务单。"))}</div>`}</div>
		${context.can_assign === false ? `<p class="text-muted worker-assignment-read-only">${esc(t("当前工单没有可派工的生产任务，仅显示已有状态和历史记录。"))}</p>` : ""}
	</div>`;
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
		const context = response.message || null;
		const canSubmit = assignmentDialogCanSubmit(context);
		const defaultJobCard = context ? defaultAssignmentJobCard(context) : "";
		const defaultRow = context
			? (context.job_cards || []).find((row) => row.name === defaultJobCard)
			: null;
		const fields = [];
		if (context) {
			fields.push({
				fieldtype: "HTML",
				options: workOrderAssignmentContextHtml(context, {
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
				onchange: () => {
					dialog.set_value("employee", "");
					if (!context?.can_choose_supervisor) return;
					const row = (context.job_cards || []).find(
						(item) => item.name === dialog.get_value("job_card")
					);
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
				fieldname: "employee",
				fieldtype: "Link",
				options: "Employee",
				label: __("工人"),
				reqd: 1,
				get_query: () => ({
					query: "process_simplification.api.production_reporting.search_workers",
					filters: { job_card: dialog.get_value("job_card") },
				}),
			},
			{ fieldname: "notes", fieldtype: "Small Text", label: __("派工备注") }
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
				: __("新增工人派工"),
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
				dialog.get_primary_btn().prop("disabled", true).text(__("处理中..."));
				try {
					await frappe.call({
						method: "process_simplification.api.production_reporting.assign_worker",
						type: "POST",
						args: {
							...values,
							supervisor: context
								? values.supervisor || selected?.assignment_supervisor
								: undefined,
						},
					});
					dialog.hide();
					frappe.show_alert({ message: __("派工已创建。"), indicator: "green" });
					await options.on_success?.();
				} finally {
					dialog.get_primary_btn().prop("disabled", false).text(__("确认派工"));
				}
			};
		}
		const dialog = new frappe.ui.Dialog(dialogOptions);
		dialog.show();
		return dialog;
	});
}

const workerAssignmentApi = {
	workerAssignmentStatusMeta,
	workerAssignmentMaterialStatusMeta,
	canManageWorkerAssignments,
	defaultAssignmentJobCard,
	workerAssignmentBlockMessage,
	assignmentDialogCanSubmit,
	workOrderAssignmentContextHtml,
	openWorkerAssignmentDialog,
};

if (typeof module !== "undefined" && module.exports) module.exports = workerAssignmentApi;
if (typeof window !== "undefined") {
	window.process_simplification = window.process_simplification || {};
	window.process_simplification.open_worker_assignment_dialog = openWorkerAssignmentDialog;
	window.process_simplification.can_manage_worker_assignments = () =>
		canManageWorkerAssignments(frappe.session.user, frappe.user_roles || []);
}
