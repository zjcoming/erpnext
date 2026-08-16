function isProductionDueWithin7Days(demand) {
	return ["today", "within_7_days"].includes(demand.delivery_timing);
}

function productionMaterialStatusMeta(status, translate = (message) => message) {
	const statusCopy = {
		ready_now: { label: translate("当前可生产"), indicator: "green" },
		awaiting_purchase_receipt: { label: translate("待采购到货"), indicator: "blue" },
		purchase_request_pending: { label: translate("已提采购申请"), indicator: "orange" },
		new_purchase_required: { label: translate("需新采购"), indicator: "red" },
		waiting_subassembly: { label: translate("等待半成品"), indicator: "blue" },
		cannot_calculate: { label: translate("无法判断"), indicator: "gray" },
	};
	return statusCopy[status] || statusCopy.cannot_calculate;
}

function workOrderReadinessMeta(status, translate = (message) => message) {
	const statuses = {
		ready_now: { label: translate("当前可开工"), indicator: "green" },
		waiting_subassembly: { label: translate("等待半成品"), indicator: "blue" },
		awaiting_purchase_receipt: { label: translate("等待采购到货"), indicator: "blue" },
		purchase_request_pending: { label: translate("等待采购下单"), indicator: "orange" },
		purchase_shortage: { label: translate("缺底层原材料"), indicator: "red" },
		production_task_missing: { label: translate("缺少下级生产任务"), indicator: "red" },
		materials_transferred: { label: translate("已发料"), indicator: "green" },
		in_progress: { label: translate("生产中"), indicator: "blue" },
		completed: { label: translate("已完成"), indicator: "green" },
		blocked: { label: translate("已阻塞"), indicator: "red" },
	};
	return statuses[status] || { label: translate("待判断"), indicator: "gray" };
}

function productionStatusMeta(status) {
	const statusCopy = {
		ready_to_start: { indicator: "green" },
		in_production: { indicator: "blue" },
		partially_completed: { indicator: "blue" },
		unplanned: { indicator: "orange" },
		planning_required: { indicator: "orange" },
		legacy_work_order: { indicator: "red" },
		material_shortage: { indicator: "red" },
		awaiting_supply: { indicator: "blue" },
		waiting_subassembly: { indicator: "blue" },
		master_data_blocked: { indicator: "red" },
		awaiting_order_reservation: { indicator: "gray" },
		overplanned: { indicator: "gray" },
	};
	return statusCopy[status] || { indicator: "gray" };
}

function filterProductionDemands(demands, filters = {}) {
	const search = String(filters.search || "").trim().toLowerCase();
	return (demands || []).filter((demand) => {
		const searchable = [
			demand.demand_key,
			demand.sales_order,
			demand.customer,
			demand.customer_name,
			demand.item_code,
			demand.item_name,
			...(demand.work_orders || []).map((row) => row.name),
		]
			.join(" ")
			.toLowerCase();
		return (
			(!search || searchable.includes(search)) &&
			(!filters.customer || demand.customer === filters.customer) &&
			(!filters.deliveryWindow ||
				(filters.deliveryWindow === "within_7_days"
					? isProductionDueWithin7Days(demand)
					: demand.delivery_timing === filters.deliveryWindow)) &&
			(!filters.status || demand.status_code === filters.status) &&
			(!filters.risk || demand.risk_level === filters.risk) &&
			(!filters.shortageOnly || Number(demand.material_summary?.shortage_item_count || 0) > 0) &&
			(!filters.unplannedOnly || Number(demand.unplanned_production_qty || 0) > 0)
		);
	});
}

function productionSummary(demands) {
	return (demands || []).reduce(
		(summary, demand) => {
			summary.total_demands += 1;
			summary.unplanned_demands += Number(Number(demand.unplanned_production_qty || 0) > 0);
			summary.overdue_demands += Number(demand.delivery_timing === "overdue");
			summary.due_within_7_days += Number(isProductionDueWithin7Days(demand));
			summary.material_shortage_demands += Number(
				Number(demand.material_summary?.shortage_item_count || 0) > 0
			);
			summary.in_production_demands += Number(
				["in_production", "partially_completed"].includes(demand.status_code)
			);
			summary.awaiting_order_reservation_demands += Number(
				demand.status_code === "awaiting_order_reservation"
			);
			return summary;
		},
		{
			total_demands: 0,
			unplanned_demands: 0,
			overdue_demands: 0,
			due_within_7_days: 0,
			material_shortage_demands: 0,
			in_production_demands: 0,
			awaiting_order_reservation_demands: 0,
		}
	);
}

function workbenchPaginationHtml(pagination = {}, helpers) {
	const t = helpers.translate;
	const esc = helpers.escapeHtml;
	const page = Number(pagination.page || 1);
	const pageSize = Number(pagination.page_size || 20);
	const totalPages = Number(pagination.total_pages || 0);
	const totalCount = Number(pagination.total_count || 0);
	const previousPage = Math.max(page - 1, 1);
	const nextPage = totalPages ? Math.min(page + 1, totalPages) : page + 1;
	const pageSizes = [20, 50, 100];
	return `
		<div class="workbench-pagination" aria-label="${esc(t("åˆ†é¡µ"))}">
			<div class="workbench-pagination-summary">${esc(t("ç¬¬"))} ${page} / ${totalPages || 1} ${esc(t("é¡µ"))} · ${esc(t("å…±"))} ${totalCount} ${esc(t("æ¡"))}</div>
			<div class="workbench-pagination-actions">
				<button class="btn btn-default btn-sm workbench-page-action" data-page="${previousPage}" ${pagination.has_prev ? "" : "disabled"}>${esc(t("ä¸Šä¸€é¡µ"))}</button>
				<button class="btn btn-default btn-sm workbench-page-action" data-page="${nextPage}" ${pagination.has_next ? "" : "disabled"}>${esc(t("ä¸‹ä¸€é¡µ"))}</button>
				<select class="form-control input-sm workbench-page-size" aria-label="${esc(t("æ¯é¡µæ¡æ•°"))}">
					${pageSizes
						.map((size) => `<option value="${size}" data-page-size="${size}" ${size === pageSize ? "selected" : ""}>${size} ${esc(t("æ¡/é¡µ"))}</option>`)
						.join("")}
				</select>
			</div>
		</div>`;
}

function workbenchPaginationHtmlSafe(pagination = {}, helpers) {
	const t = helpers.translate;
	const esc = helpers.escapeHtml;
	const page = Number(pagination.page || 1);
	const pageSize = Number(pagination.page_size || 20);
	const totalPages = Number(pagination.total_pages || 0);
	const totalCount = Number(pagination.total_count || 0);
	const previousPage = Math.max(page - 1, 1);
	const nextPage = totalPages ? Math.min(page + 1, totalPages) : page + 1;
	const pageSizes = [20, 50, 100];
	return `
		<div class="workbench-pagination" aria-label="${esc(t("\u5206\u9875"))}">
			<div class="workbench-pagination-summary">${esc(t("\u7b2c"))} ${page} / ${totalPages || 1} ${esc(t("\u9875"))} · ${esc(t("\u5171"))} ${totalCount} ${esc(t("\u6761"))}</div>
			<div class="workbench-pagination-actions">
				<button class="btn btn-default btn-sm workbench-page-action" data-page="${previousPage}" ${pagination.has_prev ? "" : "disabled"}>${esc(t("\u4e0a\u4e00\u9875"))}</button>
				<button class="btn btn-default btn-sm workbench-page-action" data-page="${nextPage}" ${pagination.has_next ? "" : "disabled"}>${esc(t("\u4e0b\u4e00\u9875"))}</button>
				<select class="form-control input-sm workbench-page-size" aria-label="${esc(t("\u6bcf\u9875\u6761\u6570"))}">
					${pageSizes
						.map((size) => `<option value="${size}" data-page-size="${size}" ${size === pageSize ? "selected" : ""}>${size} ${esc(t("\u6761/\u9875"))}</option>`)
						.join("")}
				</select>
			</div>
		</div>`;
}

function productionDemandHtml(demand, helpers) {
	const t = helpers.translate;
	const esc = helpers.escapeHtml;
	const number = helpers.formatNumber;
	const date = helpers.formatDate;
	const quantityFacts = [
		[t("订单待交"), demand.pending_qty],
		[t("有效预留"), demand.reserved_qty],
		[t("可用成品"), demand.available_to_reserve],
		[t("成品覆盖"), demand.finished_stock_coverage_qty],
		[t("需要生产"), demand.production_required_qty],
		[t("工单覆盖"), demand.active_work_order_qty],
		[t("未安排"), demand.unplanned_production_qty],
		[t("已完工"), demand.completed_qty],
		[t("待回补"), demand.completed_unreserved_qty],
	];
	const actions = (demand.next_actions || [])
		.map(
			(row) =>
				`<button class="btn btn-sm btn-default production-action" data-action="${esc(row.action)}" data-sales-order="${esc(demand.sales_order)}" data-row="${esc(demand.sales_order_item)}" ${row.enabled === false ? "disabled" : ""}>${esc(t(row.label))}</button>`
		)
		.join(" ");
	const hasProductionPlan = (demand.production_plans || []).length > 0;
	const productionPlans = hasProductionPlan
		? (demand.production_plans || [])
				.map(
					(plan) => `<div class="production-plan-card">
						<a href="/app/production-plan/${encodeURIComponent(plan.name || "")}"><strong>${esc(plan.name || "")}</strong></a>
						<span>${esc(t("计划优先日期"))}: ${esc(date(plan.planned_date)) || esc(t("未设置"))}</span>
						<span>${esc(t("可开工工单"))}: ${number(plan.summary?.ready_work_order_count)}</span>
						<span>${esc(t("等待半成品"))}: ${number(plan.summary?.waiting_subassembly_count)}</span>
					</div>`
				)
				.join("")
		: `<div class="text-muted production-empty-section">${esc(t("尚未关联生产计划。"))}</div>`;
	const workOrders = (demand.work_orders || []).length
		? (demand.work_orders || [])
				.map((row) => {
					const readiness = !hasProductionPlan && !row.readiness_status
						? { label: t("未纳入生产计划"), indicator: "red" }
						: workOrderReadinessMeta(row.readiness_status, t);
					return `
						<div class="production-work-order-card">
							<div class="production-work-order-heading"><a href="/app/work-order/${encodeURIComponent(row.name || "")}"><strong>${esc(row.name || "")}</strong></a><span class="indicator-pill ${esc(readiness.indicator)}">${esc(readiness.label)}</span></div>
							<div data-label="${esc(t("生产物料"))}">${esc(row.production_item || "")}</div>
							<div data-label="${esc(t("计划数量"))}">${number(row.qty)}</div>
							<div data-label="${esc(t("已生产"))}">${number(row.produced_qty)}</div>
							<div data-label="${esc(t("剩余"))}">${number(Math.max(Number(row.qty || 0) - Number(row.produced_qty || 0), 0))}</div>
							<div data-label="BOM">${esc(row.bom_no || t("未设置"))}</div>
							<div data-label="${esc(t("原料仓"))}">${esc(row.source_warehouse || t("未设置"))}</div>
							<div data-label="${esc(t("在制仓"))}">${esc(row.wip_warehouse || t("未设置"))}</div>
							<div data-label="${esc(t("成品仓"))}">${esc(row.fg_warehouse || t("未设置"))}</div>
						</div>`;
				})
				.join("")
		: `<div class="text-muted production-empty-section">${esc(t("尚未创建工单。"))}</div>`;
	const emptyMaterialsMessage = !hasProductionPlan && (demand.work_orders || []).length
		? t("存在未关联 Production Plan 的旧工单。请先完成、停止或迁移旧工单；在此之前不计算可开工和缺料。")
		: !hasProductionPlan
			? t("请先创建生产计划；计划生成层级工单后才能检查工单直接用料和采购缺口。")
			: t("当前工单没有可展示的直接用料。");
	const materials = (demand.materials || []).length
		? `
			<div class="production-material-table-wrap">
				<table class="table table-bordered production-material-table">
					<thead><tr><th>${esc(t("物料"))}</th><th>${esc(t("本需求"))}</th><th>${esc(t("全部需求"))}</th><th>${esc(t("仓库库存"))}</th><th>${esc(t("已占用"))}</th><th>${esc(t("本次可用"))}</th><th>${esc(t("采购申请"))}</th><th>${esc(t("在途采购"))}</th><th>${esc(t("即时缺口"))}</th><th>${esc(t("采购缺口"))}</th><th>${esc(t("状态"))}</th></tr></thead>
					<tbody>${(demand.materials || [])
						.map((row) => {
							const statusMeta = productionMaterialStatusMeta(row.status, t);
							const hasCurrentGap = Number(row.current_gap_qty || 0) > 0;
							const docs = (row.supply_documents || []).filter(
								(doc) => hasCurrentGap || Number(doc.allocated_qty || 0) > 0
							);
							const docList = docs.length
								? `<tr class="production-supply-docs"><td colspan="11" data-label="${esc(t("采购单据"))}"><div class="production-supply-doc-list">${docs
									.map((doc) => {
										const typeLabel = doc.doctype === "Material Request" ? t("采购申请") : t("采购单");
										const lateTag = doc.is_late ? ` · <span class="indicator-pill red">${esc(t("晚于计划日期"))}</span>` : "";
										const allocation = Number(doc.allocated_qty || 0) > 0
											? ` · ${esc(t("已分配给本单"))} ${number(doc.allocated_qty)}`
											: ` · ${esc(t("未分配给本单"))}`;
										return `<a class="production-supply-doc${doc.is_late ? " is-late" : ""}" href="/app/${esc(frappe.router.slug(doc.doctype))}/${esc(doc.name)}" target="_blank"><span class="production-supply-doc-type">${esc(typeLabel)}</span> <strong>${esc(doc.name)}</strong> · <span class="indicator-pill grey">${esc(doc.status || "")}</span> · ${esc(t("未完成"))} ${number(doc.outstanding_qty)}${allocation}${doc.schedule_date ? ` · ${esc(t("交期"))} ${esc(frappe.datetime.str_to_user(doc.schedule_date))}` : ""}${lateTag}</a>`;
									})
									.join("")}</div></td></tr>`
								: "";
							return `
								<tr>
									<td data-label="${esc(t("物料"))}"><strong>${esc(row.item_code || "")}</strong><br><small>${esc(row.item_name || "")} · ${esc(row.warehouse || row.source_warehouse || t("未设置仓库"))}${row.is_shared ? ` · <span class="production-shared-material">${esc(t("共享物料"))}</span>` : ""}${row.supply_type === "manufactured" ? ` · <span class="production-shared-material">${esc(t("生产件"))}</span>` : ` · ${esc(t("采购件"))}`}</small></td>
									<td data-label="${esc(t("本需求"))}">${number(row.source_required_qty ?? row.required_qty)}</td>
									<td data-label="${esc(t("全部需求"))}">${number(row.total_required_qty ?? row.required_qty)}</td>
									<td data-label="${esc(t("仓库库存"))}">${number(row.actual_qty)}</td>
									<td data-label="${esc(t("已占用"))}">${number(row.committed_qty)}</td>
									<td data-label="${esc(t("本次可用"))}">${number(row.available_qty)}</td>
									<td data-label="${esc(t("采购申请"))}">${number(row.open_material_request_qty)}</td>
									<td data-label="${esc(t("在途采购"))}">${number(row.open_purchase_order_qty)}</td>
									<td data-label="${esc(t("即时缺口"))}">${number(row.current_gap_qty)}</td>
									<td data-label="${esc(t("采购缺口"))}">${number(row.shortage_qty)}</td>
									<td data-label="${esc(t("状态"))}"><span class="indicator-pill ${esc(statusMeta.indicator)}">${esc(statusMeta.label)}</span>${Number(row.shortage_qty || 0) > 0 && !docs.length ? `<br><small class="text-muted">${esc(t("尚未发起采购"))}</small>` : ""}</td>
								</tr>${docList}`;
						})
						.join("")}</tbody>
				</table>
			</div>`
		: `<div class="text-muted production-empty-section">${esc(emptyMaterialsMessage)}</div>`;
	return `
		<details class="production-demand production-risk-${esc(demand.risk_level || "gray")}" data-demand-key="${esc(demand.demand_key)}">
			<summary>
				<div class="production-demand-source"><strong>${esc(demand.sales_order || "")}</strong><span>${esc(demand.customer_name || demand.customer || "")}</span></div>
				<div class="production-demand-product"><strong>${esc(demand.item_code || "")}</strong><span>${esc(demand.item_name || "")}</span></div>
				<div class="production-demand-fact"><span>${esc(t("交期"))}</span><strong>${esc(date(demand.delivery_date)) || esc(t("未设置"))}</strong></div>
				<div class="production-demand-fact"><span>${esc(t("成品覆盖 / 待交"))}</span><strong>${number(demand.finished_stock_coverage_qty)} / ${number(demand.pending_qty)}</strong></div>
				<div class="production-demand-fact"><span>${esc(t("已安排 / 需生产"))}</span><strong>${number(demand.active_work_order_qty)} / ${number(demand.production_required_qty)}</strong></div>
				<div class="production-demand-fact"><span>${esc(t("未安排 / 已完工"))}</span><strong>${number(demand.unplanned_production_qty)} / ${number(demand.completed_qty)}</strong></div>
				<div class="production-demand-risk"><span class="indicator-pill ${esc(demand.risk_level || "gray")}">${esc(demand.risk_label || "")}</span><span class="indicator-pill ${esc(productionStatusMeta(demand.status_code).indicator)}">${esc(demand.status_label || "")}</span></div>
				<span class="production-demand-toggle">${esc(t("查看并处理"))}</span>
			</summary>
			<div class="production-demand-details">
				<div class="production-demand-actions">${actions}</div>
				<section><h5>${esc(t("数量关系"))}</h5><div class="production-quantity-grid">${quantityFacts
					.map(([label, value]) => `<div data-label="${esc(label)}"><span>${esc(label)}</span><strong>${number(value)}</strong></div>`)
					.join("")}</div></section>
				<section><h5>${esc(t("关联生产计划"))}</h5><p class="text-muted">${esc(t("现货与在途供应统一按 Production Plan 的计划日期优先分配。"))}</p><div class="production-plan-list">${productionPlans}</div></section>
				<section><h5>${esc(t("关联工单"))}</h5><div class="production-work-order-list">${workOrders}</div></section>
				<section><h5>${esc(t("工单直接用料"))}</h5><p class="text-muted">${esc(t("半成品由下级工单生产；只有底层采购件会进入采购缺口。采购动作提交前会再次复核。"))}</p>${materials}</section>
			</div>
		</details>`;
}

function refreshProductionOverview(page, demandKey) {
	if (!page || !page.production_workbench) return;
	const { state, loadOverview } = page.production_workbench;
	state.filters.search = demandKey || "";
	if (state.pagination) state.pagination.page = 1;
	state.expandedDemands.clear();
	if (demandKey) state.expandedDemands.add(demandKey);
	return loadOverview();
}

const productionWorkbenchApi = {
	filterProductionDemands,
	productionMaterialStatusMeta,
	workOrderReadinessMeta,
	productionStatusMeta,
	productionSummary,
	workbenchPaginationHtml: workbenchPaginationHtmlSafe,
	productionDemandHtml,
	refreshProductionOverview,
};

if (typeof module !== "undefined" && module.exports) {
	module.exports = productionWorkbenchApi;
}

if (typeof frappe !== "undefined") {
	frappe.pages["production-workbench"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({ parent: wrapper, title: __("生产工作台"), single_column: true });
		page.main.html(`
			<div class="process-simplification-page production-workbench">
				<div class="production-kpis"></div>
				<div class="production-filter-bar">
					<input class="form-control production-search" data-filter="search" placeholder="${__("搜索订单、客户、产品或工单")}">
					<select class="form-control" data-filter="deliveryWindow"><option value="">${__("全部交期")}</option><option value="overdue">${__("已逾期")}</option><option value="today">${__("今日交期")}</option><option value="within_7_days">${__("7 天内交期")}</option><option value="later">${__("稍后交期")}</option><option value="missing">${__("缺少交期")}</option></select>
					<select class="form-control" data-filter="status"><option value="">${__("全部状态")}</option><option value="master_data_blocked">${__("基础资料异常")}</option><option value="planning_required">${__("待创建生产计划")}</option><option value="legacy_work_order">${__("旧工单未纳入计划")}</option><option value="material_shortage">${__("缺底层原材料")}</option><option value="awaiting_supply">${__("等待到料")}</option><option value="waiting_subassembly">${__("等待半成品")}</option><option value="ready_to_start">${__("可开工")}</option><option value="in_production">${__("生产中")}</option><option value="partially_completed">${__("部分完工")}</option><option value="awaiting_order_reservation">${__("待回补订单")}</option><option value="overplanned">${__("超计划生产")}</option></select>
					<select class="form-control" data-filter="risk"><option value="">${__("全部风险")}</option><option value="red">${__("高风险")}</option><option value="orange">${__("需关注")}</option><option value="blue">${__("处理中")}</option><option value="green">${__("正常")}</option></select>
					<select class="form-control" data-filter="customer"><option value="">${__("全部客户")}</option></select>
					<label><input type="checkbox" data-filter="shortageOnly"> ${__("只看缺料")}</label>
					<label><input type="checkbox" data-filter="unplannedOnly"> ${__("只看未纳入计划")}</label>
					<label><input type="checkbox" data-filter="showOther"> ${__("其他生产")}</label>
				</div>
				<div class="production-update-time text-muted"></div>
				<p class="text-muted production-sort-note">${__("生产物料竞争优先级：Production Plan 计划日期；页面展示仍按订单交期和风险排序。")}</p>
				<div class="production-demand-list"></div>
				<div class="production-pagination"></div>
				<div class="production-other-section"></div>
			</div>`);

		const $root = page.main.find(".production-workbench");
		const state = {
			data: { demands: [], other_work_orders: [], summary: {}, pagination: { page: 1, page_size: 20 } },
			filters: {},
			pagination: { page: 1, page_size: 20 },
			expandedDemands: new Set(),
		};
		page.production_workbench = { state, loadOverview };

		const helpers = () => ({
			translate: __,
			escapeHtml: frappe.utils.escape_html,
			formatNumber: (value) => format_number(flt(value), null, 2),
			formatDate: (value) => (value ? frappe.datetime.str_to_user(value) : ""),
		});

		function visibleDemands() {
			return state.data.demands || [];
		}

		function renderKpis(summary) {
			const cards = [
				[__("未纳入生产计划"), summary.unplanned_demands, "orange"],
				[__("已逾期生产"), summary.overdue_demands, "red"],
				[__("7 天内到期"), summary.due_within_7_days, "orange"],
				[__("原料短缺"), summary.material_shortage_demands, "red"],
				[__("生产中"), summary.in_production_demands, "blue"],
				[__("待回补订单"), summary.awaiting_order_reservation_demands, "green"],
			];
			$root.find(".production-kpis").html(
				cards.map(([label, value, color]) => `<div class="production-kpi production-kpi-${color}"><span>${frappe.utils.escape_html(label)}</span><strong>${value}</strong></div>`).join("")
			);
		}

		function renderOtherWorkOrders() {
			if (!state.filters.showOther) {
				$root.find(".production-other-section").empty();
				return;
			}
			const rows = state.data.other_work_orders || [];
			const html = rows.length
				? rows.map((row) => `<div class="production-other-card"><a href="/app/work-order/${encodeURIComponent(row.name || "")}"><strong>${frappe.utils.escape_html(row.name || "")}</strong></a><span>${frappe.utils.escape_html(row.production_item || "")}</span><span>${frappe.utils.escape_html(row.status || "")}</span><strong>${format_number(flt(row.produced_qty), null, 2)} / ${format_number(flt(row.qty), null, 2)}</strong></div>`).join("")
				: `<div class="text-muted production-empty-section">${frappe.utils.escape_html(__("没有无订单来源的活动生产任务。"))}</div>`;
			$root.find(".production-other-section").html(`<h4>${frappe.utils.escape_html(__("其他生产"))}</h4>${html}`);
		}

		function render() {
			const demands = visibleDemands();
			renderKpis(state.data.summary || productionSummary(demands));
			for (const [name, value] of Object.entries(state.filters)) {
				const $field = $root.find(`[data-filter="${name}"]`);
				if ($field.is(":checkbox")) $field.prop("checked", Boolean(value));
				else $field.val(value || "");
			}
			$root.find(".production-demand-list").html(
				demands.length
					? demands.map((row) => productionDemandHtml(row, helpers())).join("")
					: `<div class="text-muted fulfillment-empty">${frappe.utils.escape_html(__("没有符合当前筛选条件的生产需求。"))}</div>`
			);
			$root.find(".production-demand").each((_, element) => {
				const $demand = $(element);
				$demand.prop("open", state.expandedDemands.has($demand.data("demand-key")));
				$demand.on("toggle", () => {
					const key = $demand.data("demand-key");
					if ($demand.prop("open")) state.expandedDemands.add(key);
					else state.expandedDemands.delete(key);
				});
			});
			$root.find(".production-pagination").html(workbenchPaginationHtmlSafe(state.data.pagination || state.pagination, helpers()));
			renderOtherWorkOrders();
		}

		function loadOverview() {
			return frappe.call({
				method: "process_simplification.api.production.get_production_overview",
				freeze: true,
				args: {
					page: state.pagination.page,
					page_size: state.pagination.page_size,
					filters: state.filters,
				},
				freeze_message: __("正在计算生产需求与物料风险..."),
			}).then((response) => {
				state.data = response.message || { demands: [], other_work_orders: [] };
				state.pagination = state.data.pagination || state.pagination;
				const customers = state.data.customers || [];
				$root.find('[data-filter="customer"]').html(
					[`<option value="">${frappe.utils.escape_html(__("全部客户"))}</option>`]
						.concat(customers.map((row) => `<option value="${frappe.utils.escape_html(row.value)}">${frappe.utils.escape_html(row.label)}</option>`))
						.join("")
				);
				$root.find(".production-update-time").text(
					state.data.checked_at ? `${__("数据更新于")} ${frappe.datetime.str_to_user(state.data.checked_at)}` : ""
				);
				render();
			});
		}

		function runAction(action, salesOrder, salesOrderItem) {
			if (action === "check_materials") return loadOverview();
			if (action === "view_sales_order") return frappe.set_route("Form", "Sales Order", salesOrder);
			if (action === "handle_shortage") {
				frappe.route_options = { selected_rows: [{ sales_order: salesOrder, sales_order_item: salesOrderItem }] };
				return frappe.set_route("shortage-purchase-planning");
			}
			const methods = {
				create_work_order: "process_simplification.api.actions.create_work_order",
				reserve_completed_stock: "process_simplification.api.actions.reserve_completed_stock",
			};
			if (!methods[action]) return;
			const demand = (state.data.demands || []).find((row) => row.sales_order_item === salesOrderItem);
			const message = action === "create_work_order"
				? `${__("确认按当前未安排数量创建生产计划并生成层级工单？")}<br>${frappe.utils.escape_html(demand?.item_code || "")} · ${format_number(flt(demand?.unplanned_production_qty), null, 2)}`
				: __("确认将当前可用完工成品回补到来源订单？");
			frappe.confirm(message, () => {
				frappe.call({ method: methods[action], args: { sales_order: salesOrder, sales_order_item: salesOrderItem }, freeze: true }).then((r) => {
					const created = (r && r.message) || {};
					let done = __("操作完成，已按最新数据重新检查。");
					if (action === "create_work_order") {
						const total = (created.work_orders || []).length;
						const sub = flt(created.sub_assembly_count);
						const plan = created.production_plan || "";
						done = sub > 0
							? __("已创建生产计划 {0}，并生成 {1} 个工单（含 {2} 个半成品工单），已按最新数据重新检查。", [plan, total, sub])
							: __("已创建生产计划 {0}，并生成 {1} 个工单，已按最新数据重新检查。", [plan, total]);
					}
					frappe.show_alert({ message: done, indicator: "green" });
					loadOverview();
				});
			});
		}

		$root.on("input change", "[data-filter]", (event) => {
			const $input = $(event.currentTarget);
			state.filters[$input.data("filter")] = $input.is(":checkbox") ? $input.prop("checked") : $input.val();
			state.pagination.page = 1;
			loadOverview();
		});
		$root.on("click", ".workbench-page-action", (event) => {
			state.pagination.page = Number($(event.currentTarget).data("page") || 1);
			loadOverview();
		});
		$root.on("change", ".workbench-page-size", (event) => {
			state.pagination.page = 1;
			state.pagination.page_size = Number($(event.currentTarget).val() || 20);
			loadOverview();
		});
		$root.on("click", ".production-action", (event) => {
			const $button = $(event.currentTarget);
			runAction($button.data("action"), $button.data("sales-order"), $button.data("row"));
		});
		page.add_inner_button(__("刷新"), loadOverview);
	};

	frappe.pages["production-workbench"].refresh = function (wrapper) {
		frappe.app.sidebar.set_workspace_sidebar();
		const demandKey = frappe.get_route()[1] || null;
		return refreshProductionOverview(wrapper.page, demandKey);
	};
}
