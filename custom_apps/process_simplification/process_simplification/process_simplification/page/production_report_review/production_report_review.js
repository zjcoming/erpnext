function reviewStatusMeta(status, translate = (message) => message) {
	const statuses = {
		Approved: { label: translate("已通过"), indicator: "green" },
		Rejected: { label: translate("已驳回"), indicator: "red" },
		"Pending Approval": { label: translate("待审核"), indicator: "orange" },
	};
	return statuses[status] || { label: status || translate("未知"), indicator: "gray" };
}

function reviewActionState(report, translate = (message) => message) {
	const approveDisabled = report.can_approve === false || report.capacity_conflict;
	const rejectDisabled = report.can_reject === false;
	return {
		approve_disabled: approveDisabled,
		reject_disabled: rejectDisabled,
		message:
			report.approve_block_message ||
			report.reject_block_message ||
			(approveDisabled ? translate("数据已变化，请刷新；当前只能驳回重报。") : ""),
	};
}

function reviewAssignmentActionState(assignment, translate = (message) => message) {
	if (assignment.active_report) {
		return {
			action: "cancel_session",
			label: translate("取消活动计时"),
			message: translate("工人仍在计时；可先取消活动计时，再处理派工。"),
		};
	}
	if (assignment.can_unassign) {
		return { action: "unassign", label: translate("取消派工"), message: "" };
	}
	return {
		action: null,
		label: "",
		message: assignment.unassign_block_message || translate("已有报工历史，不能取消派工。"),
	};
}

function reviewEmployeeLabel(row = {}) {
	return row.employee_name || row.employee || "-";
}

function reviewPaginationHtml(pagination = {}, listName, helpers) {
	const translate = helpers.translate;
	const esc = helpers.escapeHtml;
	const page = Number(pagination.page || 1);
	const pageLength = Number(pagination.page_length || 20);
	const totalPages = Number(pagination.total_pages || 0);
	const totalCount = Number(pagination.total_count || 0);
	const previousPage = Math.max(page - 1, 1);
	const nextPage = totalPages ? Math.min(page + 1, totalPages) : page + 1;
	return `<div class="report-review-pagination" aria-label="${esc(translate("分页"))}">
		<div>${esc(translate("第"))} ${page} / ${totalPages || 1} ${esc(translate("页"))} · ${esc(translate("共"))} ${totalCount} ${esc(translate("条"))}</div>
		<div class="report-review-pagination-actions">
			<button class="btn btn-default btn-sm report-page-action" data-list="${esc(listName)}" data-page="${previousPage}" ${pagination.has_prev ? "" : "disabled"}>${esc(translate("上一页"))}</button>
			<button class="btn btn-default btn-sm report-page-action" data-list="${esc(listName)}" data-page="${nextPage}" ${pagination.has_next ? "" : "disabled"}>${esc(translate("下一页"))}</button>
			<select class="form-control input-sm report-page-size" data-list="${esc(listName)}" aria-label="${esc(translate("每页条数"))}">
				${[20, 50, 100].map((size) => `<option value="${size}" ${size === pageLength ? "selected" : ""}>${size} ${esc(translate("条/页"))}</option>`).join("")}
			</select>
		</div>
	</div>`;
}

function reviewDocumentLink(doctype, name, label, helpers) {
	if (!name) return "-";
	const esc = helpers.escapeHtml;
	return `<a href="#" class="report-document-link" data-doctype="${esc(doctype)}" data-name="${esc(name)}">${esc(label || name)}</a>`;
}

function reviewDetailsHtml(row = {}, helpers) {
	const t = helpers.translate;
	const esc = helpers.escapeHtml;
	const number = helpers.formatNumber;
	const dateTime = helpers.formatDateTime;
	const meta = reviewStatusMeta(row.status, t);
	const value = (label, content) => `<div class="report-detail-item"><span>${esc(t(label))}</span><strong>${content}</strong></div>`;
	const wageType = row.wage_type === "Time" ? t("计时") : t("计件");
	return `<div class="report-review-detail-grid">
		${value("报工记录", reviewDocumentLink("Job Card Work Report", row.name, row.name, helpers))}
		${value("状态", `<span class="indicator-pill ${meta.indicator}">${esc(meta.label)}</span>`)}
		${value("工人姓名", esc(reviewEmployeeLabel(row)))}
		${value("工号", esc(row.employee || "-"))}
		${value("生产任务单", reviewDocumentLink("Job Card", row.job_card, row.job_card, helpers))}
		${value("生产工单", reviewDocumentLink("Work Order", row.work_order, row.work_order, helpers))}
		${value("工序", esc(row.operation || "-"))}
		${value("生产日期", esc(row.labor_date || "-"))}
		${value("实际开始", esc(dateTime(row.actual_start_time)))}
		${value("实际结束", esc(dateTime(row.actual_end_time)))}
		${value("实际分钟", number(row.actual_minutes))}
		${value("申报数量", number(row.completed_qty))}
			${value("计价方式", esc(wageType))}
			${value("计薪分钟", number(row.reported_minutes))}
			${row.wage_type === "Time" ? value("计薪工时来源", esc(row.manual_time_entry ? t("工人手工填写") : t("计时器有效分钟"))) : ""}
		${value("单价快照", number(row.rate))}
		${value("计薪金额", number(row.wage_amount))}
		${value("提交时间", esc(dateTime(row.submitted_at)))}
		${value("审核人", esc(row.reviewed_by || "-"))}
		${value("审核时间", esc(dateTime(row.reviewed_at)))}
		${row.rejection_reason ? `<div class="report-detail-item report-detail-wide"><span>${esc(t("驳回原因"))}</span><strong>${esc(row.rejection_reason)}</strong></div>` : ""}
		${row.monthly_summary ? value("月度工资汇总", reviewDocumentLink("Monthly Worker Wage Summary", row.monthly_summary, row.monthly_summary, helpers)) : ""}
	</div>`;
}

function normalizeFrappeDateTime(value) {
	return typeof value === "string"
		? value.replace(/(\d{2}:\d{2}:\d{2})\.\d{1,6}(?=(?:Z|[+-]\d{2}:?\d{2})?$)/, "$1")
		: value;
}

function reviewMonthRange(month) {
	const match = /^(\d{4})-(\d{2})$/.exec(String(month || ""));
	if (!match) return { from_date: null, to_date: null };
	const year = Number(match[1]);
	const monthNumber = Number(match[2]);
	if (monthNumber < 1 || monthNumber > 12) return { from_date: null, to_date: null };
	const lastDay = new Date(Date.UTC(year, monthNumber, 0)).getUTCDate();
	return {
		from_date: `${match[1]}-${match[2]}-01`,
		to_date: `${match[1]}-${match[2]}-${String(lastDay).padStart(2, "0")}`,
	};
}

function runReviewToolbarLoad(load) {
	load();
}

const productionReportReviewApi = {
	reviewStatusMeta,
	reviewActionState,
	reviewAssignmentActionState,
	reviewEmployeeLabel,
	reviewPaginationHtml,
	reviewDetailsHtml,
	normalizeFrappeDateTime,
	reviewMonthRange,
	runReviewToolbarLoad,
};
if (typeof module !== "undefined" && module.exports) module.exports = productionReportReviewApi;

if (typeof frappe !== "undefined") {
	frappe.pages["production-report-review"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({ parent: wrapper, title: __("报工审核"), single_column: true });
		page.main.html(`
			<div class="process-simplification-page report-review-page">
				<section class="review-hero report-review-hero">
					<div class="review-hero-copy">
						<span class="review-eyebrow">${__("当班审核台")}</span>
						<h2>${__("先处理待审，再回看历史")}</h2>
						<p>${__("数量、工时和物料边界一眼可见；通过后立即写入生产任务单。")}</p>
					</div>
					<div class="review-kpi-grid">
						<div class="review-kpi is-urgent"><span>${__("待审核")}</span><strong class="report-kpi-pending">0</strong><small>${__("需要主管决定")}</small></div>
						<div class="review-kpi"><span>${__("活动派工")}</span><strong class="report-kpi-assignments">0</strong><small>${__("当前在岗任务")}</small></div>
						<div class="review-kpi is-success"><span>${__("今日已处理")}</span><strong class="report-kpi-processed">0</strong><small>${__("通过与驳回")}</small></div>
					</div>
				</section>
				<section class="review-section review-priority-section">
					<div class="review-section-heading"><div><span class="review-step">1</span><div><h3>${__("待审核报工")}</h3><p>${__("按提交时间排序，优先核对数量与工时。")}</p></div></div><span class="review-count-badge report-pending-count">0</span></div>
					<div class="report-review-queue"></div><div class="report-pager-reports"></div>
				</section>
				<section class="review-section review-secondary-section">
					<div class="review-section-heading"><div><span class="review-step is-muted">2</span><div><h3>${__("当前派工")}</h3><p>${__("用于处理活动计时或尚未报工的派工。")}</p></div></div><span class="review-count-badge report-assignment-count">0</span></div>
					<div class="report-assignment-list"></div><div class="report-pager-assignments"></div>
				</section>
				<section class="review-section review-history-section" id="report-review-history">
					<div class="review-section-heading"><div><span class="review-step is-history">3</span><div><h3>${__("报工审核历史")}</h3><p>${__("按月份、员工或生产单据追溯每一次审核。")}</p></div></div><span class="review-count-badge report-history-count">0</span></div>
					<div class="review-filter-bar">
						<label class="review-filter-field"><span>${__("月份")}</span><input type="month" class="form-control report-history-month"></label>
						<div class="review-filter-field report-history-employee"></div>
						<div class="review-filter-field report-history-status"></div>
						<div class="review-filter-field report-history-work-order"></div>
						<div class="review-filter-actions"><button class="btn btn-primary report-history-search">${__("查询历史")}</button><button class="btn btn-default report-history-reset">${__("重置")}</button></div>
					</div>
					<div class="report-history-results"></div><div class="report-pager-history"></div>
				</section>
			</div>`);
		const $root = page.main.find(".report-review-page");
		const state = {
			data: { reports: [], assignments: [], processed_today: [], pagination: {} },
			pages: { reports: 1, assignments: 1 },
			pageLength: 20,
			history: { rows: [], pagination: { page: 1, page_length: 20 } },
			wageButtonsAdded: false,
		};
		page.report_review = { state, load };
		const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
		const number = (value) => format_number(flt(value), null, 2);
		const dateTime = (value) =>
			value ? frappe.datetime.str_to_user(normalizeFrappeDateTime(value)) : "-";
		const helpers = () => ({ translate: __, escapeHtml: esc, formatNumber: number, formatDateTime: dateTime });
		const employeeLabel = (row) => reviewEmployeeLabel(row);
		const documentLink = (doctype, name, label) => reviewDocumentLink(doctype, name, label, helpers());
		const currentMonth = () => String(frappe.datetime.get_today()).slice(0, 7);
		$root.find(".report-history-month").val(currentMonth());
		const historyControls = {
			employee: frappe.ui.form.make_control({
				parent: $root.find(".report-history-employee"),
				df: { fieldname: "employee", fieldtype: "Link", options: "Employee", label: __("员工") },
				render_input: true,
			}),
			status: frappe.ui.form.make_control({
				parent: $root.find(".report-history-status"),
				df: { fieldname: "status", fieldtype: "Select", label: __("审核结果"), options: [
					{ label: __("全部结果"), value: "" },
					{ label: __("已通过"), value: "Approved" },
					{ label: __("已驳回"), value: "Rejected" },
				] },
				render_input: true,
			}),
			workOrder: frappe.ui.form.make_control({
				parent: $root.find(".report-history-work-order"),
				df: { fieldname: "work_order", fieldtype: "Link", options: "Work Order", label: __("生产工单") },
				render_input: true,
			}),
		};

		function renderPager(listName) {
			$root.find(`.report-pager-${listName}`).html(
				reviewPaginationHtml(state.data.pagination?.[listName] || {}, listName, helpers())
			);
		}

		function renderQueue() {
			const rows = state.data.reports || [];
			const total = Number(state.data.pagination?.reports?.total_count || rows.length);
			$root.find(".report-kpi-pending, .report-pending-count").text(total);
			$root.find(".report-review-queue").html(
				rows.length
					? rows
							.map((row) => {
								const action = reviewActionState(row, __);
								const wage = row.wage_type === "Time"
									? `${number(row.reported_minutes)} ${__("分钟")} × ${number(row.rate)}/${__("小时")}`
									: `${number(row.completed_qty)} × ${number(row.rate)}/${__("件")}`;
								return `<article class="report-review-card is-pending" data-report="${esc(row.name)}">
									<div class="report-review-heading"><div><span class="review-card-kicker">${__("待审核")}</span><strong>${esc(employeeLabel(row))} · ${esc(row.operation)}</strong></div><span>${esc(dateTime(row.submitted_at))}</span></div>
									<div class="review-card-metrics"><div class="is-primary"><span>${__("申报数量")}</span><strong>${number(row.completed_qty)}</strong></div><div><span>${__("有效工时")}</span><strong>${number(row.actual_minutes)} <small>${__("分钟")}</small></strong></div><div><span>${__("计薪金额")}</span><strong>${number(row.wage_amount)}</strong></div></div>
									<div class="report-review-facts"><span>${__("生产任务单")}：${documentLink("Job Card", row.job_card, row.job_card)}</span><span>${__("生产工单")}：${documentLink("Work Order", row.work_order, row.work_order)}</span><span>${__("实际时间")}：${esc(dateTime(row.actual_start_time))} — ${esc(dateTime(row.actual_end_time))}</span><span>${__("计价")}：${esc(wage)}</span><span>${__("已通过/任务量")}：${number(row.job_card_completed_qty)} / ${number(row.for_quantity)}</span></div>
									${action.message ? `<div class="review-alert is-danger">${esc(action.message)}</div>` : ""}
									<div class="report-review-actions"><button class="btn btn-primary btn-sm report-approve" data-report="${esc(row.name)}" ${action.approve_disabled ? "disabled" : ""}>${__("通过")}</button><button class="btn btn-default btn-sm report-reject" data-report="${esc(row.name)}" ${action.reject_disabled ? "disabled" : ""}>${__("驳回")}</button><button class="btn btn-default btn-sm report-view-details" data-report="${esc(row.name)}">${__("查看明细")}</button></div>
								</article>`;
							})
							.join("")
					: `<div class="text-muted worker-reporting-empty">${__("当前没有待审核报工。")}</div>`
			);
		}

		function renderAssignments() {
			const rows = state.data.assignments || [];
			const total = Number(state.data.pagination?.assignments?.total_count || rows.length);
			$root.find(".report-kpi-assignments, .report-assignment-count").text(total);
			$root.find(".report-assignment-list").html(
				rows.length
					? `<div class="review-compact-card-list">${rows
							.map((row) => {
								const action = reviewAssignmentActionState(row, __);
								const control = action.action === "cancel_session"
									? `<button class="btn btn-xs btn-warning assignment-cancel-session" data-assignment="${esc(row.name)}">${esc(action.label)}</button>`
									: action.action === "unassign"
										? `<button class="btn btn-xs btn-default assignment-remove" data-assignment="${esc(row.name)}">${esc(action.label)}</button>`
										: "";
								const movement = Number(row.released_qty || 0) || Number(row.redispatched_qty || 0)
									? `<small>${__("原派")} ${number(row.original_assigned_qty)}${Number(row.released_qty || 0) ? ` · ${__("释放")} ${number(row.released_qty)}` : ""}${Number(row.redispatched_qty || 0) ? ` · ${__("转入")} ${number(row.redispatched_qty)}` : ""} · ${__("剩余")} ${number(row.remaining_qty)}</small>`
									: `<small>${__("剩余")} ${number(row.remaining_qty)}</small>`;
								const dispatchControl = `<button class="btn btn-xs btn-default assignment-open-plan" data-work-order="${esc(row.work_order)}">${__("查看/重新派工")}</button>`;
								return `<article class="review-compact-card"><div class="review-compact-main"><strong>${esc(employeeLabel(row))}</strong><span>${esc(row.operation || "-")}</span></div><div class="review-compact-metric"><span>${__("当前分配")}</span><strong>${number(row.effective_assigned_qty ?? row.assigned_qty)}</strong>${movement}</div><div class="review-compact-docs"><span>${__("任务单")} ${documentLink("Job Card", row.job_card, row.job_card)}</span><span>${__("工单")} ${documentLink("Work Order", row.work_order, row.work_order)}</span></div><div class="review-compact-action">${control}${dispatchControl}${action.message ? `<small>${esc(action.message)}</small>` : ""}</div></article>`;
							})
							.join("")}</div>`
					: `<div class="text-muted worker-reporting-empty">${__("当前没有活动派工。")}</div>`
			);
		}

		function renderHistory() {
			const rows = state.history.rows || [];
			const pagination = state.history.pagination || {};
			$root.find(".report-history-count").text(Number(pagination.total_count || 0));
			$root.find(".report-history-results").html(
				rows.length
					? `<div class="review-history-list">${rows
							.map((row) => {
								const meta = reviewStatusMeta(row.status, __);
								return `<article class="review-history-row report-details-row" data-report="${esc(row.name)}" tabindex="0"><div class="review-history-main"><strong>${esc(employeeLabel(row))} · ${esc(row.operation || "-")}</strong><span>${esc(dateTime(row.reviewed_at))}</span></div><div class="review-history-status"><span class="indicator-pill ${meta.indicator}">${esc(meta.label)}</span>${row.rejection_reason ? `<small>${esc(row.rejection_reason)}</small>` : ""}</div><div class="review-history-metric"><span>${__("数量")}</span><strong>${number(row.completed_qty)}</strong></div><div class="review-history-docs"><span>${documentLink("Job Card", row.job_card, row.job_card)}</span><span>${documentLink("Work Order", row.work_order, row.work_order)}</span></div><button class="btn btn-default btn-sm report-view-details" data-report="${esc(row.name)}">${__("查看明细")}</button></article>`;
							})
							.join("")}</div>`
					: `<div class="text-muted worker-reporting-empty">${__("没有符合条件的报工审核记录。")}</div>`
			);
			$root.find(".report-pager-history").html(
				reviewPaginationHtml(pagination, "history", helpers())
			);
		}

		function addWageButtons() {
			if (!state.data.can_manage_wages || state.wageButtonsAdded) return;
			state.wageButtonsAdded = true;
			page.add_inner_button(__("计价规则"), () => frappe.set_route("List", "Operation Wage Rate"), __("工资管理"));
			page.add_inner_button(__("月度汇总"), () => frappe.set_route("List", "Monthly Worker Wage Summary"), __("工资管理"));
			page.add_inner_button(__("生成月度汇总"), openBuildSummaryDialog, __("工资管理"));
		}

		function render() {
			renderQueue();
			renderAssignments();
			$root.find(".report-kpi-processed").text(
				Number(state.data.pagination?.processed_today?.total_count || 0)
			);
			renderPager("reports");
			renderPager("assignments");
			addWageButtons();
		}

		function load() {
			return frappe.call({
				method: "process_simplification.api.production_reporting.get_review_dashboard",
				args: {
					pending_page: state.pages.reports,
					assignment_page: state.pages.assignments,
					processed_page: state.pages.processed_today,
					page_length: state.pageLength,
				},
				freeze: true,
				freeze_message: __("正在读取待审核报工..."),
			}).then((response) => {
				state.data = response.message || { reports: [], assignments: [], processed_today: [], pagination: {} };
				for (const listName of Object.keys(state.pages)) {
					state.pages[listName] = Number(state.data.pagination?.[listName]?.page || state.pages[listName]);
				}
				render();
			}).then(() => loadHistory(state.history.pagination.page || 1));
		}

		function loadHistory(pageNumber = 1) {
			const range = reviewMonthRange($root.find(".report-history-month").val());
			$root.find(".report-history-results").addClass("is-loading");
			return Promise.resolve(frappe.call({
				method: "process_simplification.api.production_reporting.get_review_history",
				args: {
					page: pageNumber,
					page_length: state.history.pagination.page_length || 20,
					employee: historyControls.employee.get_value(),
					status: historyControls.status.get_value(),
					work_order: historyControls.workOrder.get_value(),
					...range,
				},
			})).then((response) => {
				state.history.rows = response.message?.rows || [];
				state.history.pagination = response.message?.pagination || { page: 1, page_length: 20 };
				renderHistory();
			}).finally(() => $root.find(".report-history-results").removeClass("is-loading"));
		}

		function setDialogBusy(dialog, busy, label) {
			dialog.get_primary_btn().prop("disabled", busy).text(busy ? __("处理中...") : label);
		}

		function routeToDocument(event) {
			event.preventDefault();
			event.stopPropagation();
			const $link = $(event.currentTarget);
			return frappe.set_route("Form", $link.data("doctype"), $link.data("name"));
		}

		function openReportDetails(row) {
			if (!row) return;
			const dialog = new frappe.ui.Dialog({
				title: __("报工审核明细"),
				size: "large",
				fields: [{ fieldtype: "HTML", options: reviewDetailsHtml(row, helpers()) }],
			});
			dialog.$wrapper.on("click", ".report-document-link", routeToDocument);
			dialog.show();
		}

		function focusHistory() {
			$root.find("#report-review-history")[0]?.scrollIntoView({ behavior: "smooth", block: "start" });
			$root.find(".report-history-month").trigger("focus");
		}

		function openApproveDialog(row) {
			const dialog = new frappe.ui.Dialog({
				title: __("确认通过报工"),
					fields: [{ fieldtype: "HTML", options: `<p>${__("通过后将立即把以下生产数量和有效计时段写入 Job Card，且不能在审核时修改：")}</p><p><strong>${esc(employeeLabel(row))} · ${esc(row.operation)}</strong><br>${__("数量")}：${number(row.completed_qty)}<br>${__("实际时间")}：${esc(dateTime(row.actual_start_time))} — ${esc(dateTime(row.actual_end_time))}（${number(row.actual_minutes)} ${__("分钟，不含暂停")}）<br>${row.wage_type === "Time" ? `${__("计薪时间")}：${number(row.reported_minutes)} ${__("分钟")}（${row.manual_time_entry ? __("工人手工填写") : __("计时器有效分钟")}）<br>` : ""}${__("计薪金额")}：${number(row.wage_amount)}</p>` }],
				primary_action_label: __("确认通过"),
				primary_action: async () => {
					setDialogBusy(dialog, true, __("确认通过"));
					try {
						await frappe.call({ method: "process_simplification.api.production_reporting.approve_work_report", type: "POST", args: { report: row.name } });
						dialog.hide();
						frappe.show_alert({ message: __("报工已通过并写入 Job Card。"), indicator: "green" });
						await load();
					} finally {
						setDialogBusy(dialog, false, __("确认通过"));
					}
				},
			});
			dialog.show();
		}

		function openRejectDialog(row) {
			const dialog = new frappe.ui.Dialog({
				title: __("驳回报工"),
				fields: [{ fieldname: "reason", fieldtype: "Small Text", label: __("驳回原因"), reqd: 1 }],
				primary_action_label: __("确认驳回"),
				primary_action: async (values) => {
					setDialogBusy(dialog, true, __("确认驳回"));
					try {
						await frappe.call({ method: "process_simplification.api.production_reporting.reject_work_report", type: "POST", args: { report: row.name, reason: values.reason } });
						dialog.hide();
						await load();
					} finally {
						setDialogBusy(dialog, false, __("确认驳回"));
					}
				},
			});
			dialog.show();
		}

		function openAssignmentDialog() {
			return window.process_simplification.open_worker_assignment_dialog({ on_success: load });
		}

		function openUnassignDialog(row) {
			const dialog = new frappe.ui.Dialog({
				title: __("确认取消派工"),
				fields: [{ fieldtype: "HTML", options: `<p>${__("仅没有任何报工记录的派工可以取消。")}</p><p><strong>${esc(employeeLabel(row))} · ${esc(row.job_card)}</strong></p>` }],
				primary_action_label: __("取消派工"),
				primary_action: async () => {
					setDialogBusy(dialog, true, __("取消派工"));
					try {
						await frappe.call({ method: "process_simplification.api.production_reporting.unassign_worker", type: "POST", args: { assignment: row.name } });
						dialog.hide();
						await load();
					} finally {
						setDialogBusy(dialog, false, __("取消派工"));
					}
				},
			});
			dialog.show();
		}

		function openCancelSessionDialog(row) {
			const dialog = new frappe.ui.Dialog({
				title: __("取消工人活动计时"),
				fields: [{ fieldtype: "HTML", options: `<p>${__("只删除尚未提交的活动计时，不会产生 Job Card 数量或工资。")}</p><p><strong>${esc(employeeLabel(row))} · ${esc(row.job_card)}</strong><br>${__("开始时间")}：${esc(dateTime(row.active_started_at))}</p>` }],
				primary_action_label: __("确认取消计时"),
				primary_action: async () => {
					setDialogBusy(dialog, true, __("确认取消计时"));
					try {
						await frappe.call({ method: "process_simplification.api.production_reporting.cancel_work_session", type: "POST", args: { report: row.active_report } });
						dialog.hide();
						frappe.show_alert({ message: __("活动计时已取消。"), indicator: "orange" });
						await load();
					} finally {
						setDialogBusy(dialog, false, __("确认取消计时"));
					}
				},
			});
			dialog.show();
		}

		function openBuildSummaryDialog() {
			const companies = state.data.companies || [];
			const dialog = new frappe.ui.Dialog({
				title: __("生成月度工资汇总"),
				fields: [
					{ fieldname: "company", fieldtype: "Select", label: __("公司"), options: companies.join("\n"), default: companies[0], reqd: 1 },
					{ fieldname: "month_start", fieldtype: "Date", label: __("月份"), default: frappe.datetime.month_start(), reqd: 1 },
					{ fieldname: "employee", fieldtype: "Link", options: "Employee", label: __("工人（留空为全部）"), get_query: () => ({ query: "process_simplification.api.production_reporting.search_wage_employees", filters: { company: dialog.get_value("company") } }) },
				],
				primary_action_label: __("生成草稿"),
				primary_action: async (values) => {
					setDialogBusy(dialog, true, __("生成草稿"));
					try {
						const response = await frappe.call({ method: "process_simplification.api.production_reporting.build_monthly_summaries", type: "POST", args: values });
						dialog.hide();
						const count = (response.message?.summaries || []).length;
						frappe.show_alert({ message: __("已生成 {0} 张月度汇总草稿。", [count]), indicator: "green" });
						frappe.set_route("List", "Monthly Worker Wage Summary");
					} finally {
						setDialogBusy(dialog, false, __("生成草稿"));
					}
				},
			});
			dialog.show();
		}

		$root.on("click", ".report-approve", (event) => {
			const row = (state.data.reports || []).find((item) => item.name === $(event.currentTarget).data("report"));
			if (row?.can_approve !== false && !row?.capacity_conflict) openApproveDialog(row);
		});
			$root.on("click", ".report-reject", (event) => {
				const row = (state.data.reports || []).find((item) => item.name === $(event.currentTarget).data("report"));
				if (row?.can_reject !== false) openRejectDialog(row);
			});
		$root.on("click", ".assignment-remove", (event) => {
			const row = (state.data.assignments || []).find((item) => item.name === $(event.currentTarget).data("assignment"));
			if (row?.can_unassign) openUnassignDialog(row);
		});
		$root.on("click", ".assignment-cancel-session", (event) => {
			const row = (state.data.assignments || []).find((item) => item.name === $(event.currentTarget).data("assignment"));
			if (row?.active_report) openCancelSessionDialog(row);
		});
		$root.on("click", ".assignment-open-plan", (event) => {
			const workOrder = String($(event.currentTarget).data("work-order") || "");
			if (workOrder && window.process_simplification?.open_worker_assignment_dialog) {
				window.process_simplification.open_worker_assignment_dialog({
					work_order: workOrder,
					on_success: load,
				});
			}
		});
		$root.on("click", ".report-document-link", routeToDocument);
		$root.on("click", ".report-view-details", (event) => {
			event.stopPropagation();
			const report = $(event.currentTarget).data("report");
			const row = [...(state.data.reports || []), ...(state.history.rows || [])]
				.find((item) => item.name === report);
			openReportDetails(row);
		});
		$root.on("click", ".report-details-row", (event) => {
			if ($(event.target).closest(".report-document-link, button").length) return;
			const report = $(event.currentTarget).data("report");
			openReportDetails((state.history.rows || []).find((row) => row.name === report));
		});
		$root.on("keydown", ".report-details-row", (event) => {
			if (event.key === "Enter" || event.key === " ") $(event.currentTarget).trigger("click");
		});
		$root.on("click", ".report-page-action", (event) => {
			const listName = $(event.currentTarget).data("list");
			if (listName === "history") {
				loadHistory(Number($(event.currentTarget).data("page") || 1));
				return;
			}
			if (!Object.prototype.hasOwnProperty.call(state.pages, listName)) return;
			state.pages[listName] = Number($(event.currentTarget).data("page") || 1);
			load();
		});
		$root.on("change", ".report-page-size", (event) => {
			if ($(event.currentTarget).data("list") === "history") {
				state.history.pagination.page_length = Number($(event.currentTarget).val() || 20);
				loadHistory(1);
				return;
			}
			state.pageLength = Number($(event.currentTarget).val() || 20);
			state.pages = { reports: 1, assignments: 1 };
			load();
		});
		$root.on("click", ".report-history-search", () => loadHistory(1));
		$root.on("click", ".report-history-reset", async () => {
			$root.find(".report-history-month").val(currentMonth());
			await Promise.all([
				historyControls.employee.set_value(""),
				historyControls.status.set_value(""),
				historyControls.workOrder.set_value(""),
			]);
			await loadHistory(1);
		});
		page.add_inner_button(__("新增派工"), openAssignmentDialog);
		page.add_inner_button(__("查看历史"), focusHistory);
		page.add_inner_button(__("刷新"), () => runReviewToolbarLoad(load));
	};

	frappe.pages["production-report-review"].refresh = function (wrapper) {
		return wrapper.page?.report_review?.load?.();
	};
}
