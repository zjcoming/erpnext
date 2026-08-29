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
		message: assignment.unassign_block_message || translate("已有报工记录"),
	};
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
	normalizeFrappeDateTime,
	runReviewToolbarLoad,
};
if (typeof module !== "undefined" && module.exports) module.exports = productionReportReviewApi;

if (typeof frappe !== "undefined") {
	frappe.pages["production-report-review"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({ parent: wrapper, title: __("报工审核"), single_column: true });
		page.main.html(`
			<div class="process-simplification-page report-review-page">
				<section><h4>${__("待审核报工")}</h4><div class="report-review-queue"></div></section>
				<section><h4>${__("当前派工")}</h4><div class="report-assignment-list"></div></section>
				<section><h4>${__("今日已处理")}</h4><div class="report-processed-list"></div></section>
			</div>`);
		const $root = page.main.find(".report-review-page");
		const state = { data: { reports: [], assignments: [], processed_today: [] }, wageButtonsAdded: false };
		page.report_review = { state, load };
		const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
		const number = (value) => format_number(flt(value), null, 2);
		const dateTime = (value) =>
			value ? frappe.datetime.str_to_user(normalizeFrappeDateTime(value)) : "-";

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
									<div class="report-review-heading"><strong>${esc(row.employee)} · ${esc(row.operation)}</strong><span>${esc(dateTime(row.submitted_at))}</span></div>
									<div class="report-review-facts"><span>${__("Job Card")}：${esc(row.job_card)}</span><span>${__("生产工单")}：${esc(row.work_order)}</span><span>${__("申报数量")}：${number(row.completed_qty)}</span><span>${__("实际开始")}：${esc(dateTime(row.actual_start_time))}</span><span>${__("实际结束")}：${esc(dateTime(row.actual_end_time))}</span><span>${__("实际分钟")}：${number(row.actual_minutes)}</span><span>${__("计价")}：${esc(wage)}</span><span>${__("金额")}：${number(row.wage_amount)}</span><span>${__("已通过/任务量")}：${number(row.job_card_completed_qty)} / ${number(row.for_quantity)}</span></div>
									${action.message ? `<p class="text-danger">${esc(action.message)}</p>` : ""}
									<div class="report-review-actions"><button class="btn btn-primary btn-sm report-approve" data-report="${esc(row.name)}" ${action.approve_disabled ? "disabled" : ""}>${__("通过")}</button><button class="btn btn-default btn-sm report-reject" data-report="${esc(row.name)}" ${action.reject_disabled ? "disabled" : ""}>${__("驳回")}</button></div>
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
					? `<div class="table-responsive"><table class="table table-bordered"><thead><tr><th>${__("工人")}</th><th>${__("Job Card")}</th><th>${__("工序")}</th><th>${__("主管")}</th><th></th></tr></thead><tbody>${rows
							.map((row) => {
								const action = reviewAssignmentActionState(row, __);
								const control = action.action === "cancel_session"
									? `<button class="btn btn-xs btn-warning assignment-cancel-session" data-assignment="${esc(row.name)}">${esc(action.label)}</button>`
									: action.action === "unassign"
										? `<button class="btn btn-xs btn-default assignment-remove" data-assignment="${esc(row.name)}">${esc(action.label)}</button>`
										: "";
								return `<tr><td>${esc(row.employee)}</td><td>${esc(row.job_card)}</td><td>${esc(row.operation)}</td><td>${esc(row.supervisor)}</td><td>${control}${action.message ? `<div class="text-muted small">${esc(action.message)}</div>` : ""}</td></tr>`;
							})
							.join("")}</tbody></table></div>`
					: `<div class="text-muted worker-reporting-empty">${__("当前没有活动派工。")}</div>`
			);
		}

		function renderProcessed() {
			const rows = state.data.processed_today || [];
			$root.find(".report-processed-list").html(
				rows.length
					? `<div class="table-responsive"><table class="table table-bordered"><thead><tr><th>${__("时间")}</th><th>${__("工人")}</th><th>${__("工序")}</th><th>${__("数量")}</th><th>${__("状态")}</th></tr></thead><tbody>${rows
							.map((row) => {
								const meta = reviewStatusMeta(row.status, __);
								return `<tr><td>${esc(dateTime(row.reviewed_at))}</td><td>${esc(row.employee)}</td><td>${esc(row.operation)}</td><td>${number(row.completed_qty)}</td><td><span class="indicator-pill ${meta.indicator}">${esc(meta.label)}</span>${row.rejection_reason ? `<div class="small text-muted">${esc(row.rejection_reason)}</div>` : ""}</td></tr>`;
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
			addWageButtons();
		}

		function load() {
			return frappe.call({
				method: "process_simplification.api.production_reporting.get_review_dashboard",
				freeze: true,
				freeze_message: __("正在读取待审核报工..."),
			}).then((response) => {
				state.data = response.message || { reports: [], assignments: [], processed_today: [] };
				render();
			});
		}

		function setDialogBusy(dialog, busy, label) {
			dialog.get_primary_btn().prop("disabled", busy).text(busy ? __("处理中...") : label);
		}

		function openApproveDialog(row) {
			const dialog = new frappe.ui.Dialog({
				title: __("确认通过报工"),
				fields: [{ fieldtype: "HTML", options: `<p>${__("通过后将立即把以下生产数量和实际计时写入 Job Card，且不能在审核时修改：")}</p><p><strong>${esc(row.employee)} · ${esc(row.operation)}</strong><br>${__("数量")}：${number(row.completed_qty)}<br>${__("时间")}：${esc(dateTime(row.actual_start_time))} — ${esc(dateTime(row.actual_end_time))}（${number(row.actual_minutes)} ${__("分钟")}）<br>${__("计薪金额")}：${number(row.wage_amount)}</p>` }],
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
			const dialog = new frappe.ui.Dialog({
				title: __("新增工人派工"),
				fields: [
					{ fieldname: "job_card", fieldtype: "Link", options: "Job Card", label: __("Job Card"), reqd: 1, onchange: () => dialog.set_value("employee", ""), get_query: () => ({ query: "process_simplification.api.production_reporting.search_draft_job_cards" }) },
					{ fieldname: "employee", fieldtype: "Link", options: "Employee", label: __("工人"), reqd: 1, get_query: () => ({ query: "process_simplification.api.production_reporting.search_workers", filters: { job_card: dialog.get_value("job_card") } }) },
					{ fieldname: "notes", fieldtype: "Small Text", label: __("派工备注") },
				],
				primary_action_label: __("确认派工"),
				primary_action: async (values) => {
					setDialogBusy(dialog, true, __("确认派工"));
					try {
						await frappe.call({ method: "process_simplification.api.production_reporting.assign_worker", type: "POST", args: values });
						dialog.hide();
						frappe.show_alert({ message: __("派工已创建。"), indicator: "green" });
						await load();
					} finally {
						setDialogBusy(dialog, false, __("确认派工"));
					}
				},
			});
			dialog.show();
		}

		function openUnassignDialog(row) {
			const dialog = new frappe.ui.Dialog({
				title: __("确认取消派工"),
				fields: [{ fieldtype: "HTML", options: `<p>${__("仅没有任何报工记录的派工可以取消。")}</p><p><strong>${esc(row.employee)} · ${esc(row.job_card)}</strong></p>` }],
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
				fields: [{ fieldtype: "HTML", options: `<p>${__("只删除尚未提交的活动计时，不会产生 Job Card 数量或工资。")}</p><p><strong>${esc(row.employee)} · ${esc(row.job_card)}</strong><br>${__("开始时间")}：${esc(dateTime(row.active_started_at))}</p>` }],
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
		page.add_inner_button(__("新增派工"), openAssignmentDialog);
		page.add_inner_button(__("刷新"), () => runReviewToolbarLoad(load));
	};

	frappe.pages["production-report-review"].refresh = function (wrapper) {
		return wrapper.page?.report_review?.load?.();
	};
}
