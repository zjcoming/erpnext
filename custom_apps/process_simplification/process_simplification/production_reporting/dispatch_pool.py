"""Read-only operation view and opt-in first-assignment orchestration.

Each write owns exactly one Job Card. Stock, wages, operation readiness and
assignment creation continue to belong to the existing reporting service.
"""

from __future__ import annotations

from math import ceil, isfinite

import frappe
from frappe import _
from frappe.utils import cint, flt

from process_simplification.production_reporting import service


MAX_PAGE_LENGTH = 20
INACTIVE_STATUSES = {"Closed", "Stopped", "Completed", "Cancelled"}


def is_enabled() -> bool:
	return frappe.conf.get("enable_operation_dispatch_pool") in (True, 1, "1")


def _require_access():
	if not is_enabled():
		frappe.throw(_("按工序集中派工尚未启用。"), frappe.PermissionError)
	service.require_reviewer()
	frappe.has_permission("Job Card", "read", throw=True)
	frappe.has_permission("Work Order", "read", throw=True)
	return service.reviewer_companies()


def _normalize_filters(filters):
	filters = frappe.parse_json(filters) if isinstance(filters, str) else filters
	if filters is not None and not isinstance(filters, dict):
		frappe.throw(_("工序池筛选条件必须是对象。"))
	filters = filters or {}
	result = {key: str(filters.get(key) or "").strip() for key in ("company", "operation", "search")}
	result["assignment_state"] = str(filters.get("assignment_state") or "all")
	if result["assignment_state"] not in {"all", "unassigned", "assigned"}:
		frappe.throw(_("无效的派工状态筛选。"))
	if any(len(value) > 140 for value in result.values()):
		frappe.throw(_("筛选文本不能超过 140 个字符。"))
	if filters.get("ready_only") or filters.get("readiness"):
		frappe.throw(_("首版工序池不支持跨分页的就绪状态筛选。"))
	return result


_ASSIGNMENT_EXISTS = "exists (select 1 from `tabJob Card Worker Assignment` a where a.job_card = jc.name)"
_REPORT_EXISTS = "exists (select 1 from `tabJob Card Work Report` r where r.job_card = jc.name)"
_TIME_EXISTS = "exists (select 1 from `tabJob Card Time Log` t where t.parent = jc.name and t.parenttype = 'Job Card')"
_MOVEMENT_EXISTS = "exists (select 1 from `tabJob Card Assignment Movement` m where m.job_card = jc.name)"


def _pool_query_scope(filters, companies):
	conditions = [
		"jc.docstatus = 0", "wo.docstatus = 1", "pp.docstatus = 1",
		"jc.company = wo.company", "pp.company = wo.company",
		"wo.status not in ('Closed', 'Stopped', 'Completed', 'Cancelled')",
		"ifnull(jc.is_corrective_job_card, 0) = 0",
		"ifnull(jc.track_semi_finished_goods, 0) = 0",
		"ifnull(jc.is_subcontracted, 0) = 0",
		"ifnull(jc.operation, '') != ''", "ifnull(jc.operation_id, '') != ''",
		"ifnull(jc.for_quantity, 0) > 0",
		"not exists (select 1 from `tabJob Card Operation` subop where subop.parent = jc.name and subop.parenttype = 'Job Card' and subop.parentfield = 'sub_operations')",
	]
	values = {}
	if companies is not None:
		if not companies:
			conditions.append("1 = 0")
		else:
			conditions.append("jc.company in %(companies)s")
			values["companies"] = tuple(sorted(companies))
	for field in ("company", "operation"):
		if filters[field]:
			conditions.append(f"jc.{field} = %({field})s")
			values[field] = filters[field]
	if filters["search"]:
		conditions.append("(jc.name like %(search)s or wo.name like %(search)s or wo.sales_order like %(search)s or wo.production_plan like %(search)s or jc.production_item like %(search)s or item.item_name like %(search)s)")
		values["search"] = "%" + filters["search"] + "%"
	if filters["assignment_state"] != "all":
		conditions.append(("not " if filters["assignment_state"] == "unassigned" else "") + _ASSIGNMENT_EXISTS)
	joins = """
		from `tabJob Card` jc
		inner join `tabWork Order` wo on wo.name = jc.work_order
		inner join `tabProduction Plan` pp on pp.name = wo.production_plan
		left join `tabItem` item on item.name = jc.production_item
	"""
	return joins + " where " + " and ".join(conditions), values


def get_operation_dispatch_pool(page=1, page_length=20, filters=None):
	companies = _require_access()
	filters = _normalize_filters(filters)
	if filters["company"] and companies is not None and filters["company"] not in companies:
		frappe.throw(_("无权查看该公司的生产任务。"), frappe.PermissionError)
	page = max(cint(page), 1)
	page_length = min(max(cint(page_length), 1), MAX_PAGE_LENGTH)
	scope, values = _pool_query_scope(filters, companies)
	total = cint(frappe.db.sql("select count(*) " + scope, values)[0][0])
	values.update(offset=(page - 1) * page_length, page_length=page_length)
	rows = frappe.db.sql(
		f"""select jc.name as job_card, jc.work_order, jc.company,
			jc.production_item, item.item_name, item.description, item.stock_uom,
			jc.operation, jc.operation_id, jc.workstation, jc.sequence_id,
			jc.for_quantity, jc.total_completed_qty as completed_qty, jc.process_loss_qty,
			wo.production_plan, wo.sales_order, wo.expected_delivery_date as delivery_date,
			{_ASSIGNMENT_EXISTS} as has_assignment_history,
			({_REPORT_EXISTS} or {_TIME_EXISTS}) as has_work_history,
			{_MOVEMENT_EXISTS} as has_movement_history
		{scope}
		order by jc.operation, ifnull(wo.expected_delivery_date, '9999-12-31'), wo.name, jc.sequence_id, jc.name
		limit %(page_length)s offset %(offset)s""",
		values, as_dict=True,
	)
	contexts = {}
	for row in rows:
		if row.work_order not in contexts:
			contexts[row.work_order] = service.get_work_order_assignment_context(row.work_order)
		context = contexts[row.work_order]
		card = next((card for card in context.get("job_cards", []) if card["name"] == row.job_card), None)
		row.update(_row_dispatch_state(row, card))
	company_filters = {"name": ["in", sorted(companies)]} if companies is not None else {}
	return {
		"rows": rows,
		"pagination": {"page": page, "page_length": page_length, "total_count": total,
			"total_pages": ceil(total / page_length), "has_more": page * page_length < total},
		"filters": filters,
		"companies": frappe.get_list("Company", filters=company_filters, pluck="name", order_by="name", limit_page_length=0),
	}


def _row_dispatch_state(row, card):
	card = card or {}
	block_code = card.get("block_code")
	block_message = card.get("block_message")
	if (
		row.get("has_assignment_history")
		or row.get("has_work_history")
		or row.get("has_movement_history")
		or card.get("assignments")
		or flt(row.get("completed_qty"))
		or flt(row.get("process_loss_qty"))
	):
		block_code = "EXISTING_HISTORY"
		block_message = _("已有派工或生产历史，请通过原单卡入口查看或调整。")
	elif not card:
		block_code = "TASK_CHANGED"
		block_message = _("任务状态已变化，请刷新后重试。")
	return {
		"can_assign_first": bool(card.get("can_assign") and not block_code),
		"block_code": block_code, "block_message": block_message,
		"remaining_qty": flt(card.get("remaining_qty")),
		"assignments": card.get("assignments", []),
		"assignment_supervisor": card.get("assignment_supervisor"),
		"can_choose_supervisor": bool(card.get("can_choose_supervisor")),
	}


def assign_workers_first(job_card, assignments, expected_qty, supervisor=None,
	*, expected_work_order=None, expected_operation_id=None):
	companies = _require_access()
	jc = service.job_card_values(job_card, for_update=True)
	if not jc:
		frappe.throw(_("生产任务不存在或已被移除。"))
	if companies is not None and jc.company not in companies:
		frappe.throw(_("无权处理该公司的生产任务。"), frappe.PermissionError)
	work_order = frappe.db.get_value("Work Order", jc.work_order,
		["name", "company", "status", "docstatus", "production_plan"], as_dict=True, for_update=True)
	if (not work_order or work_order.company != jc.company or work_order.docstatus != 1
		or work_order.status in INACTIVE_STATUSES or not work_order.production_plan):
		frappe.throw(_("仅支持已纳入生产计划且可执行的正式工单。"))
	plan = frappe.db.get_value("Production Plan", work_order.production_plan, ["company", "docstatus"], as_dict=True)
	if not plan or plan.company != jc.company or plan.docstatus != 1:
		frappe.throw(_("生产计划已失效，请刷新后通过原入口处理。"))
	try:
		quantity = float(expected_qty)
	except (TypeError, ValueError):
		frappe.throw(_("必须提供预览时的有效任务量。"))
	if not isfinite(quantity) or quantity <= 0 or flt(quantity, service.job_card_qty_precision()) != flt(jc.for_quantity, service.job_card_qty_precision()):
		frappe.throw(_("任务量已变化，请刷新并重新确认。"))
	if (expected_work_order is not None and expected_work_order != jc.work_order
		or expected_operation_id is not None and expected_operation_id != jc.operation_id):
		frappe.throw(_("任务所属工单或工序已变化，请刷新并重新确认。"))
	# This remains the sole business write path, including material reservations,
	# native sequence validation, worker/role checks, wage rules and notifications.
	# Its first-only guard runs AFTER its normal locks, using current reads; an
	# early nonlocking history query here could miss a concurrent committed plan.
	docs = service.assign_workers(job_card, assignments, supervisor, first_only=True)
	return {"status": "assigned", "job_card": job_card, "assignments": docs}
