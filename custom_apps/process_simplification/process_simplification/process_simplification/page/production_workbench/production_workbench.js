const productionWorkbenchItemIdentity = typeof module !== "undefined" && module.exports
	? require("../../../public/js/item_identity.js")
	: window.process_simplification.item_identity;

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
		ready_now: { label: translate("物料已齐，待发料"), indicator: "green" },
		waiting_subassembly: { label: translate("等待半成品"), indicator: "blue" },
		awaiting_purchase_receipt: { label: translate("等待采购到货"), indicator: "blue" },
		purchase_request_pending: { label: translate("等待采购下单"), indicator: "orange" },
		purchase_shortage: { label: translate("缺底层原材料"), indicator: "red" },
		production_task_missing: { label: translate("缺少下级生产任务"), indicator: "red" },
		replenishment_required: { label: translate("缺料，待生成补产任务"), indicator: "orange" },
		materials_transferred: { label: translate("已发料"), indicator: "green" },
		in_progress: { label: translate("生产中"), indicator: "blue" },
		completed: { label: translate("已完成"), indicator: "green" },
		blocked: { label: translate("已阻塞"), indicator: "red" },
	};
	return statuses[status] || { label: translate("待判断"), indicator: "gray" };
}

function workOrderOperationMeta(code, translate = (message) => message) {
	const statuses = {
		pending: { label: translate("工序待开始"), indicator: "gray" },
		in_progress: { label: translate("工序进行中"), indicator: "blue" },
		completed: { label: translate("所有工序已完成"), indicator: "green" },
		not_applicable: { label: translate("无工序"), indicator: "gray" },
	};
	return statuses[code] || null;
}

function workOrderReceiptMeta(code, translate = (message) => message) {
	const statuses = {
		not_ready: { label: translate("入库未就绪"), indicator: "gray" },
		requestable: { label: translate("待申请入库"), indicator: "orange" },
		draft_pending: { label: translate("入库申请处理中"), indicator: "blue" },
		received: { label: translate("已入库"), indicator: "green" },
	};
	return statuses[code] || null;
}

function workOrderIssueMeta(code, translate = (message) => message) {
	const statuses = {
		not_required: { label: translate("无需发料"), indicator: "gray" },
		waiting_material: { label: translate("等待物料"), indicator: "red" },
		partially_ready: { label: translate("可部分发料"), indicator: "orange" },
		ready: { label: translate("可申请发料"), indicator: "green" },
		draft_pending: { label: translate("发料申请处理中"), indicator: "blue" },
		partially_issued: { label: translate("已部分发料"), indicator: "orange" },
		issued: { label: translate("已发料"), indicator: "green" },
	};
	return statuses[code] || null;
}

function workOrderAssignmentActionMeta(
	workOrder = {},
	hasProductionPlan = true,
	translate = (message) => message
) {
	const terminalStatuses = new Set(["Completed", "Stopped", "Closed", "Cancelled"]);
	const isTerminal = terminalStatuses.has(workOrder.status) || Number(workOrder.docstatus) === 2;
	if (!workOrder.name) {
		return null;
	}
	const historyAction = Number(workOrder.worker_assignment_history_count || 0) > 0
		? { label: translate("查看派工记录"), primary: false, mode: "history" }
		: null;
	if (!hasProductionPlan || isTerminal || workOrder.operation_state?.code === "completed" || ["requestable", "draft_pending", "received"].includes(workOrder.receipt_state?.code)) {
		return historyAction;
	}

	if (
		workOrder.can_dispatch === true ||
		(workOrder.can_dispatch === undefined && workOrder.readiness_status === "materials_transferred")
	) {
		return { label: translate("派工"), primary: true, mode: "assign" };
	}
	return historyAction;
}

function workOrderNextActionMeta(workOrder = {}, translate = (message) => message) {
	const terminalStatuses = new Set(["Completed", "Stopped", "Closed", "Cancelled"]);
	if (!workOrder.name || terminalStatuses.has(workOrder.status) || Number(workOrder.docstatus) === 2) {
		return null;
	}
	const receipt = workOrder.receipt_state || {};
	const issue = workOrder.issue_state || {};
	if (receipt.code === "draft_pending" && receipt.draft_entry?.name) {
		return { label: translate("查看入库申请"), action: "open_stock_entry", document: receipt.draft_entry.name, withdrawalDocument: receipt.draft_entry.name };
	}
	if (receipt.code === "requestable" || workOrder.flow_status === "awaiting_receipt_request") {
		return { label: translate("申请入库"), action: "request_manufacture", primary: true };
	}
	if (issue.code === "draft_pending" && issue.draft_entry?.name) {
		return { label: translate("查看发料申请"), action: "open_stock_entry", document: issue.draft_entry.name, withdrawalDocument: issue.draft_entry.name };
	}
	if (issue.code === "partially_ready") {
		const partialQty = Number(issue.additional_issueable_qty || 0);
		if (partialQty <= 0) return null;
		return {
			label: translate("申请部分发料"),
			action: "request_material_issue",
			primary: true,
			allowPartial: true,
			qty: partialQty,
		};
	}
	if (issue.code === "ready" || (!issue.code && workOrder.readiness_status === "ready_now")) {
		return { label: translate("申请发料"), action: "request_material_issue", primary: true };
	}
	const priorityAllocationConflicts = (workOrder.required_items || []).flatMap(
		(item) => (item.allocation_conflict?.sources || []).filter(
			(source) => source.source_type === "priority_allocation"
		)
	);
	if (
		new Set(["waiting_material", "partially_issued"]).has(issue.code)
		&& priorityAllocationConflicts.length
	) {
		const hasReturnGap = (workOrder.required_items || []).some(
			(item) => Number(item.returned_qty || 0) > 0 && Number(item.remaining_issue_qty || 0) > 0
		);
		return {
			label: translate(hasReturnGap ? "调整优先级并申请补发" : "调整优先级并申请发料"),
			action: "request_material_issue_override",
			primary: false,
			allowPartial: true,
			overridePriority: true,
		};
	}
	const replenishmentItem = (workOrder.required_items || []).find(
		(item) => item.replenishment_required && item.name
	);
	if (replenishmentItem) {
		return {
			label: translate("生成补产任务"),
			action: "create_replenishment",
			primary: true,
			workOrderItem: replenishmentItem.name,
		};
	}
	return null;
}

function workOrderActionConfirmation(action, workOrder = {}, translate = (message) => message) {
	if (action === "request_manufacture") {
		return translate("确认发起完工入库申请？本操作只创建入库待办，库存将在库房提交入库单后增加。");
	}
	if (action === "request_material_issue_override") {
		return translate("当前物料已按交期优先分配给其他工单。继续会改变原分配并可能使高优先级工单缺料；硬预留仍不会被抢用。确认继续申请发料？");
	}
	if (action === "request_material_issue") {
		if (workOrder.issue_state?.code === "partially_ready") {
			return translate("当前只能部分发料。确认创建部分发料待办？剩余缺料未补齐前不会允许正式派工。");
		}
		return translate("确认发起生产发料申请？提交前会重新检查实际库存和硬预留；库房完成发料前不会通知工人开工。");
	}
	if (action === "create_replenishment") {
		return translate("确认生成补产任务？系统会创建新的补产申请、生产计划和工单，不会重新打开已经完成的原工单。");
	}
	if (action === "withdraw_stock_request") {
		return translate("确认撤回该库存申请？草稿会删除，关联的硬预留会立即释放；如仍需处理，必须重新发起申请。");
	}
	return "";
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
		awaiting_receipt: { indicator: "blue" },
		awaiting_issue: { indicator: "blue" },
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
			...(demand.work_orders || []).flatMap((row) => [
				row.name,
				row.production_item,
				row.production_item_name,
				...(row.required_items || []).flatMap((item) => [item.item_code, item.item_name]),
			]),
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
			summary.unchecked_material_demands += Number(demand.material_summary?.status_code === "not_checked");
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
			unchecked_material_demands: 0,
			in_production_demands: 0,
			awaiting_order_reservation_demands: 0,
		}
	);
}

function aggregatePurchasedMaterials(materials) {
	const groups = new Map();
	for (const source of materials || []) {
		if (source.supply_type === "manufactured") continue;
		const warehouse = source.warehouse || source.source_warehouse || "";
		const key = `${source.item_code || ""}\u0000${warehouse}`;
		if (!groups.has(key)) {
			groups.set(key, {
				item_code: source.item_code,
				item_name: source.item_name,
				stock_uom: source.stock_uom,
				warehouse,
				supply_type: "purchased",
				required_qty: 0,
				actual_qty: 0,
				committed_qty: 0,
				available_qty: 0,
				open_material_request_qty: 0,
				open_purchase_order_qty: 0,
				current_gap_qty: 0,
				shortage_qty: 0,
				blocked: false,
				is_shared: false,
				source_work_orders: [],
				supply_documents: [],
				_source_work_order_names: new Set(),
				_supply_documents: new Map(),
			});
		}
		const group = groups.get(key);
		group.item_name = group.item_name || source.item_name;
		group.stock_uom = group.stock_uom || source.stock_uom;
		group.required_qty += Number(source.source_required_qty ?? source.required_qty ?? 0);
		group.actual_qty = Math.max(group.actual_qty, Number(source.actual_qty || 0));
		group.committed_qty = Math.max(group.committed_qty, Number(source.committed_qty || 0));
		group.available_qty += Number(source.available_qty || 0);
		group.open_material_request_qty += Number(source.open_material_request_qty || 0);
		group.open_purchase_order_qty += Number(source.open_purchase_order_qty || 0);
		group.current_gap_qty += Number(source.current_gap_qty || 0);
		group.shortage_qty += Number(source.shortage_qty || 0);
		group.blocked ||= Boolean(source.blocked);
		group.is_shared ||= Boolean(source.is_shared);
		if (source.work_order && !group._source_work_order_names.has(source.work_order)) {
			group._source_work_order_names.add(source.work_order);
			const sourceWorkOrder = {
				name: source.work_order,
				production_item: source.production_item || "",
			};
			if (source.production_item_name) {
				sourceWorkOrder.production_item_name = source.production_item_name;
			}
			group.source_work_orders.push(sourceWorkOrder);
		}
		for (const sourceDocument of source.supply_documents || []) {
			const documentKey = `${sourceDocument.doctype || ""}\u0000${sourceDocument.detail_name || sourceDocument.name || ""}`;
			if (!group._supply_documents.has(documentKey)) {
				group._supply_documents.set(documentKey, {
					...sourceDocument,
					allocated_qty: 0,
				});
			}
			const document = group._supply_documents.get(documentKey);
			document.allocated_qty += Number(sourceDocument.allocated_qty || 0);
			document.outstanding_qty = Math.max(
				Number(document.outstanding_qty || 0),
				Number(sourceDocument.outstanding_qty || 0)
			);
			document.is_late ||= Boolean(sourceDocument.is_late);
		}
	}

	return [...groups.values()]
		.map((group) => {
			group.source_work_orders.sort((left, right) => left.name.localeCompare(right.name));
			group.supply_documents = [...group._supply_documents.values()];
			group.is_shared ||= group.source_work_orders.length > 1;
			group.status = group.blocked
				? "cannot_calculate"
				: group.current_gap_qty <= 0
					? "ready_now"
					: group.shortage_qty > 0
						? "new_purchase_required"
						: group.open_purchase_order_qty >= group.current_gap_qty
							? "awaiting_purchase_receipt"
							: "purchase_request_pending";
			delete group._source_work_order_names;
			delete group._supply_documents;
			return group;
		})
		.sort((left, right) => `${left.warehouse}\u0000${left.item_code}`.localeCompare(`${right.warehouse}\u0000${right.item_code}`));
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

function workOrderDirectMaterialsHtml(workOrder, helpers, hasProductionPlan) {
	const t = helpers.translate;
	const esc = helpers.escapeHtml;
	const number = helpers.formatNumber;
	const items = workOrder.required_items || [];
	if (!items.length) {
		const message = hasProductionPlan
			? t("当前工单没有剩余直接用料。")
			: t("旧工单未纳入生产计划，不在此计算直接用料。");
		return `<div class="text-muted production-work-order-material-empty">${esc(message)}</div>`;
	}
	return `<div class="production-work-order-material-list">${items
		.map((item) => {
			const status = productionMaterialStatusMeta(item.status, t);
			const completedLabel = workOrder.issue_state?.code === "not_required"
				? t("已消耗")
				: t("净发料");
			const completedQty = item.net_transferred_qty
				?? (workOrder.issue_state?.code === "not_required"
					? item.consumed_qty
					: Math.max(Number(item.transferred_qty || 0) - Number(item.returned_qty || 0), 0));
			const isManufactured = item.supply_type === "manufactured";
			const workOrderLinks = (rows) => (rows || [])
				.map((row) => row?.name || row)
				.filter(Boolean)
				.map((name) => `<a href="/app/work-order/${encodeURIComponent(name)}"><strong>${esc(name)}</strong></a>`)
				.join("、");
			const completedChildWorkOrders = item.completed_child_work_orders || [];
			const replenishmentWorkOrders = item.supply_work_orders || [];
			let supply = esc(t("采购件"));
			if (isManufactured) {
				if (item.child_work_order) {
					supply = `${esc(t("由下级工单"))} ${workOrderLinks([item.child_work_order])}`;
				} else if (replenishmentWorkOrders.length) {
					supply = `${esc(t("由补产工单"))} ${workOrderLinks(replenishmentWorkOrders)}`;
				} else if (completedChildWorkOrders.length) {
					const pendingIssueQty = Number(item.required_qty || 0);
					const currentGapQty = Number(item.current_gap_qty || 0);
					const label = pendingIssueQty <= 0
						? t("下级工单已完成，已发料")
						: currentGapQty <= 0
							? t("下级工单已完成，可发料")
							: t("原下级工单已完成，待补产");
					supply = `${esc(label)} ${workOrderLinks(completedChildWorkOrders)}`;
				} else {
					supply = `<span class="text-danger">${esc(t("缺少下级工单"))}</span>`;
				}
			}
			const supplyTasks = (item.supply_work_orders || []).length
				? `<div class="text-muted">${esc(t("补产任务"))}: ${(item.supply_work_orders || []).map((row) => `<a href="/app/work-order/${encodeURIComponent(row.name || row)}">${esc(row.name || row)}</a>`).join("、")}</div>`
				: "";
			const allocationSources = item.allocation_conflict?.sources || [];
			const hasPriorityAllocation = allocationSources.some(
				(source) => source.source_type === "priority_allocation"
			);
			const replenishmentAction = helpers.canManageAssignments && item.replenishment_required && item.name
				? `<button type="button" class="btn btn-xs btn-default production-work-order-action" data-action="create_replenishment" data-work-order="${esc(workOrder.name || "")}" data-work-order-item="${esc(item.name)}">${esc(t(hasPriorityAllocation ? "保留优先分配并生成补产任务" : "生成补产任务"))}</button>`
				: "";
			const allocationWarning = allocationSources.length
				? `<div class="text-danger production-allocation-warning">${esc(t("优先分配影响"))}: ${allocationSources.map((source) => `${esc(source.work_order || source.sales_order || t("其他工单"))} ${number(source.impact_qty || 0)}`).join("、")}</div>`
				: "";
			return `<div class="production-work-order-material-row">
				<div class="production-work-order-material-name">${productionWorkbenchItemIdentity.itemIdentityHtml(
					item.item_code,
					item.item_name,
					{ translate: t, escapeHtml: esc },
					{ linkToItem: true }
				)}</div>
				<div data-label="${esc(t("需求"))}">${number(item.original_required_qty ?? item.required_qty)} ${esc(item.stock_uom || "")}</div>
				<div data-label="${esc(completedLabel)}">${number(completedQty)}</div>
				<div data-label="${esc(t("待备料"))}">${number(item.required_qty)}</div>
				<div data-label="${esc(t("本次可用"))}">${number(item.available_qty)}</div>
				<div data-label="${esc(t("即时缺口"))}">${number(item.current_gap_qty)}</div>
				<div data-label="${esc(t("供应方式"))}">${supply}</div>
				<div data-label="${esc(t("状态"))}"><span class="indicator-pill ${esc(status.indicator)}">${esc(status.label)}</span>${allocationWarning}${replenishmentAction}${supplyTasks}</div>
			</div>`;
		})
		.join("")}</div>`;
}

function productionNextTask(demand, helpers) {
	const candidates = [];
	for (const workOrder of demand.work_orders || []) {
		const action = workOrderNextActionMeta(workOrder, helpers.translate);
		if (action && (action.action === "open_stock_entry"
			? helpers.canReadStockEntries || helpers.canManageAssignments
			: helpers.canManageAssignments)) {
			const priority = { request_manufacture: 0, open_stock_entry: 1, request_material_issue: 2, create_replenishment: 4, request_material_issue_override: 5 };
			candidates.push({ workOrder, ...action, priority: priority[action.action] ?? 9 });
		}
		const assignment = workOrderAssignmentActionMeta(workOrder, Boolean(demand.production_plans?.length), helpers.translate);
		if (helpers.canManageAssignments && assignment?.mode === "assign") {
			candidates.push({ workOrder, ...assignment, action: "assign", priority: 3 });
		}
	}
	return candidates.sort((a, b) => a.priority - b.priority)[0] || null;
}

function productionTaskButtonHtml(task, helpers) {
	if (!task) return "";
	const esc = helpers.escapeHtml;
	if (task.action === "assign") return `<button type="button" class="btn btn-primary production-assignment-action" data-work-order="${esc(task.workOrder.name)}" data-assignment-mode="assign">${esc(task.label)}</button>`;
	return `<button type="button" class="btn btn-primary production-work-order-action" data-action="${esc(task.action)}" data-work-order="${esc(task.workOrder.name)}" data-work-order-item="${esc(task.workOrderItem || "")}" data-document="${esc(task.document || "")}" data-allow-partial="${task.allowPartial ? "1" : "0"}" data-override-priority="${task.overridePriority ? "1" : "0"}" data-qty="${esc(task.qty || "")}">${esc(task.label)}</button>`;
}

function workOrderCardHtml(workOrder, sequence, helpers, hasProductionPlan, expanded = false) {
	const t = helpers.translate;
	const esc = helpers.escapeHtml;
	const number = helpers.formatNumber;
	const readiness = workOrder.receipt_state?.code === "requestable"
		? { label: t("待申请入库"), indicator: "orange" }
		: workOrder.receipt_state?.code === "draft_pending"
			? { label: t("入库申请处理中"), indicator: "blue" }
			: !hasProductionPlan && !workOrder.readiness_status
		? { label: t("未纳入生产计划"), indicator: "red" }
		: workOrderReadinessMeta(workOrder.readiness_status, t);
	const bom = workOrder.bom_no
		? `<a href="/app/bom/${encodeURIComponent(workOrder.bom_no)}"><strong>${esc(workOrder.bom_no)}</strong></a>`
		: esc(t("未设置"));
	const outputTarget = workOrder.parent_work_order
		? `${esc(t("供给上级工单"))}: <a href="/app/work-order/${encodeURIComponent(workOrder.parent_work_order)}"><strong>${esc(workOrder.parent_work_order)}</strong></a>${workOrder.parent_item_code ? ` · ${esc(productionWorkbenchItemIdentity.itemIdentityText(workOrder.parent_item_code, workOrder.parent_item_name, t))}` : ""}`
		: esc(t("最终成品工单"));
	const assignmentMeta = workOrderAssignmentActionMeta(workOrder, hasProductionPlan, t);
	const assignmentAction = helpers.canManageAssignments && assignmentMeta
		? `<button type="button" class="btn btn-xs ${assignmentMeta.primary ? "btn-primary" : "btn-default"} production-assignment-action" data-work-order="${esc(workOrder.name || "")}" data-assignment-mode="${esc(assignmentMeta.mode || "assign")}">${esc(assignmentMeta.label)}</button>`
		: "";
	const nextActionMeta = workOrderNextActionMeta(workOrder, t);
	const canRunNextAction = nextActionMeta?.action === "open_stock_entry"
		? (helpers.canReadStockEntries || helpers.canManageAssignments)
		: helpers.canManageAssignments;
	const nextAction = canRunNextAction && nextActionMeta
		? `<button type="button" class="btn btn-xs ${nextActionMeta.primary ? "btn-primary" : "btn-default"} production-work-order-action" data-action="${esc(nextActionMeta.action)}" data-work-order="${esc(workOrder.name || "")}" data-work-order-item="${esc(nextActionMeta.workOrderItem || "")}" data-document="${esc(nextActionMeta.document || "")}" data-allow-partial="${nextActionMeta.allowPartial ? "1" : "0"}" data-override-priority="${nextActionMeta.overridePriority ? "1" : "0"}" data-qty="${esc(nextActionMeta.qty || "")}">${esc(nextActionMeta.label)}</button>`
		: "";
	const withdrawalAction = helpers.canManageAssignments && nextActionMeta?.withdrawalDocument
		? `<button type="button" class="btn btn-xs btn-default production-work-order-action" data-action="withdraw_stock_request" data-work-order="${esc(workOrder.name || "")}" data-document="${esc(nextActionMeta.withdrawalDocument)}">${esc(t("撤回申请"))}</button>`
		: "";
	const stateBadges = [
		workOrderOperationMeta(workOrder.operation_state?.code, t),
		workOrderReceiptMeta(workOrder.receipt_state?.code, t),
		workOrderIssueMeta(workOrder.issue_state?.code, t),
	]
		.filter(Boolean)
		.map((meta) => `<span class="indicator-pill ${esc(meta.indicator)}">${esc(meta.label)}</span>`)
		.join("");
	return `<details class="production-work-order-card" data-work-order-card="${esc(workOrder.name || "")}"${expanded ? " open" : ""}>
		<summary>
			<div class="production-work-order-heading"><span class="production-work-order-sequence">${esc(t("第"))} ${sequence} ${esc(t("步"))}</span><a href="/app/work-order/${encodeURIComponent(workOrder.name || "")}"><strong>${esc(workOrder.name || "")}</strong></a><span class="indicator-pill ${esc(readiness.indicator)}">${esc(readiness.label)}</span>${stateBadges}${nextAction}${withdrawalAction}${assignmentAction}</div>
			<strong class="production-work-order-product">${esc(workOrder.production_item_name || workOrder.production_item || "")}</strong>
		</summary>
		<div class="production-work-order-summary">
			<div data-label="${esc(t("生产物料"))}">${productionWorkbenchItemIdentity.itemIdentityHtml(
				workOrder.production_item,
				workOrder.production_item_name,
				{ translate: t, escapeHtml: esc },
				{ linkToItem: true }
			)}</div>
			<div data-label="${esc(t("计划数量"))}">${number(workOrder.qty)}</div>
			<div data-label="${esc(t("已生产"))}">${number(workOrder.produced_qty)}</div>
			<div data-label="${esc(t("剩余"))}">${number(Math.max(Number(workOrder.qty || 0) - Number(workOrder.produced_qty || 0), 0))}</div>
			<div data-label="BOM">${bom}</div>
			<div data-label="${esc(t("原料仓"))}">${esc(workOrder.source_warehouse || t("未设置"))}</div>
			<div data-label="${esc(t("在制仓"))}">${esc(workOrder.wip_warehouse || t("未设置"))}</div>
			<div data-label="${esc(t("成品仓"))}">${esc(workOrder.fg_warehouse || t("未设置"))}</div>
		</div>
		<div class="production-work-order-output">${outputTarget}</div>
		<details class="production-work-order-materials"><summary class="production-work-order-material-heading">${esc(t("查看本工单直接用料"))}</summary>${workOrderDirectMaterialsHtml(workOrder, helpers, hasProductionPlan)}</details>
	</details>`;
}

function purchaseMaterialSummaryHtml(materials, helpers) {
	const t = helpers.translate;
	const esc = helpers.escapeHtml;
	const number = helpers.formatNumber;
	if (!materials.length) {
		return `<div class="text-muted production-empty-section">${esc(t("当前计划没有需要采购的底层物料。"))}</div>`;
	}
	return `<div class="production-material-table-wrap">
		<table class="table table-bordered production-material-table">
			<thead><tr><th>${esc(t("物料"))}</th><th>${esc(t("需求数量"))}</th><th>${esc(t("来源工单"))}</th><th>${esc(t("仓库库存"))}</th><th>${esc(t("已分配库存"))}</th><th>${esc(t("采购申请"))}</th><th>${esc(t("在途采购"))}</th><th>${esc(t("即时缺口"))}</th><th>${esc(t("采购缺口"))}</th><th>${esc(t("状态"))}</th></tr></thead>
			<tbody>${materials
				.map((row) => {
					const statusMeta = productionMaterialStatusMeta(row.status, t);
					const hasCurrentGap = Number(row.current_gap_qty || 0) > 0;
					const documents = (row.supply_documents || []).filter(
						(document) => hasCurrentGap || Number(document.allocated_qty || 0) > 0
					);
					const documentList = documents.length
						? `<tr class="production-supply-docs"><td colspan="10" data-label="${esc(t("采购单据"))}"><div class="production-supply-doc-list">${documents
							.map((document) => {
								const typeLabel = document.doctype === "Material Request" ? t("采购申请") : t("采购单");
								const deadlineTag = document.deadline_unknown
									? ` · <span class="indicator-pill orange">${esc(t("交期不可核验"))}</span>`
									: document.is_late
										? ` · <span class="indicator-pill red">${esc(t("晚于订单交期"))}</span>`
										: "";
								const allocation = Number(document.allocated_qty || 0) > 0
									? ` · ${esc(t("已分配给本单"))} ${number(document.allocated_qty)}`
									: ` · ${esc(t("未分配给本单"))}`;
								return `<a class="production-supply-doc${document.is_late ? " is-late" : ""}" href="/app/${esc(frappe.router.slug(document.doctype))}/${esc(document.name)}" target="_blank"><span class="production-supply-doc-type">${esc(typeLabel)}</span> <strong>${esc(document.name)}</strong> · <span class="indicator-pill grey">${esc(document.status || "")}</span> · ${esc(t("未完成"))} ${number(document.outstanding_qty)}${allocation}${document.schedule_date ? ` · ${esc(t("交期"))} ${esc(frappe.datetime.str_to_user(document.schedule_date))}` : ""}${deadlineTag}</a>`;
							})
							.join("")}</div></td></tr>`
						: "";
					const sources = row.source_work_orders.length
						? row.source_work_orders
							.map((source) => `<a href="/app/work-order/${encodeURIComponent(source.name)}"><strong>${esc(source.name)}</strong>${source.production_item ? `<small>${esc(productionWorkbenchItemIdentity.itemIdentityText(source.production_item, source.production_item_name, t))}</small>` : ""}</a>`)
							.join("")
						: `<span class="text-muted">${esc(t("未设置工单"))}</span>`;
					return `<tr>
						<td data-label="${esc(t("物料"))}">${productionWorkbenchItemIdentity.itemIdentityHtml(
							row.item_code,
							row.item_name,
							{ translate: t, escapeHtml: esc },
							{ linkToItem: true }
						)}<small>${esc(row.warehouse || t("未设置仓库"))}${row.is_shared ? ` · <span class="production-shared-material">${esc(t("多工单共用"))}</span>` : ""} · ${esc(t("采购件"))}</small></td>
						<td data-label="${esc(t("需求数量"))}">${number(row.required_qty)} ${esc(row.stock_uom || "")}</td>
						<td data-label="${esc(t("来源工单"))}"><div class="production-material-sources">${sources}</div></td>
						<td data-label="${esc(t("仓库库存"))}">${number(row.actual_qty)}</td>
						<td data-label="${esc(t("已分配库存"))}">${number(row.available_qty)}</td>
						<td data-label="${esc(t("采购申请"))}">${number(row.open_material_request_qty)}</td>
						<td data-label="${esc(t("在途采购"))}">${number(row.open_purchase_order_qty)}</td>
						<td data-label="${esc(t("即时缺口"))}">${number(row.current_gap_qty)}</td>
						<td data-label="${esc(t("采购缺口"))}">${number(row.shortage_qty)}</td>
						<td data-label="${esc(t("状态"))}"><span class="indicator-pill ${esc(statusMeta.indicator)}">${esc(statusMeta.label)}</span>${Number(row.shortage_qty || 0) > 0 && !documents.length ? `<br><small class="text-muted">${esc(t("尚未发起采购"))}</small>` : ""}</td>
					</tr>${documentList}`;
				})
				.join("")}</tbody>
		</table>
	</div>`;
}

function productionDemandHtml(demand, helpers) {
	const t = helpers.translate;
	const esc = helpers.escapeHtml;
	const number = helpers.formatNumber;
	const date = helpers.formatDate;
	const currentTask = productionNextTask(demand, helpers);
	const quantityFacts = [
		[t("订单待交"), demand.pending_qty],
		[t("有效预留"), demand.reserved_qty],
		[t("优先获配成品"), demand.available_to_reserve],
		[t("成品覆盖"), demand.finished_stock_coverage_qty],
		[t("需要生产"), demand.production_required_qty],
		[t("工单覆盖"), demand.active_work_order_qty],
		[t("未安排"), demand.unplanned_production_qty],
		[t("已完工"), demand.completed_qty],
		[t("当前可回补"), demand.completed_unreserved_qty],
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
						<span>${esc(t("计划开始"))}: ${esc(date(plan.planned_date)) || esc(t("未设置"))}</span>
						<span>${esc(t("物料优先依据"))}: ${esc(date(plan.material_priority_date)) || esc(t("未设置"))}</span>
						<span>${esc(t("可开工工单"))}: ${number(plan.summary?.ready_work_order_count)}</span>
						<span>${esc(t("等待半成品"))}: ${number(plan.summary?.waiting_subassembly_count)}</span>
					</div>`
				)
				.join("")
		: `<div class="text-muted production-empty-section">${esc(t("尚未关联生产计划。"))}</div>`;
	const workOrders = (demand.work_orders || []).length
		? (demand.work_orders || [])
				.map((row, index) => workOrderCardHtml(row, index + 1, helpers, hasProductionPlan, row.name === currentTask?.workOrder.name))
				.join("")
		: `<div class="text-muted production-empty-section">${esc(t("尚未创建工单。"))}</div>`;
	const emptyMaterialsMessage = !hasProductionPlan && (demand.work_orders || []).length
		? t("存在未关联 Production Plan 的旧工单。请先完成、停止或迁移旧工单；在此之前不计算可开工和缺料。")
		: !hasProductionPlan
			? t("请先创建生产计划；计划生成层级工单后才能检查工单直接用料和采购缺口。")
			: t("当前计划没有需要采购的底层物料。");
	const purchaseMaterials = hasProductionPlan
		? purchaseMaterialSummaryHtml(aggregatePurchasedMaterials(demand.materials || []), helpers)
		: `<div class="text-muted production-empty-section">${esc(emptyMaterialsMessage)}</div>`;
	const plannedStart = (demand.production_plans || []).find((plan) => plan.planned_date)?.planned_date;
	const primaryAction = (demand.next_actions || []).find(
		(row) => row.enabled !== false && !["view_sales_order", "check_materials"].includes(row.action)
	);
	const nextActionLabel = (currentTask ? `${currentTask.workOrder.production_item_name || currentTask.workOrder.production_item} · ${currentTask.label}` : null)
		|| primaryAction?.label
		|| demand.status_label
		|| t("查看详情");
	return `
		<details class="production-demand production-risk-${esc(demand.risk_level || "gray")}" data-demand-key="${esc(demand.demand_key)}">
			<summary>
				<div class="production-demand-source"><strong>${esc(demand.sales_order || "")}</strong><span>${esc(demand.customer_name || demand.customer || "")}</span></div>
				<div class="production-demand-product">${productionWorkbenchItemIdentity.itemIdentityHtml(
					demand.item_code,
					demand.item_name,
					{ translate: t, escapeHtml: esc },
					{ linkToItem: true, codeLabel: t("产品编码") }
				)}</div>
				<div class="production-demand-fact"><span>${esc(t("客户交期"))}</span><strong>${esc(date(demand.delivery_date)) || esc(t("未设置"))}</strong></div>
				<div class="production-demand-fact"><span>${esc(t("计划开工"))}</span><strong>${esc(date(plannedStart)) || esc(t("未安排"))}</strong></div>
				<div class="production-demand-fact"><span>${esc(t("需生产 / 未安排"))}</span><strong>${number(demand.production_required_qty)} / ${number(demand.unplanned_production_qty)}</strong></div>
				<div class="production-demand-risk"><span class="indicator-pill ${esc(demand.risk_level || "gray")}">${esc(demand.risk_label || "")}</span><span class="indicator-pill ${esc(productionStatusMeta(demand.status_code).indicator)}">${esc(demand.status_label || "")}</span></div>
				<span class="production-demand-next-action"><small>${esc(t("下一步"))}</small><strong>${esc(t(nextActionLabel))}</strong></span>
			</summary>
			<div class="production-demand-details">
				${currentTask ? `<div class="production-current-task"><div><small>${esc(t("当前待办"))}</small><strong>${esc(nextActionLabel)}</strong><a href="/app/work-order/${encodeURIComponent(currentTask.workOrder.name)}">${esc(currentTask.workOrder.name)}</a></div>${productionTaskButtonHtml(currentTask, helpers)}</div>` : ""}
				<div class="production-demand-actions">${actions}</div>
				<section><h5>${esc(t("生产执行链"))}</h5><p class="text-muted">${esc(t("展开工单查看详情；当前待办默认展开，已完成工单保留追溯。"))}</p><div class="production-work-order-list">${workOrders}</div></section>
				<details class="production-secondary-details"><summary>${esc(t("数量关系与生产计划"))}</summary><div class="production-quantity-grid">${quantityFacts
					.map(([label, value]) => `<div data-label="${esc(label)}"><span>${esc(label)}</span><strong>${number(value)}</strong></div>`)
					.join("")}</div><p class="text-muted">${esc(t("现货、原料与在途供应统一按订单行交付日期分配；计划开始仅用于生产排程。"))}</p><div class="production-plan-list">${productionPlans}</div></details>
				<details class="production-purchase-summary production-secondary-details"><summary>${esc(t("底层采购物料汇总"))}</summary><p class="text-muted">${esc(t("只汇总采购件；半成品在上方生产执行链中由下级工单供应。采购动作提交前会再次复核。"))}</p>${purchaseMaterials}</details>
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

function runProductionWorkbenchToolbarLoad(loadOverview) {
	loadOverview();
}

const productionWorkbenchApi = {
	productionNextTask,
	filterProductionDemands,
	productionMaterialStatusMeta,
	workOrderReadinessMeta,
	workOrderOperationMeta,
	workOrderReceiptMeta,
	workOrderIssueMeta,
	workOrderAssignmentActionMeta,
	workOrderNextActionMeta,
	workOrderActionConfirmation,
	productionStatusMeta,
	productionSummary,
	aggregatePurchasedMaterials,
	workbenchPaginationHtml: workbenchPaginationHtmlSafe,
	productionDemandHtml,
	refreshProductionOverview,
	runProductionWorkbenchToolbarLoad,
};

if (typeof module !== "undefined" && module.exports) {
	module.exports = productionWorkbenchApi;
}

if (typeof frappe !== "undefined") {
	frappe.pages["production-workbench"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({ parent: wrapper, title: __("生产计划中心"), single_column: true });
		page.main.html(`
			<div class="process-simplification-page production-workbench">
				<div class="production-kpis"></div>
				<details class="workbench-filter-panel" open>
					<summary><span>${__("筛选生产需求")}</span><small>${__("按交期、状态、风险或客户缩小范围")}</small></summary>
				<div class="production-filter-bar">
					<input class="form-control production-search" data-filter="search" placeholder="${__("搜索订单、客户、产品或工单")}">
					<select class="form-control" data-filter="deliveryWindow"><option value="">${__("全部交期")}</option><option value="overdue">${__("已逾期")}</option><option value="today">${__("今日交期")}</option><option value="within_7_days">${__("7 天内交期")}</option><option value="later">${__("稍后交期")}</option><option value="missing">${__("缺少交期")}</option></select>
					<select class="form-control" data-filter="status"><option value="">${__("全部状态")}</option><option value="master_data_blocked">${__("基础资料异常")}</option><option value="planning_required">${__("待创建生产计划")}</option><option value="legacy_work_order">${__("旧工单未纳入计划")}</option><option value="material_shortage">${__("缺底层原材料")}</option><option value="awaiting_supply">${__("等待到料")}</option><option value="waiting_subassembly">${__("等待半成品")}</option><option value="awaiting_receipt">${__("待完工入库")}</option><option value="awaiting_issue">${__("待生产发料")}</option><option value="ready_to_start">${__("可开工")}</option><option value="in_production">${__("生产中")}</option><option value="partially_completed">${__("部分完工")}</option><option value="awaiting_order_reservation">${__("待回补订单")}</option><option value="overplanned">${__("超计划生产")}</option></select>
					<select class="form-control" data-filter="risk"><option value="">${__("全部风险")}</option><option value="red">${__("高风险")}</option><option value="orange">${__("需关注")}</option><option value="blue">${__("处理中")}</option><option value="green">${__("正常")}</option></select>
					<select class="form-control" data-filter="customer"><option value="">${__("全部客户")}</option></select>
					<label><input type="checkbox" data-filter="shortageOnly"> ${__("只看缺料")}</label>
					<label><input type="checkbox" data-filter="unplannedOnly"> ${__("只看未纳入计划")}</label>
					<label><input type="checkbox" data-filter="showOther"> ${__("其他生产")}</label>
				</div>
				</details>
				<div class="production-update-time text-muted"></div>
				<p class="text-muted production-sort-note">${__("客户物料分配优先级：订单行交付日期；同交期按订单创建时间和订单行顺序。")}</p>
				<div class="production-demand-list"></div>
				<div class="production-pagination"></div>
				<div class="production-other-section"></div>
			</div>`);

		const $root = page.main.find(".production-workbench");
		if (window.matchMedia("(max-width: 767px)").matches) {
			$root.find(".workbench-filter-panel").prop("open", false);
		}
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
			canManageAssignments: Boolean(
				window.process_simplification?.can_manage_worker_assignments?.()
			),
			canReadStockEntries: Boolean(frappe.model.can_read("Stock Entry")),
		});

		function visibleDemands() {
			return state.data.demands || [];
		}

		function renderKpis(summary) {
			const cards = [
				[__("未纳入生产计划"), summary.unplanned_demands, "orange"],
				[__("已逾期生产"), summary.overdue_demands, "red"],
				[__("7 天内到期"), summary.due_within_7_days, "orange"],
				[__("已核料缺料"), summary.material_shortage_demands, "red"],
				[__("生产中"), summary.in_production_demands, "blue"],
				[__("待回补订单"), summary.awaiting_order_reservation_demands, "green"],
			];
			$root.find(".production-kpis").html(
				cards.map(([label, value, color]) => `<div class="production-kpi production-kpi-${color} ${Number(value || 0) ? "" : "is-zero"}"><span>${frappe.utils.escape_html(label)}</span><strong>${value}</strong></div>`).join("")
			);
			$("<span>").text(__("待核料") + " " + Number(summary.unchecked_material_demands || 0))
				.appendTo($root.find(".production-kpi").eq(3).toggleClass("is-zero",
					!Number(summary.material_shortage_demands || 0) && !Number(summary.unchecked_material_demands || 0)));
		}

		function renderOtherWorkOrders() {
			if (!state.filters.showOther) {
				$root.find(".production-other-section").empty();
				return;
			}
			const rows = state.data.other_work_orders || [];
			const html = rows.length
				? rows.map((row) => `<div class="production-other-card"><a href="/app/work-order/${encodeURIComponent(row.name || "")}"><strong>${frappe.utils.escape_html(row.name || "")}</strong></a>${productionWorkbenchItemIdentity.itemIdentityHtml(row.production_item, row.production_item_name, { translate: __, escapeHtml: frappe.utils.escape_html }, { linkToItem: true })}<span>${frappe.utils.escape_html(row.status || "")}</span><strong>${format_number(flt(row.produced_qty), null, 2)} / ${format_number(flt(row.qty), null, 2)}</strong></div>`).join("")
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
				(state.data.access_notice ? `<div class="alert alert-warning">${frappe.utils.escape_html(__(state.data.access_notice))}</div>` : "") + (demands.length
					? demands.map((row) => productionDemandHtml(row, helpers())).join("")
					: `<div class="text-muted fulfillment-empty">${frappe.utils.escape_html(__("没有符合当前筛选条件的生产需求。"))}</div>`)
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

		function loadOverview(options = {}) {
			return frappe.ps_read_page(page, {
				method: "process_simplification.page_refresh.production_overview",
				background: options.background,
				args: {
					page: state.pagination.page,
					page_size: state.pagination.page_size,
					filters: state.filters,
				},
				freeze_message: __("正在计算生产需求与物料风险..."),
				apply(response) {
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
				},
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
				? `${__("确认按当前未安排数量创建生产计划并生成层级工单？")}<br>${frappe.utils.escape_html(productionWorkbenchItemIdentity.itemIdentityText(demand?.item_code, demand?.item_name, __, __("产品编码")))} · ${format_number(flt(demand?.unplanned_production_qty), null, 2)}`
				: __("确认将当前可用完工成品回补到来源订单？");
			frappe.confirm(message, () => {
				frappe.call({ method: methods[action], type: "POST", args: { sales_order: salesOrder, sales_order_item: salesOrderItem }, freeze: true }).then((r) => {
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

		function runWorkOrderAction(action, workOrder, options = {}) {
			if (action === "open_stock_entry") {
				return frappe.set_route("Form", "Stock Entry", options.document);
			}
			const methods = {
				request_manufacture: "process_simplification.production_workflow.service.request_manufacture",
				request_material_issue: "process_simplification.production_workflow.service.request_material_issue",
				request_material_issue_override: "process_simplification.production_workflow.service.request_material_issue",
				create_replenishment: "process_simplification.production_workflow.service.create_replenishment_work_order",
				withdraw_stock_request: "process_simplification.production_workflow.service.withdraw_stock_request",
			};
			if (!methods[action]) return;
			const target = (state.data.demands || [])
				.flatMap((demand) => demand.work_orders || [])
				.find((row) => row.name === workOrder) || {};
			const message = workOrderActionConfirmation(action, target, __);
			frappe.confirm(message, () => {
				frappe.call({
					method: methods[action],
					type: "POST",
					args: {
						...(action === "withdraw_stock_request" ? { stock_entry: options.document } : { work_order: workOrder }),
						...(options.workOrderItem ? { work_order_item: options.workOrderItem } : {}),
						...(options.allowPartial ? { allow_partial: 1 } : {}),
						...(options.overridePriority || action === "request_material_issue_override" ? { override_priority: 1 } : {}),
						...(options.qty ? { qty: options.qty } : {}),
					},
					freeze: true,
				}).then((response) => {
					const result = response.message || {};
					frappe.show_alert({
						message: result.withdrawn
							? __("申请已撤回，关联库存预留已释放。")
							: result.reused ? __("已存在待处理单据，未重复创建。") : __("已创建待处理任务。"),
						indicator: "green",
					});
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
		$root.on("click", ".production-assignment-action", (event) => {
			event.preventDefault();
			event.stopPropagation();
			return window.process_simplification.open_worker_assignment_dialog({
				work_order: $(event.currentTarget).data("work-order"),
				mode: $(event.currentTarget).data("assignment-mode") || "assign",
				on_success: loadOverview,
			});
		});
		$root.on("click", ".production-work-order-action", (event) => {
			event.preventDefault();
			event.stopPropagation();
			const $button = $(event.currentTarget);
			return runWorkOrderAction($button.data("action"), $button.data("work-order"), {
				document: $button.data("document"),
				workOrderItem: $button.data("work-order-item"),
				allowPartial: Number($button.data("allow-partial") || 0) === 1,
				overridePriority: Number($button.data("override-priority") || 0) === 1,
				qty: Number($button.data("qty") || 0),
			});
		});
		page.add_inner_button(__("刷新"), () => runProductionWorkbenchToolbarLoad(loadOverview));
	};

	frappe.pages["production-workbench"].refresh = function (wrapper) {
		const route = frappe.get_route();
		if (route[1]) {
			return frappe.set_route("production-workbench", { demand_key: route[1] });
		}
		const demandKey = frappe.route_options?.demand_key || null;
		if (frappe.route_options) delete frappe.route_options.demand_key;
		return refreshProductionOverview(wrapper.page, demandKey);
	};
}
