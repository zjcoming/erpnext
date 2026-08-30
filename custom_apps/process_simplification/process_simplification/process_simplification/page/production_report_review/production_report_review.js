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
		message: translate("已有报工历史，不能取消派工。"),
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
	runReviewToolbarLoad,
};
if (typeof module !== "undefined" && module.exports) module.exports = productionReportReviewApi;

if (typeof frappe !== "undefined") {
	frappe.pages["production-report-review"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({ parent: wrapper, title: __("报工审核"), single_column: true });
		page.main.html(`
			<div class="process-simplification-page report-review-page">
				<section><h4>${__("待审核报工")}</h4><div class="report-review-queue"></div><div class="report-pager-reports"></div></section>
				<section><h4>${__("当前派工")}</h4><div class="report-assignment-list"></div><div class="report-pager-assignments"></div></section>
				<section><h4>${__("今日已处理")}</h4><div class="report-processed-list"></div><div class="report-pager-processed_today"></div></section>
			</div>`);
		const $root = page.main.find(".report-review-page");
		const state = {
			data: { reports: [], assignments: [], processed_today: [], pagination: {} },
			pages: { reports: 1, assignments: 1, processed_today: 1 },
			pageLength: 20,
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

		function renderPager(listName) {
			$root.find(`.report-pager-${listName}`).html(
				reviewPaginationHtml(state.data.pagination?.[listName] || {}, listName, helpers())
			);
		}

		function renderQueue() {
			const rows = state.data.reports || [];
			$root.find(".report-review-queue").html(
				rows.length
					? rows
							.map((row) => {
								const action = reviewActionState(row, __);
								const wage = row.wage_type === "Time"
									? `${number(row.reported_minutes)} ${__("分钟")} × ${number(row.rate)}/${__("小时")}`
									: `${number(row.completed_qty)} × ${number(row.rate)}/${__("件")}`;
								return `<article class="report-review-card" data-report="${esc(row.name)}">
									<div class="report-review-heading"><strong>${esc(employeeLabel(row))} · ${esc(row.operation)}</strong><span>${esc(dateTime(row.submitted_at))}</span></div>
									<div class="report-review-facts"><span>${__("生产任务单")}：${documentLink("Job Card", row.job_card, row.job_card)}</span><span>${__("生产工单")}：${documentLink("Work Order", row.work_order, row.work_order)}</span><span>${__("申报数量")}：${number(row.completed_qty)}</span><span>${__("实际开始")}：${esc(dateTime(row.actual_start_time))}</span><span>${__("实际结束")}：${esc(dateTime(row.actual_end_time))}</span><span>${__("实际分钟")}：${number(row.actual_minutes)}</span><span>${__("计价")}：${esc(wage)}</span><span>${__("金额")}：${number(row.wage_amount)}</span><span>${__("已通过/任务量")}：${number(row.job_card_completed_qty)} / ${number(row.for_quantity)}</span></div>
									${action.message ? `<p class="text-danger">${esc(action.message)}</p>` : ""}
									<div class="report-review-actions"><button class="btn btn-primary btn-sm report-approve" data-report="${esc(row.name)}" ${action.approve_disabled ? "disabled" : ""}>${__("通过")}</button><button class="btn btn-default btn-sm report-reject" data-report="${esc(row.name)}" ${action.reject_disabled ? "disabled" : ""}>${__("驳回")}</button><button class="btn btn-default btn-sm report-view-details" data-report="${esc(row.name)}">${__("查看明细")}</button></div>
								</article>`;
							})
							.join("")
					: `<div class="text-muted worker-reporting-empty">${__("当前没有待审核报工。")}</div>`
			);
		}

		function renderAssignments() {
			const rows = state.data.assignments || [];
			$root.find(".report-assignment-list").html(
				rows.length
					? `<div class="table-responsive"><table class="table table-bordered"><thead><tr><th>${__("工人")}</th><th>${__("生产任务单")}</th><th>${__("生产工单")}</th><th>${__("工序")}</th><th>${__("主管")}</th><th></th></tr></thead><tbody>${rows
							.map((row) => {
								const action = reviewAssignmentActionState(row, __);
								const control = action.action === "cancel_session"
									? `<button class="btn btn-xs btn-warning assignment-cancel-session" data-assignment="${esc(row.name)}">${esc(action.label)}</button>`
									: action.action === "unassign"
										? `<button class="btn btn-xs btn-default assignment-remove" data-assignment="${esc(row.name)}">${esc(action.label)}</button>`
										: "";
								return `<tr><td>${esc(employeeLabel(row))}</td><td>${documentLink("Job Card", row.job_card, row.job_card)}</td><td>${documentLink("Work Order", row.work_order, row.work_order)}</td><td>${esc(row.operation)}</td><td>${esc(row.supervisor)}</td><td>${control}${action.message ? `<div class="text-muted small">${esc(action.message)}</div>` : ""}</td></tr>`;
							})
							.join("")}</tbody></table></div>`
					: `<div class="text-muted worker-reporting-empty">${__("当前没有活动派工。")}</div>`
			);
		}

		function renderProcessed() {
			const rows = state.data.processed_today || [];
			$root.find(".report-processed-list").html(
				rows.length
					? `<div class="table-responsive"><table class="table table-bordered"><thead><tr><th>${__("时间")}</th><th>${__("工人")}</th><th>${__("生产任务单")}</th><th>${__("工序")}</th><th>${__("数量")}</th><th>${__("状态")}</th><th></th></tr></thead><tbody>${rows
							.map((row) => {
								const meta = reviewStatusMeta(row.status, __);
								return `<tr class="report-details-row" data-report="${esc(row.name)}" tabindex="0"><td>${esc(dateTime(row.reviewed_at))}</td><td>${esc(employeeLabel(row))}</td><td>${documentLink("Job Card", row.job_card, row.job_card)}</td><td>${esc(row.operation)}</td><td>${number(row.completed_qty)}</td><td><span class="indicator-pill ${meta.indicator}">${esc(meta.label)}</span>${row.rejection_reason ? `<div class="small text-muted">${esc(row.rejection_reason)}</div>` : ""}</td><td><button class="btn btn-default btn-xs report-view-details" data-report="${esc(row.name)}">${__("明细")}</button></td></tr>`;
							})
							.join("")}</tbody></table></div>`
					: `<div class="text-muted worker-reporting-empty">${__("今天还没有已处理报工。")}</div>`
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
			renderProcessed();
			renderPager("reports");
			renderPager("assignments");
			renderPager("processed_today");
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
			});
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

		function openHistoryDialog() {
			const historyState = { rows: [], pagination: { page: 1, page_length: 20 } };
			const dialog = new frappe.ui.Dialog({
				title: __("历史报工审核"),
				size: "extra-large",
				fields: [
					{ fieldname: "status", fieldtype: "Select", label: __("审核结果"), options: [
						{ label: __("全部"), value: "" },
						{ label: __("已通过"), value: "Approved" },
						{ label: __("已驳回"), value: "Rejected" },
					] },
					{ fieldtype: "Column Break" },
					{ fieldname: "employee", fieldtype: "Link", options: "Employee", label: __("工人") },
					{ fieldtype: "Column Break" },
					{ fieldname: "work_order", fieldtype: "Link", options: "Work Order", label: __("生产工单") },
					{ fieldtype: "Section Break" },
					{ fieldname: "job_card", fieldtype: "Link", options: "Job Card", label: __("生产任务单") },
					{ fieldtype: "Column Break" },
					{ fieldname: "from_date", fieldtype: "Date", label: __("审核开始日期") },
					{ fieldtype: "Column Break" },
					{ fieldname: "to_date", fieldtype: "Date", label: __("审核结束日期") },
					{ fieldtype: "Column Break" },
					{ fieldname: "page_length", fieldtype: "Select", label: __("每页条数"), options: "20\n50\n100", default: "20" },
					{ fieldtype: "Section Break" },
					{ fieldname: "history_results", fieldtype: "HTML" },
				],
				primary_action_label: __("查询"),
				primary_action: () => loadHistory(1),
			});

			function renderHistory() {
				const rows = historyState.rows || [];
				const table = rows.length
					? `<div class="table-responsive"><table class="table table-bordered"><thead><tr><th>${__("审核时间")}</th><th>${__("工人")}</th><th>${__("生产任务单")}</th><th>${__("生产工单")}</th><th>${__("工序")}</th><th>${__("数量")}</th><th>${__("状态")}</th><th></th></tr></thead><tbody>${rows.map((row) => {
						const meta = reviewStatusMeta(row.status, __);
						return `<tr class="history-details-row" data-report="${esc(row.name)}" tabindex="0"><td>${esc(dateTime(row.reviewed_at))}</td><td>${esc(employeeLabel(row))}</td><td>${documentLink("Job Card", row.job_card, row.job_card)}</td><td>${documentLink("Work Order", row.work_order, row.work_order)}</td><td>${esc(row.operation)}</td><td>${number(row.completed_qty)}</td><td><span class="indicator-pill ${meta.indicator}">${esc(meta.label)}</span></td><td><button class="btn btn-default btn-xs history-view-details" data-report="${esc(row.name)}">${__("明细")}</button></td></tr>`;
					}).join("")}</tbody></table></div>`
					: `<div class="text-muted worker-reporting-empty">${__("没有符合条件的历史审核记录。")}</div>`;
				dialog.fields_dict.history_results.$wrapper.html(
					table + reviewPaginationHtml(historyState.pagination || {}, "history", helpers())
				);
			}

			function loadHistory(pageNumber) {
				const values = dialog.get_values() || {};
				dialog.get_primary_btn().prop("disabled", true).text(__("查询中..."));
				return Promise.resolve(frappe.call({
					method: "process_simplification.api.production_reporting.get_review_history",
					args: { ...values, page: pageNumber || 1 },
				})).then((response) => {
					historyState.rows = response.message?.rows || [];
					historyState.pagination = response.message?.pagination || {
						page: 1,
						page_length: Number(values.page_length || 20),
					};
					renderHistory();
				}).finally(() => dialog.get_primary_btn().prop("disabled", false).text(__("查询")));
			}

			dialog.$wrapper.on("click", ".report-document-link", routeToDocument);
			dialog.$wrapper.on("click", ".history-view-details", (event) => {
				event.stopPropagation();
				const report = $(event.currentTarget).data("report");
				openReportDetails(historyState.rows.find((row) => row.name === report));
			});
			dialog.$wrapper.on("click", ".history-details-row", (event) => {
				if ($(event.target).closest(".report-document-link").length) return;
				const report = $(event.currentTarget).data("report");
				openReportDetails(historyState.rows.find((row) => row.name === report));
			});
			dialog.$wrapper.on("keydown", ".history-details-row", (event) => {
				if (event.key === "Enter" || event.key === " ") $(event.currentTarget).trigger("click");
			});
			dialog.$wrapper.on("click", ".report-page-action[data-list='history']", (event) => {
				loadHistory(Number($(event.currentTarget).data("page") || 1));
			});
			dialog.$wrapper.on("change", ".report-page-size[data-list='history']", (event) => {
				dialog.set_value("page_length", String($(event.currentTarget).val() || 20));
				loadHistory(1);
			});
			dialog.show();
			loadHistory(1);
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
		$root.on("click", ".report-document-link", routeToDocument);
		$root.on("click", ".report-view-details", (event) => {
			event.stopPropagation();
			const report = $(event.currentTarget).data("report");
			const row = [...(state.data.reports || []), ...(state.data.processed_today || [])]
				.find((item) => item.name === report);
			openReportDetails(row);
		});
		$root.on("click", ".report-details-row", (event) => {
			if ($(event.target).closest(".report-document-link, button").length) return;
			const report = $(event.currentTarget).data("report");
			openReportDetails((state.data.processed_today || []).find((row) => row.name === report));
		});
		$root.on("keydown", ".report-details-row", (event) => {
			if (event.key === "Enter" || event.key === " ") $(event.currentTarget).trigger("click");
		});
		$root.on("click", ".report-page-action", (event) => {
			const listName = $(event.currentTarget).data("list");
			if (!Object.prototype.hasOwnProperty.call(state.pages, listName)) return;
			state.pages[listName] = Number($(event.currentTarget).data("page") || 1);
			load();
		});
		$root.on("change", ".report-page-size", (event) => {
			state.pageLength = Number($(event.currentTarget).val() || 20);
			state.pages = { reports: 1, assignments: 1, processed_today: 1 };
			load();
		});
		page.add_inner_button(__("新增派工"), openAssignmentDialog);
		page.add_inner_button(__("历史审核"), openHistoryDialog);
		page.add_inner_button(__("刷新"), () => runReviewToolbarLoad(load));
	};

	frappe.pages["production-report-review"].refresh = function (wrapper) {
		return wrapper.page?.report_review?.load?.();
	};
}
