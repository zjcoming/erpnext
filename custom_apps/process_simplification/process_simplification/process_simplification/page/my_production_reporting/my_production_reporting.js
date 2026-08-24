function workReportStatusMeta(status, translate = (message) => message) {
	const statuses = {
		"In Progress": { label: translate("计时中"), indicator: "blue" },
		"Pending Approval": { label: translate("待审核"), indicator: "orange" },
		Approved: { label: translate("已通过"), indicator: "green" },
		Rejected: { label: translate("已驳回"), indicator: "red" },
	};
	return statuses[status] || { label: status || translate("未知"), indicator: "gray" };
}

function workReportButtonMeta(assignment, translate = (message) => message) {
	if (assignment.active_report) {
		return { action: "finish", label: translate("结束并报工"), disabled: false };
	}
	if (assignment.can_start) {
		return { action: "start", label: translate("开始计时"), disabled: false };
	}
	const labels = {
		PENDING_REPORT: translate("待主管审核"),
		NO_REMAINING_QTY: translate("已完成"),
		RATE_MISSING: translate("缺少计价规则"),
		DAILY_MINUTES_LIMIT: translate("今日工时已满"),
		JOB_CARD_UNAVAILABLE: translate("不可报工"),
		TIME_LOG_SETTING: translate("配置不兼容"),
		PROCESS_LOSS: translate("需主管处理"),
	};
	return { action: null, label: labels[assignment.block_code] || translate("不可报工"), disabled: true };
}

function workReportAmount(wageType, qty, minutes, rate) {
	return wageType === "Time" ? (Number(minutes || 0) / 60) * Number(rate || 0) : Number(qty || 0) * Number(rate || 0);
}

function workReportRemainingMinutes(assignment) {
	return Math.max(0, Number(assignment.daily_minutes_limit || 0) - Number(assignment.daily_minutes_used || 0));
}

function workReportWageLabel(assignment, format = (value) => String(value), translate = (message) => message) {
	if (!assignment.wage_type) return translate("未配置计价规则");
	return assignment.wage_type === "Time"
		? translate("计时 · {0}/小时", [format(assignment.rate)])
		: translate("计件 · {0}/件", [format(assignment.rate)]);
}

const myProductionReportingApi = {
	workReportStatusMeta,
	workReportButtonMeta,
	workReportAmount,
	workReportRemainingMinutes,
	workReportWageLabel,
};
if (typeof module !== "undefined" && module.exports) module.exports = myProductionReportingApi;

if (typeof frappe !== "undefined") {
	frappe.pages["my-production-reporting"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({ parent: wrapper, title: __("我的报工"), single_column: true });
		page.main.html(`
			<div class="process-simplification-page worker-reporting-page">
				<div class="worker-reporting-summary"></div>
				<h4>${__("当前派工")}</h4>
				<div class="worker-assignment-list"></div>
				<h4>${__("最近报工")}</h4>
				<div class="worker-report-history"></div>
			</div>`);

		const $root = page.main.find(".worker-reporting-page");
		const state = { data: { assignments: [], reports: [] } };
		page.worker_reporting = { state, load };
		const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
		const number = (value) => format_number(flt(value), null, 2);

		function renderAssignments() {
			const rows = state.data.assignments || [];
			$root.find(".worker-assignment-list").html(
				rows.length
					? rows
							.map((row) => {
								const button = workReportButtonMeta(row, __);
								const wageLabel = workReportWageLabel(row, number, __);
								const remainingMinutes = workReportRemainingMinutes(row);
								return `<article class="worker-assignment-card">
									<div><strong>${esc(row.operation)}</strong><span>${esc(row.production_item || "")}</span></div>
									<div class="worker-assignment-facts">
										<span>${__("Job Card")}：${esc(row.job_card)}</span>
										<span>${__("生产工单")}：${esc(row.work_order)}</span>
										<span>${__("工作站")}：${esc(row.workstation || "-")}</span>
										<span>${__("计价")}：${esc(wageLabel)}</span>
										<span>${__("已通过/任务量")}：${number(row.completed_qty)} / ${number(row.for_quantity)}</span>
										<span>${__("当前可报")}：${number(row.reportable_qty)}</span>
										${row.active_started_at ? `<span>${__("开始时间")}：${esc(frappe.datetime.str_to_user(row.active_started_at))}</span>` : ""}
										${row.wage_type === "Time" ? `<span>${__("今日剩余计薪分钟")}：${number(remainingMinutes)}</span>` : ""}
									</div>
									${row.notes ? `<p class="text-muted">${esc(row.notes)}</p>` : ""}
									${row.block_message ? `<p class="text-muted">${esc(row.block_message)}</p>` : ""}
									<button class="btn btn-primary btn-sm worker-report-action" data-assignment="${esc(row.name)}" ${button.disabled ? "disabled" : ""}>${esc(button.label)}</button>
								</article>`;
							})
							.join("")
					: `<div class="text-muted worker-reporting-empty">${__("当前没有可报工的派工任务。")}</div>`
			);
		}

		function renderReports() {
			const rows = state.data.reports || [];
			$root.find(".worker-report-history").html(
				rows.length
						? `<div class="table-responsive"><table class="table table-bordered"><thead><tr><th>${__("开始/提交时间")}</th><th>${__("工序")}</th><th>${__("数量")}</th><th>${__("实际分钟")}</th><th>${__("计薪分钟")}</th><th>${__("金额")}</th><th>${__("状态")}</th></tr></thead><tbody>${rows
								.map((row) => {
									const meta = workReportStatusMeta(row.status, __);
									return `<tr><td>${esc(frappe.datetime.str_to_user(row.submitted_at || row.actual_start_time || row.labor_date))}</td><td>${esc(row.operation)}</td><td>${number(row.completed_qty)}</td><td>${number(row.actual_minutes)}</td><td>${number(row.reported_minutes)}</td><td>${number(row.wage_amount)}</td><td><span class="indicator-pill ${meta.indicator}">${esc(meta.label)}</span>${row.rejection_reason ? `<div class="text-danger small">${esc(row.rejection_reason)}</div>` : ""}</td></tr>`;
							})
							.join("")}</tbody></table></div>`
					: `<div class="text-muted worker-reporting-empty">${__("还没有报工记录。")}</div>`
			);
		}

		function render() {
			$root.find(".worker-reporting-summary").html(
				`<div><strong>${__("今日计薪分钟")}</strong><span>${number(state.data.daily_minutes_used)} / ${number(state.data.daily_minutes_limit)}</span></div>`
			);
			renderAssignments();
			renderReports();
		}

		function load() {
			return frappe.call({
				method: "process_simplification.api.production_reporting.get_my_dashboard",
				freeze: true,
				freeze_message: __("正在读取派工和报工状态..."),
			}).then((response) => {
				state.data = response.message || { assignments: [], reports: [] };
				render();
			});
		}

		async function startWork(row) {
			const requestId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
			await frappe.call({
				method: "process_simplification.api.production_reporting.start_work_session",
				type: "POST",
				freeze: true,
				freeze_message: __("正在开始计时..."),
				args: { assignment: row.name, request_id: requestId },
			});
			frappe.show_alert({ message: __("已经开始计时。"), indicator: "green" });
			await load();
		}

		function openFinishDialog(row) {
			const requestId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
			const dialog = new frappe.ui.Dialog({
				title: __("结束计时并提交报工"),
				fields: [
					{ fieldname: "job_card", fieldtype: "Data", label: __("Job Card"), read_only: 1, default: row.job_card },
					{ fieldname: "operation", fieldtype: "Data", label: __("工序"), read_only: 1, default: row.operation },
					{ fieldname: "started_at", fieldtype: "Datetime", label: __("实际开始时间"), read_only: 1, default: row.active_started_at },
					{ fieldname: "wage", fieldtype: "Data", label: __("计价方式与单价"), read_only: 1, default: `${row.wage_type === "Time" ? __("计时") : __("计件")} · ${number(row.rate)}` },
					{ fieldname: "remaining", fieldtype: "Float", label: __("当前可报数量"), read_only: 1, default: row.reportable_qty },
					{ fieldname: "completed_qty", fieldtype: "Float", label: __("本次完成数量"), reqd: 1 },
					{ fieldname: "time_note", fieldtype: "HTML", options: `<p class="text-muted">${__("实际生产分钟由服务器根据开始和结束时间计算；计时工资直接使用该分钟数。")}</p>` },
				],
				primary_action_label: __("提交主管审核"),
				primary_action: async (values) => {
					if (flt(values.completed_qty) <= 0 || flt(values.completed_qty) > flt(row.reportable_qty)) {
						frappe.msgprint(__("本次完成数量必须大于 0 且不超过当前可报数量。"));
						return;
					}
					const $button = dialog.get_primary_btn();
					$button.prop("disabled", true).text(__("提交中..."));
					try {
						await frappe.call({
							method: "process_simplification.api.production_reporting.finish_work_session",
							type: "POST",
							args: { report: row.active_report, completed_qty: values.completed_qty, request_id: requestId },
						});
						dialog.hide();
						frappe.show_alert({ message: __("报工已提交主管审核。"), indicator: "green" });
						await load();
					} finally {
						$button.prop("disabled", false).text(__("提交主管审核"));
					}
				},
			});
			dialog.set_secondary_action(() => {
				frappe.confirm(__("确定取消本次计时吗？未提交的计时记录将被删除。"), async () => {
					await frappe.call({
						method: "process_simplification.api.production_reporting.cancel_work_session",
						type: "POST",
						args: { report: row.active_report },
					});
					dialog.hide();
					frappe.show_alert({ message: __("本次计时已取消。"), indicator: "orange" });
					await load();
				});
			});
			dialog.set_secondary_action_label(__("取消本次计时"));
			dialog.show();
			if (!row.can_finish) {
				dialog.get_primary_btn().prop("disabled", true);
			}
		}

		$root.on("click", ".worker-report-action", (event) => {
			const row = (state.data.assignments || []).find((item) => item.name === $(event.currentTarget).data("assignment"));
			const action = row ? workReportButtonMeta(row, __).action : null;
			if (action === "start") startWork(row);
			if (action === "finish") openFinishDialog(row);
		});
		page.add_inner_button(__("刷新"), load);
	};

	frappe.pages["my-production-reporting"].refresh = function (wrapper) {
		return wrapper.page?.worker_reporting?.load?.();
	};
}
