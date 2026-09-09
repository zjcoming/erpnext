function exceptionStatusMeta(status, translate = (message) => message) {
	const statuses = {
		"Pending Approval": { label: translate("待主管审核"), indicator: "orange" },
		Approved: { label: translate("已批准，待生成库存单"), indicator: "blue" },
		"Awaiting Stock Entry": { label: translate("待仓管过账"), indicator: "blue" },
		Applied: { label: translate("已写入过程损耗"), indicator: "green" },
		Completed: { label: translate("库存已过账"), indicator: "green" },
		Rejected: { label: translate("已驳回"), indicator: "red" },
	};
	return statuses[status] || { label: status || translate("未知"), indicator: "gray" };
}

function exceptionTypeLabel(type, translate = (message) => message) {
	return ({
		"Material Return": translate("未耗用物料退回"),
		"Material Scrap": translate("物料转报废仓"),
		"Process Loss": translate("工序过程损耗"),
	})[type] || type || translate("未知");
}

function exceptionCauseLabel(cause, translate = (message) => message) {
	return ({
		"Material Defect": translate("物料问题"),
		"Operation Error": translate("操作失误"),
		Other: translate("其他"),
	})[cause] || cause || translate("未知");
}

function exceptionActionState(row = {}) {
	return {
		can_approve: Boolean(row.can_approve),
		can_reject: Boolean(row.can_reject),
		can_open_stock_entry: Boolean(row.can_open_stock_entry && row.stock_entry),
	};
}

function exceptionMonthRange(month) {
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

function exceptionPaginationHtml(pagination = {}, helpers = {}) {
	const translate = helpers.translate || ((message) => message);
	const escapeHtml = helpers.escapeHtml || ((value) => String(value));
	const page = Number(pagination.page || 1);
	const pageLength = Number(pagination.page_length || 20);
	const totalPages = Number(pagination.total_pages || 0);
	return `<div class="report-review-pagination" aria-label="${escapeHtml(translate("分页"))}"><div>${escapeHtml(translate("第"))} ${page} / ${totalPages || 1} ${escapeHtml(translate("页"))} · ${escapeHtml(translate("共"))} ${Number(pagination.total_count || 0)} ${escapeHtml(translate("条"))}</div><div class="report-review-pagination-actions"><button class="btn btn-default btn-sm exception-page-action" data-page="${Math.max(page - 1, 1)}" ${pagination.has_prev ? "" : "disabled"}>${escapeHtml(translate("上一页"))}</button><button class="btn btn-default btn-sm exception-page-action" data-page="${totalPages ? Math.min(page + 1, totalPages) : page + 1}" ${pagination.has_next ? "" : "disabled"}>${escapeHtml(translate("下一页"))}</button><select class="form-control input-sm exception-page-size" aria-label="${escapeHtml(translate("每页条数"))}">${[20, 50, 100].map((size) => `<option value="${size}" ${size === pageLength ? "selected" : ""}>${size} ${escapeHtml(translate("条/页"))}</option>`).join("")}</select></div></div>`;
}

function runExceptionReviewToolbarLoad(load) {
	load();
}

const productionExceptionReviewApi = {
	exceptionStatusMeta,
	exceptionTypeLabel,
	exceptionCauseLabel,
	exceptionActionState,
	exceptionMonthRange,
	exceptionPaginationHtml,
	runExceptionReviewToolbarLoad,
};
if (typeof module !== "undefined" && module.exports) module.exports = productionExceptionReviewApi;

if (typeof frappe !== "undefined") {
	frappe.pages["production-exception-review"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({ parent: wrapper, title: __("生产异常审核"), single_column: true });
		page.main.html(`
			<div class="process-simplification-page production-exception-page">
				<section class="review-hero exception-review-hero">
					<div class="review-hero-copy"><span class="review-eyebrow">${__("异常闭环台")}</span><h2>${__("主管定责，仓管落账")}</h2><p>${__("退料、报废与过程损耗分开处理，库存变化和生产损耗保持可追溯。")}</p></div>
					<div class="review-kpi-grid"><div class="review-kpi is-urgent"><span>${__("待主管审核")}</span><strong class="exception-kpi-pending">0</strong><small>${__("需要批准或驳回")}</small></div><div class="review-kpi is-info"><span>${__("待仓管过账")}</span><strong class="exception-kpi-stock">0</strong><small>${__("库存尚未变化")}</small></div><div class="review-kpi is-success"><span>${__("历史记录")}</span><strong class="exception-kpi-history">0</strong><small>${__("已完成闭环")}</small></div></div>
				</section>
				<div class="exception-priority-grid">
					<section class="review-section review-priority-section"><div class="review-section-heading"><div><span class="review-step">1</span><div><h3>${__("待主管审核")}</h3><p>${__("先判断异常类型、原因和申请数量。")}</p></div></div><span class="review-count-badge exception-pending-count">0</span></div><div class="exception-pending-list"></div></section>
					<section class="review-section review-stock-section"><div class="review-section-heading"><div><span class="review-step is-stock">2</span><div><h3>${__("待仓管过账")}</h3><p>${__("库存单提交后，库存才真正变化。")}</p></div></div><span class="review-count-badge exception-stock-count">0</span></div><div class="exception-stock-list"></div></section>
				</div>
				<section class="review-section review-history-section" id="exception-review-history">
					<div class="review-section-heading"><div><span class="review-step is-history">3</span><div><h3>${__("异常审核历史")}</h3><p>${__("按月份、员工、异常类型或生产工单查询。")}</p></div></div><span class="review-count-badge exception-history-count">0</span></div>
					<div class="review-filter-bar exception-filter-bar">
						<label class="review-filter-field"><span>${__("月份")}</span><input type="month" class="form-control exception-history-month"></label>
						<div class="review-filter-field exception-history-employee"></div><div class="review-filter-field exception-history-type"></div><div class="review-filter-field exception-history-status"></div><div class="review-filter-field exception-history-work-order"></div>
						<div class="review-filter-actions"><button class="btn btn-primary exception-history-search">${__("查询历史")}</button><button class="btn btn-default exception-history-reset">${__("重置")}</button></div>
					</div>
					<div class="exception-history-results"></div><div class="exception-pager-history"></div>
				</section>
			</div>`);
		const $root = page.main.find(".production-exception-page");
		const state = {
			data: { pending: [], stock_queue: [], processed: [] },
			history: { rows: [], pagination: { page: 1, page_length: 20 } },
		};
		page.production_exception_review = { state, load };
		const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
		const number = (value) => format_number(flt(value), null, 2);
		const dateTime = (value) => value ? frappe.datetime.str_to_user(String(value).replace(/(\d{2}:\d{2}:\d{2})\.\d{1,6}/, "$1")) : "-";
		const currentMonth = () => String(frappe.datetime.get_today()).slice(0, 7);
		$root.find(".exception-history-month").val(currentMonth());
		const historyControls = {
			employee: frappe.ui.form.make_control({ parent: $root.find(".exception-history-employee"), df: { fieldname: "employee", fieldtype: "Link", options: "Employee", label: __("员工") }, render_input: true }),
			type: frappe.ui.form.make_control({ parent: $root.find(".exception-history-type"), df: { fieldname: "request_type", fieldtype: "Select", label: __("异常类型"), options: [
				{ label: __("全部类型"), value: "" }, { label: __("未耗用物料退回"), value: "Material Return" }, { label: __("物料转报废仓"), value: "Material Scrap" }, { label: __("工序过程损耗"), value: "Process Loss" },
			] }, render_input: true }),
			status: frappe.ui.form.make_control({ parent: $root.find(".exception-history-status"), df: { fieldname: "status", fieldtype: "Select", label: __("处理结果"), options: [
				{ label: __("全部结果"), value: "" }, { label: __("库存已过账"), value: "Completed" }, { label: __("已写入过程损耗"), value: "Applied" }, { label: __("已驳回"), value: "Rejected" },
			] }, render_input: true }),
			workOrder: frappe.ui.form.make_control({ parent: $root.find(".exception-history-work-order"), df: { fieldname: "work_order", fieldtype: "Link", options: "Work Order", label: __("生产工单") }, render_input: true }),
		};

		function documentLink(doctype, name) {
			return name ? `<a href="#" class="exception-document-link" data-doctype="${esc(doctype)}" data-name="${esc(name)}">${esc(name)}</a>` : "-";
		}

		function rowHtml(row) {
			const status = exceptionStatusMeta(row.status, __);
			const actions = exceptionActionState(row);
			const item = row.item_code
				? `<span>${__("物料")}：${esc(row.item_code)} ${esc(row.item_name || "")}</span>`
				: `<span>${__("损耗成品数量")}：${number(row.qty)}</span>`;
			return `<article class="worker-assignment-card exception-request-card" data-request="${esc(row.name)}">
				<div class="exception-card-heading"><div><span class="review-card-kicker">${esc(exceptionCauseLabel(row.cause, __))}</span><strong>${esc(exceptionTypeLabel(row.request_type, __))}</strong></div><span class="indicator-pill ${status.indicator}">${esc(status.label)}</span></div>
				<div class="review-card-metrics"><div class="is-primary"><span>${row.item_code ? __("申请数量") : __("损耗数量")}</span><strong>${number(row.qty)} <small>${esc(row.stock_uom || "")}</small></strong></div><div><span>${__("工人")}</span><strong>${esc(row.employee_name || row.employee)}</strong></div><div><span>${__("工序")}</span><strong>${esc(row.operation || "-")}</strong></div></div>
				<div class="worker-assignment-facts">
					<span>${__("生产任务单")}：${documentLink("Job Card", row.job_card)}</span>
					<span>${__("生产工单")}：${documentLink("Work Order", row.work_order)}</span>
					${item}
					${row.source_warehouse ? `<span>${__("库存路径")}：${esc(row.source_warehouse)} → ${esc(row.target_warehouse)}</span>` : ""}
					<span>${__("申请时间")}：${esc(dateTime(row.requested_at))}</span>
				</div>
				<div class="review-reason-box"><span>${__("异常说明")}</span><strong>${esc(row.reason)}</strong></div>
				${row.rejection_reason ? `<div class="review-alert is-danger">${__("驳回原因")}：${esc(row.rejection_reason)}</div>` : ""}
				<div class="exception-actions">
					${actions.can_approve ? `<button class="btn btn-primary btn-sm exception-approve" data-request="${esc(row.name)}">${row.status === "Approved" ? __("重新生成库存单") : __("批准")}</button>` : ""}
					${actions.can_reject ? `<button class="btn btn-default btn-sm exception-reject" data-request="${esc(row.name)}">${__("驳回")}</button>` : ""}
					${actions.can_open_stock_entry ? `<button class="btn btn-default btn-sm exception-open-stock" data-stock-entry="${esc(row.stock_entry)}">${__("打开库存单")}</button>` : ""}
				</div>
			</article>`;
		}

		function renderList(selector, rows, emptyMessage) {
			$root.find(selector).html(rows.length ? `<div class="exception-card-list">${rows.map(rowHtml).join("")}</div>` : `<div class="text-muted worker-reporting-empty">${esc(emptyMessage)}</div>`);
		}

		function render() {
			const pending = state.data.pending || [];
			const stockQueue = state.data.stock_queue || [];
			$root.find(".exception-kpi-pending, .exception-pending-count").text(pending.length);
			$root.find(".exception-kpi-stock, .exception-stock-count").text(stockQueue.length);
			renderList(".exception-pending-list", pending, __("当前没有待主管审核的异常申请。"));
			renderList(".exception-stock-list", stockQueue, __("当前没有等待仓管过账的申请。"));
		}

		function renderHistory() {
			const rows = state.history.rows || [];
			const pagination = state.history.pagination || {};
			$root.find(".exception-kpi-history, .exception-history-count").text(Number(pagination.total_count || 0));
			$root.find(".exception-history-results").html(rows.length
				? `<div class="review-history-list">${rows.map((row) => {
					const meta = exceptionStatusMeta(row.status, __);
					const handledAt = row.processed_at || row.reviewed_at || row.requested_at;
					return `<article class="review-history-row exception-history-row" data-request="${esc(row.name)}" tabindex="0"><div class="review-history-main"><strong>${esc(row.employee_name || row.employee)} · ${esc(row.operation || "-")}</strong><span>${esc(dateTime(handledAt))}</span></div><div class="review-history-status"><span class="indicator-pill ${meta.indicator}">${esc(meta.label)}</span><small>${esc(exceptionTypeLabel(row.request_type, __))}</small></div><div class="review-history-metric"><span>${row.item_code ? __("数量") : __("损耗")}</span><strong>${number(row.qty)} ${esc(row.stock_uom || "")}</strong></div><div class="review-history-docs"><span>${documentLink("Job Card", row.job_card)}</span><span>${documentLink("Work Order", row.work_order)}</span></div><button class="btn btn-default btn-sm exception-view-details" data-request="${esc(row.name)}">${__("查看明细")}</button></article>`;
				}).join("")}</div>`
				: `<div class="text-muted worker-reporting-empty">${__("没有符合条件的异常审核记录。")}</div>`
			);
			$root.find(".exception-pager-history").html(exceptionPaginationHtml(pagination, { translate: __, escapeHtml: esc }));
		}

		function load(options = {}) {
			return frappe.ps_read_page(page, {
				method: "process_simplification.api.production_exceptions.get_review_dashboard",
				background: options.background,
				freeze_message: __("正在读取异常申请..."),
				apply(response) {
				state.data = response.message || { pending: [], stock_queue: [], processed: [] };
				render();
				},
			}).then((applied) => {
				if (applied && !options.background) return loadHistory(state.history.pagination.page || 1);
				return applied;
			});
		}

		function loadHistory(pageNumber = 1) {
			const range = exceptionMonthRange($root.find(".exception-history-month").val());
			$root.find(".exception-history-results").addClass("is-loading");
			return Promise.resolve(frappe.call({
				method: "process_simplification.api.production_exceptions.get_review_history",
				args: {
					page: pageNumber,
					page_length: state.history.pagination.page_length || 20,
					employee: historyControls.employee.get_value(),
					request_type: historyControls.type.get_value(),
					status: historyControls.status.get_value(),
					work_order: historyControls.workOrder.get_value(),
					...range,
				},
			})).then((response) => {
				state.history.rows = response.message?.rows || [];
				state.history.pagination = response.message?.pagination || { page: 1, page_length: 20 };
				renderHistory();
			}).finally(() => $root.find(".exception-history-results").removeClass("is-loading"));
		}

		function findRequest(name) {
			return [...(state.data.pending || []), ...(state.data.stock_queue || []), ...(state.history.rows || [])]
				.find((row) => row.name === name);
		}

		function openDetails(row) {
			if (!row) return;
			const status = exceptionStatusMeta(row.status, __);
			const detail = (label, value) => `<div class="report-detail-item"><span>${esc(label)}</span><strong>${value}</strong></div>`;
			const dialog = new frappe.ui.Dialog({
				title: __("异常审核明细"),
				size: "large",
				fields: [{ fieldtype: "HTML", options: `<div class="report-review-detail-grid">
					${detail(__("状态"), `<span class="indicator-pill ${status.indicator}">${esc(status.label)}</span>`)}
					${detail(__("异常类型"), esc(exceptionTypeLabel(row.request_type, __)))}
					${detail(__("员工"), esc(row.employee_name || row.employee || "-"))}
					${detail(__("工序"), esc(row.operation || "-"))}
					${detail(__("生产任务单"), documentLink("Job Card", row.job_card))}
					${detail(__("生产工单"), documentLink("Work Order", row.work_order))}
					${detail(__("物料"), esc(row.item_code ? `${row.item_name || ""} ${row.item_code}`.trim() : "-"))}
					${detail(__("数量"), `${number(row.qty)} ${esc(row.stock_uom || "")}`)}
					${detail(__("申请时间"), esc(dateTime(row.requested_at)))}
					${detail(__("审核时间"), esc(dateTime(row.reviewed_at)))}
					${detail(__("审核人"), esc(row.reviewed_by || "-"))}
					${detail(__("库存单"), row.stock_entry ? documentLink("Stock Entry", row.stock_entry) : "-")}
					<div class="report-detail-item report-detail-wide"><span>${__("异常说明")}</span><strong>${esc(row.reason || "-")}</strong></div>
					${row.rejection_reason ? `<div class="report-detail-item report-detail-wide"><span>${__("驳回原因")}</span><strong>${esc(row.rejection_reason)}</strong></div>` : ""}
				</div>` }],
			});
			dialog.$wrapper.on("click", ".exception-document-link", (event) => {
				event.preventDefault();
				frappe.set_route("Form", $(event.currentTarget).data("doctype"), $(event.currentTarget).data("name"));
			});
			dialog.show();
		}

		function focusHistory() {
			$root.find("#exception-review-history")[0]?.scrollIntoView({ behavior: "smooth", block: "start" });
			$root.find(".exception-history-month").trigger("focus");
		}

		function approve(row) {
			const processLoss = row.request_type === "Process Loss";
			const dialog = new frappe.ui.Dialog({
				title: processLoss ? __("确认过程损耗") : __("批准退料/报废申请"),
				fields: [{ fieldtype: "HTML", options: processLoss
					? `<p>${__("批准后将把 {0} 写入 Job Card 过程损耗；不会计入工人合格数量和计件工资。", [number(row.qty)])}</p>`
					: `<p>${__("批准后只生成原生库存移动草稿；仓管提交 Stock Entry 后库存才会变化。")}</p>` }],
				primary_action_label: __("确认批准"),
				primary_action: async () => {
					dialog.get_primary_btn().prop("disabled", true);
					try {
						await frappe.call({ method: "process_simplification.api.production_exceptions.approve_exception", type: "POST", args: { request: row.name } });
						dialog.hide();
						frappe.show_alert({ message: processLoss ? __("过程损耗已写入 Job Card。") : __("库存移动草稿已生成。"), indicator: "green" });
						await load();
					} finally {
						dialog.get_primary_btn().prop("disabled", false);
					}
				},
			});
			dialog.show();
		}

		function reject(row) {
			const dialog = new frappe.ui.Dialog({
				title: __("驳回异常申请"),
				fields: [{ fieldname: "reason", fieldtype: "Small Text", label: __("驳回原因"), reqd: 1 }],
				primary_action_label: __("确认驳回"),
				primary_action: async (values) => {
					dialog.get_primary_btn().prop("disabled", true);
					try {
						await frappe.call({ method: "process_simplification.api.production_exceptions.reject_exception", type: "POST", args: { request: row.name, reason: values.reason } });
						dialog.hide();
						await load();
					} finally {
						dialog.get_primary_btn().prop("disabled", false);
					}
				},
			});
			dialog.show();
		}

		$root.on("click", ".exception-approve", (event) => {
			const row = findRequest($(event.currentTarget).data("request"));
			if (row) approve(row);
		});
		$root.on("click", ".exception-reject", (event) => {
			const row = findRequest($(event.currentTarget).data("request"));
			if (row) reject(row);
		});
		$root.on("click", ".exception-open-stock", (event) => frappe.set_route("Form", "Stock Entry", $(event.currentTarget).data("stock-entry")));
		$root.on("click", ".exception-view-details", (event) => {
			event.stopPropagation();
			openDetails(findRequest($(event.currentTarget).data("request")));
		});
		$root.on("click", ".exception-history-row", (event) => {
			if ($(event.target).closest(".exception-document-link, button").length) return;
			openDetails(findRequest($(event.currentTarget).data("request")));
		});
		$root.on("keydown", ".exception-history-row", (event) => {
			if (event.key === "Enter" || event.key === " ") $(event.currentTarget).trigger("click");
		});
		$root.on("click", ".exception-page-action", (event) => loadHistory(Number($(event.currentTarget).data("page") || 1)));
		$root.on("change", ".exception-page-size", (event) => {
			state.history.pagination.page_length = Number($(event.currentTarget).val() || 20);
			loadHistory(1);
		});
		$root.on("click", ".exception-history-search", () => loadHistory(1));
		$root.on("click", ".exception-history-reset", async () => {
			$root.find(".exception-history-month").val(currentMonth());
			await Promise.all([
				historyControls.employee.set_value(""),
				historyControls.type.set_value(""),
				historyControls.status.set_value(""),
				historyControls.workOrder.set_value(""),
			]);
			await loadHistory(1);
		});
		$root.on("click", ".exception-document-link", (event) => {
			event.preventDefault();
			frappe.set_route("Form", $(event.currentTarget).data("doctype"), $(event.currentTarget).data("name"));
		});
		page.add_inner_button(__("查看历史"), focusHistory);
		page.add_inner_button(__("刷新"), () => runExceptionReviewToolbarLoad(load));
	};

	frappe.pages["production-exception-review"].refresh = function (wrapper) {
		return wrapper.page?.production_exception_review?.load?.();
	};
}
