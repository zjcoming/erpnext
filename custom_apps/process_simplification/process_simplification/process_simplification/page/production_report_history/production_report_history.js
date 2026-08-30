function workerReportHistoryStatusMeta(status, translate = (message) => message) {
	const statuses = {
		"Pending Approval": { label: translate("待审核"), indicator: "orange" },
		Approved: { label: translate("已通过"), indicator: "green" },
		Rejected: { label: translate("已驳回"), indicator: "red" },
	};
	return statuses[status] || { label: status || translate("未知"), indicator: "gray" };
}

function workerReportHistoryPaginationHtml(meta = {}, helpers = {}) {
	const translate = helpers.translate || ((message) => message);
	const escapeHtml = helpers.escapeHtml || ((value) => String(value ?? ""));
	if (!Number(meta.total_count || 0)) return "";
	return `<div class="worker-history-pagination">
		<span>${translate("第 {0} / {1} 页，共 {2} 条", [
			escapeHtml(meta.page || 1),
			escapeHtml(meta.total_pages || 1),
			escapeHtml(meta.total_count || 0),
		])}</span>
		<div>
			<button class="btn btn-default worker-history-page" data-page="${escapeHtml(Number(meta.page || 1) - 1)}" ${meta.has_prev ? "" : "disabled"}>${translate("上一页")}</button>
			<button class="btn btn-default worker-history-page" data-page="${escapeHtml(Number(meta.page || 1) + 1)}" ${meta.has_next ? "" : "disabled"}>${translate("下一页")}</button>
		</div>
	</div>`;
}

function workerReportHistoryCardHtml(row = {}, helpers = {}) {
	const translate = helpers.translate || ((message) => message);
	const escapeHtml = helpers.escapeHtml || ((value) => String(value ?? ""));
	const formatNumber = helpers.formatNumber || ((value) => String(value ?? ""));
	const formatDateTime = helpers.formatDateTime || ((value) => String(value || "-"));
	const status = workerReportHistoryStatusMeta(row.status, translate);
	const reviewText = row.status === "Pending Approval"
		? translate("已提交，等待主管审核")
		: `${translate("审核人")}：${row.reviewed_by || "-"} · ${translate("审核时间")}：${formatDateTime(row.reviewed_at)}`;
	return `<article class="worker-history-card">
		<div class="worker-history-heading">
			<div><strong>${escapeHtml(row.operation || "-")}</strong><small>${escapeHtml(row.labor_date || "")}</small></div>
			<span class="indicator-pill ${escapeHtml(status.indicator)}">${escapeHtml(status.label)}</span>
		</div>
		<div class="worker-history-documents">
			<button class="btn btn-link worker-history-document" data-doctype="Job Card" data-name="${escapeHtml(row.job_card || "")}">${translate("生产任务单")}：${escapeHtml(row.job_card || "-")}</button>
			<button class="btn btn-link worker-history-document" data-doctype="Work Order" data-name="${escapeHtml(row.work_order || "")}">${translate("生产工单")}：${escapeHtml(row.work_order || "-")}</button>
		</div>
		<div class="worker-history-facts">
			<span><small>${translate("完成数量")}</small><strong>${formatNumber(row.completed_qty)}</strong></span>
			<span><small>${translate("实际分钟")}</small><strong>${formatNumber(row.actual_minutes)}</strong></span>
			<span><small>${translate("计薪分钟")}</small><strong>${formatNumber(row.reported_minutes)}</strong></span>
			<span><small>${translate("计薪金额")}</small><strong>${formatNumber(row.wage_amount)}</strong></span>
		</div>
		<div class="worker-history-audit"><span>${escapeHtml(reviewText)}</span>${row.submitted_at ? `<small>${translate("提交时间")}：${escapeHtml(formatDateTime(row.submitted_at))}</small>` : ""}${row.rejection_reason ? `<strong class="text-danger">${translate("驳回原因")}：${escapeHtml(row.rejection_reason)}</strong>` : ""}</div>
	</article>`;
}

function workerExceptionHistoryCardHtml(row = {}, helpers = {}) {
	const translate = helpers.translate || ((message) => message);
	const escapeHtml = helpers.escapeHtml || ((value) => String(value ?? ""));
	const formatNumber = helpers.formatNumber || ((value) => String(value ?? ""));
	const formatDateTime = helpers.formatDateTime || ((value) => String(value || "-"));
	const reporting = helpers.reporting || {};
	const status = reporting.workerExceptionStatusMeta
		? reporting.workerExceptionStatusMeta(row.status, translate)
		: { label: row.status || translate("未知"), indicator: "gray" };
	const type = reporting.workerExceptionTypeLabel
		? reporting.workerExceptionTypeLabel(row.request_type, translate)
		: row.request_type;
	const subject = row.request_type === "Process Loss"
		? row.operation
		: `${row.item_name || row.item_code || ""}${row.item_name && row.item_code ? `（${row.item_code}）` : ""}`;
	const detail = row.rejection_reason || row.review_note || "";
	return `<article class="worker-exception-history-card">
		<div><strong>${escapeHtml(type)}</strong><span class="indicator-pill ${escapeHtml(status.indicator)}">${escapeHtml(status.label)}</span></div>
		<span>${escapeHtml(subject || "-")} · ${formatNumber(row.qty)}</span>
		<small class="text-muted">${escapeHtml(formatDateTime(row.requested_at))}</small>
		${detail ? `<p class="${row.status === "Rejected" ? "text-danger" : "text-muted"}">${escapeHtml(detail)}</p>` : ""}
	</article>`;
}

const productionReportHistoryApi = {
	workerReportHistoryStatusMeta,
	workerReportHistoryPaginationHtml,
	workerReportHistoryCardHtml,
	workerExceptionHistoryCardHtml,
};

if (typeof module !== "undefined" && module.exports) module.exports = productionReportHistoryApi;

if (typeof frappe !== "undefined") {
	frappe.pages["production-report-history"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("报工历史"),
			single_column: true,
		});
		page.main.html(`
			<div class="process-simplification-page worker-history-page">
				<form class="worker-history-filters">
					<label><span>${__("审核状态")}</span><select name="status" class="form-control"><option value="">${__("全部")}</option><option value="Pending Approval">${__("待审核")}</option><option value="Approved">${__("已通过")}</option><option value="Rejected">${__("已驳回")}</option></select></label>
					<label><span>${__("工序")}</span><input name="operation" class="form-control" placeholder="${__("工序名称")}"></label>
					<label><span>${__("生产工单")}</span><input name="work_order" class="form-control" placeholder="${__("可留空")}"></label>
					<label><span>${__("生产任务单")}</span><input name="job_card" class="form-control" placeholder="${__("可留空")}"></label>
					<label><span>${__("开始日期")}</span><input name="from_date" type="date" class="form-control"></label>
					<label><span>${__("结束日期")}</span><input name="to_date" type="date" class="form-control"></label>
					<label><span>${__("每页")}</span><select name="page_length" class="form-control"><option>20</option><option>50</option><option>100</option></select></label>
					<button class="btn btn-primary worker-history-search" type="submit">${__("查询")}</button>
				</form>
				<section><div class="worker-history-results"></div><div class="worker-history-pager"></div></section>
				<details class="worker-exception-history-section">
					<summary>${__("退料/报废申请记录")}</summary>
					<div class="worker-exception-history-list"></div>
				</details>
			</div>`);

		const $root = page.main.find(".worker-history-page");
		const reporting = window.process_simplification.worker_reporting;
		const state = { rows: [], exceptions: [], pagination: { page: 1, page_length: 20 } };
		const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
		const number = (value) => format_number(flt(value), null, 2);
		const dateTime = (value) =>
			value ? frappe.datetime.str_to_user(reporting.normalizeFrappeDateTime(value)) : "-";
		const helpers = {
			translate: __,
			escapeHtml: esc,
			formatNumber: number,
			formatDateTime: dateTime,
			reporting,
		};

		function filterValues() {
			const values = {};
			for (const field of $root.find(".worker-history-filters").serializeArray()) {
				values[field.name] = String(field.value || "").trim();
			}
			return values;
		}

		function renderReports() {
			$root.find(".worker-history-results").html(
				state.rows.length
					? `<div class="worker-history-list">${state.rows
							.map((row) => workerReportHistoryCardHtml(row, helpers))
							.join("")}</div>`
					: `<div class="text-muted worker-reporting-empty">${__("没有符合条件的报工记录。")}</div>`
			);
			$root.find(".worker-history-pager").html(
				workerReportHistoryPaginationHtml(state.pagination, helpers)
			);
		}

		function renderExceptions() {
			$root.find(".worker-exception-history-list").html(
				state.exceptions.length
					? state.exceptions
							.map((row) => workerExceptionHistoryCardHtml(row, helpers))
							.join("")
					: `<div class="text-muted worker-reporting-empty">${__("还没有退料或报废申请。")}</div>`
			);
		}

		function loadReports(pageNumber = 1) {
			return frappe.call({
				method: "process_simplification.api.production_reporting.get_my_report_history",
				args: { ...filterValues(), page: pageNumber },
				freeze: true,
				freeze_message: __("正在查询报工与审核记录..."),
			}).then((response) => {
				state.rows = response.message?.rows || [];
				state.pagination = response.message?.pagination || { page: 1, page_length: 20 };
				renderReports();
			});
		}

		function loadExceptions() {
			return frappe.call({
				method: "process_simplification.api.production_exceptions.get_my_requests",
				args: { limit: 100 },
			}).then((response) => {
				state.exceptions = response.message || [];
				renderExceptions();
			});
		}

		function load() {
			return Promise.all([loadReports(1), loadExceptions()]);
		}

		$root.on("submit", ".worker-history-filters", (event) => {
			event.preventDefault();
			loadReports(1);
		});
		$root.on("click", ".worker-history-page", (event) => {
			loadReports(Number($(event.currentTarget).data("page") || 1));
		});
		$root.on("click", ".worker-history-document", (event) => {
			event.preventDefault();
			const $button = $(event.currentTarget);
			if ($button.data("name")) {
				frappe.set_route("Form", $button.data("doctype"), $button.data("name"));
			}
		});
		page.add_inner_button(__("刷新"), () => reporting.runWorkerReportingToolbarLoad(load));
		page.worker_history = { state, load };
	};

	frappe.pages["production-report-history"].refresh = function (wrapper) {
		return wrapper.page?.worker_history?.load?.();
	};
}
