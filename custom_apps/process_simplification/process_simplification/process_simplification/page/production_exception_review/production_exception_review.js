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

function runExceptionReviewToolbarLoad(load) {
	load();
}

const productionExceptionReviewApi = {
	exceptionStatusMeta,
	exceptionTypeLabel,
	exceptionCauseLabel,
	exceptionActionState,
	runExceptionReviewToolbarLoad,
};
if (typeof module !== "undefined" && module.exports) module.exports = productionExceptionReviewApi;

if (typeof frappe !== "undefined") {
	frappe.pages["production-exception-review"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({ parent: wrapper, title: __("生产异常审核"), single_column: true });
		page.main.html(`
			<div class="process-simplification-page production-exception-page">
				<section><h4>${__("待主管审核")}</h4><div class="exception-pending-list"></div></section>
				<section><h4>${__("待仓管过账")}</h4><div class="exception-stock-list"></div></section>
				<section><h4>${__("最近已处理")}</h4><div class="exception-processed-list"></div></section>
			</div>`);
		const $root = page.main.find(".production-exception-page");
		const state = { data: { pending: [], stock_queue: [], processed: [] } };
		page.production_exception_review = { state, load };
		const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
		const number = (value) => format_number(flt(value), null, 2);
		const dateTime = (value) => value ? frappe.datetime.str_to_user(String(value).replace(/(\d{2}:\d{2}:\d{2})\.\d{1,6}/, "$1")) : "-";

		function documentLink(doctype, name) {
			return name ? `<a href="#" class="exception-document-link" data-doctype="${esc(doctype)}" data-name="${esc(name)}">${esc(name)}</a>` : "-";
		}

		function rowHtml(row) {
			const status = exceptionStatusMeta(row.status, __);
			const actions = exceptionActionState(row);
			const item = row.item_code
				? `<span>${__("物料")}：${esc(row.item_code)} ${esc(row.item_name || "")}</span>`
				: `<span>${__("损耗成品数量")}：${number(row.qty)}</span>`;
			return `<article class="worker-assignment-card exception-request-card">
				<div><strong>${esc(exceptionTypeLabel(row.request_type, __))}</strong><span class="indicator-pill ${status.indicator}">${esc(status.label)}</span></div>
				<div class="worker-assignment-facts">
					<span>${__("工人")}：${esc(row.employee_name || row.employee)}</span>
					<span>${__("生产任务单")}：${documentLink("Job Card", row.job_card)}</span>
					<span>${__("生产工单")}：${documentLink("Work Order", row.work_order)}</span>
					<span>${__("工序")}：${esc(row.operation)}</span>
					<span>${__("原因类别")}：${esc(exceptionCauseLabel(row.cause, __))}</span>
					${item}
					${row.item_code ? `<span>${__("数量")}：${number(row.qty)} ${esc(row.stock_uom || "")}</span>` : ""}
					${row.source_warehouse ? `<span>${esc(row.source_warehouse)} → ${esc(row.target_warehouse)}</span>` : ""}
					<span>${__("申请时间")}：${esc(dateTime(row.requested_at))}</span>
				</div>
				<p>${esc(row.reason)}</p>
				${row.rejection_reason ? `<p class="text-danger">${__("驳回原因")}：${esc(row.rejection_reason)}</p>` : ""}
				<div class="exception-actions">
					${actions.can_approve ? `<button class="btn btn-primary btn-sm exception-approve" data-request="${esc(row.name)}">${row.status === "Approved" ? __("重新生成库存单") : __("批准")}</button>` : ""}
					${actions.can_reject ? `<button class="btn btn-default btn-sm exception-reject" data-request="${esc(row.name)}">${__("驳回")}</button>` : ""}
					${actions.can_open_stock_entry ? `<button class="btn btn-default btn-sm exception-open-stock" data-stock-entry="${esc(row.stock_entry)}">${__("打开库存单")}</button>` : ""}
				</div>
			</article>`;
		}

		function renderList(selector, rows, emptyMessage) {
			$root.find(selector).html(rows.length ? rows.map(rowHtml).join("") : `<div class="text-muted worker-reporting-empty">${esc(emptyMessage)}</div>`);
		}

		function render() {
			renderList(".exception-pending-list", state.data.pending || [], __("当前没有待审核的异常申请。"));
			renderList(".exception-stock-list", state.data.stock_queue || [], __("当前没有等待库存处理的申请。"));
			renderList(".exception-processed-list", state.data.processed || [], __("还没有已处理的异常申请。"));
		}

		function load() {
			return frappe.call({
				method: "process_simplification.api.production_exceptions.get_review_dashboard",
				freeze: true,
				freeze_message: __("正在读取异常申请..."),
			}).then((response) => {
				state.data = response.message || { pending: [], stock_queue: [], processed: [] };
				render();
			});
		}

		function findRequest(name) {
			return [...(state.data.pending || []), ...(state.data.stock_queue || []), ...(state.data.processed || [])]
				.find((row) => row.name === name);
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
		$root.on("click", ".exception-document-link", (event) => {
			event.preventDefault();
			frappe.set_route("Form", $(event.currentTarget).data("doctype"), $(event.currentTarget).data("name"));
		});
		page.add_inner_button(__("刷新"), () => runExceptionReviewToolbarLoad(load));
	};

	frappe.pages["production-exception-review"].refresh = function (wrapper) {
		return wrapper.page?.production_exception_review?.load?.();
	};
}
