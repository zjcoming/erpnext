"""Personnel plans are inert until an authorized worker explicitly starts work.

Saving or reading a plan never creates assignments, reserves stock or starts a
wage session. Activation deliberately delegates to the existing assignment and
work-session services in one request transaction, retaining their live checks.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from math import isfinite

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime, nowdate

from process_simplification.production_reporting import dispatch_pool, service


PLAN = "Job Card Prearrangement"
WORKER = "Job Card Prearrangement Worker"
DEFAULT = "Operation Worker Default"
MAX_CARDS = 20


def _enabled():
	if not dispatch_pool.is_enabled():
		frappe.throw(_("集中安排尚未启用；原有已派工任务可以继续执行。"), frappe.PermissionError)


def _manager(company=None):
	service.require_reviewer()
	if company:
		service._assert_supervisor_company(frappe.session.user, company)


def _block(code, message):
	return frappe._dict(code=code, message=message)


def _plan_for_card(job_card, *, for_update=False):
	name = frappe.db.get_value(PLAN, {"job_card": job_card}, "name", for_update=for_update)
	return frappe.get_doc(PLAN, name, for_update=for_update) if name else None


def _history(jc, *, for_update=False):
	if flt(jc.total_completed_qty) or flt(jc.process_loss_qty):
		return True
	return any(frappe.db.get_value(doctype, filters, "name", for_update=for_update) for doctype, filters in (
		("Job Card Worker Assignment", {"job_card": jc.name}),
		("Job Card Work Report", {"job_card": jc.name}),
		("Job Card Time Log", {"parent": jc.name, "parenttype": "Job Card"}),
		("Job Card Assignment Movement", {"job_card": jc.name}),
	))


def _scope_block(jc, *, for_update=False):
	if not jc or not all(jc.get(key) for key in ("work_order", "company", "operation", "operation_id")):
		return _block("TASK_CHANGED", _("任务不存在或缺少工单、公司、工序信息。"))
	order = frappe.db.get_value("Work Order", jc.work_order,
		["company", "docstatus", "status", "production_plan"], as_dict=True, for_update=for_update)
	if (not order or order.docstatus != 1 or order.company != jc.company
		or order.status in dispatch_pool.INACTIVE_STATUSES or not order.production_plan):
		return _block("WORK_ORDER_UNAVAILABLE", _("仅支持已纳入生产计划且可执行的正式工单。"))
	production_plan = frappe.db.get_value("Production Plan", order.production_plan,
		["company", "docstatus", "status"], as_dict=True, for_update=for_update)
	if (not production_plan or production_plan.company != jc.company or production_plan.docstatus != 1
		or production_plan.status in {"Closed", "Cancelled"}):
		return _block("PRODUCTION_PLAN_UNAVAILABLE", _("生产计划已关闭或失效，请联系主管处理。"))
	block = service.job_card_block(jc, for_update=for_update)
	if block and block.code != "PREVIOUS_OPERATION_PENDING":
		return block
	if not isfinite(flt(jc.for_quantity)) or flt(jc.for_quantity) <= 0:
		return _block("INVALID_QUANTITY", _("任务数量必须大于零。"))
	return None


def _plan_block(jc, *, for_update=False):
	block = _scope_block(jc, for_update=for_update)
	if block:
		return block
	if _history(jc, for_update=for_update):
		return _block("EXISTING_HISTORY", _("已有正式派工或生产历史，请通过原任务入口处理。"))
	# Planning does not freeze wage configuration. The formal service locks and
	# validates it after its Employee/Assignment locks when activation occurs.
	if not service.get_wage_rate(jc.company, jc.operation, nowdate()):
		return _block("RATE_MISSING", _("请先为本工序配置有效工资规则。"))
	return None


def _assert_revision(doc, expected_revision):
	try:
		value = float(expected_revision)
	except (TypeError, ValueError):
		value = -1
	if not isfinite(value) or value != int(value) or int(value) != cint(doc.revision if doc else 0):
		frappe.throw(_("安排已被其他人修改，请刷新后重新确认。"))


def _assert_snapshot(plan, jc):
	if plan.job_card != jc.name:
		frappe.throw(_("任务已变化，请主管重新确认安排。"))
	if any(plan.get(key) != jc.get(key) for key in ("work_order", "company", "operation", "operation_id")):
		frappe.throw(_("任务所属工单或工序已变化，请主管重新确认安排。"))
	precision = service.job_card_qty_precision()
	if flt(plan.job_card_qty, precision) != flt(jc.for_quantity, precision):
		frappe.throw(_("任务数量已变化，请主管重新确认安排。"))


def _assert_supervisor(supervisor, company, *, for_update=False):
	# This check is also used as the worker, so it must not apply the manager's
	# "may only select myself" UI rule. Authority is the persisted reviewer plan.
	if supervisor != "Administrator" and not frappe.db.get_value("User", supervisor, "enabled", for_update=for_update):
		frappe.throw(_("原安排的审核主管已停用，请主管重新安排。"))
	if not service.user_roles(supervisor, for_update=for_update).intersection(service.REVIEW_ROLES):
		frappe.throw(_("原安排的审核主管已无生产审核权限，请重新安排。"), frappe.PermissionError)
	service._assert_supervisor_company(supervisor, company)


def _validate_workers(rows, company, supervisor, *, for_update=False, check_snapshot=False):
	for row in sorted(rows, key=lambda value: value.get("employee") or ""):
		employee = frappe.db.get_value("Employee", row.get("employee"),
			["company", "status", "user_id"], as_dict=True, for_update=for_update)
		if not employee or employee.company != company or employee.status != "Active":
			frappe.throw(_("工人 {0} 必须是在本公司任职的有效员工。").format(row.get("employee")))
		user = service.employee_user(row.get("employee"), for_update=for_update)
		if user == supervisor:
			frappe.throw(_("工人不能审核自己的报工。"))
		if check_snapshot and row.get("employee_user") != user:
			frappe.throw(_("工人账号关联已变化，请主管重新确认安排。"))
		row.employee_user = user


def _normalize_allocations(allocations, qty):
	allocations = frappe.parse_json(allocations) if isinstance(allocations, str) else allocations
	if not isinstance(allocations, list) or not 1 <= len(allocations) <= 50:
		frappe.throw(_("每个工序需安排 1 至 50 名工人。"))
	for row in allocations:
		try:
			value = float(row.get("assigned_qty")) if isinstance(row, dict) else float("nan")
		except (TypeError, ValueError):
			value = float("nan")
		if not isfinite(value) or value <= 0:
			frappe.throw(_("每位工人的安排数量必须是大于零的有效数字。"))
	return service._normalize_assignment_plan(allocations, qty, service.job_card_qty_precision())


def _serialize(plan):
	if not plan:
		return None
	return {key: plan.get(key) for key in (
		"name", "job_card", "work_order", "company", "operation", "operation_id", "job_card_qty",
		"revision", "status", "supervisor", "arranged_by", "arranged_at", "activated_by", "activated_at",
	)} | {"allocations": [{"employee": row.employee, "employee_user": row.employee_user,
		"employee_name": frappe.db.get_value("Employee", row.employee, "employee_name") or row.employee,
		"assigned_qty": flt(row.assigned_qty), "notes": row.notes or "", "assignment": row.assignment}
		for row in plan.workers]}


def _save_doc(doc):
	doc.flags.prearrangement_action = True
	return doc.insert(ignore_permissions=True) if doc.is_new() else doc.save(ignore_permissions=True)


def _queue_refresh(doc, previous_users=()):
	from process_simplification.page_refresh import queue_changes

	users = set(previous_users)
	users.update(row.employee_user for row in (doc.get("workers") or []) if row.employee_user)
	queue_changes({("company", doc.company, "production"), ("any-company", "production")}
		| {("user", user, "tasks") for user in users})


def _manager_plan_scope(plan):
	_manager(plan.company)
	if not service.is_admin_reviewer() and plan.supervisor != frappe.session.user:
		frappe.throw(_("该安排由其他生产主管负责。"), frappe.PermissionError)


def save_plan(job_card, allocations, expected_qty, expected_revision=0, supervisor=None,
	*, expected_work_order=None, expected_operation_id=None):
	_enabled()
	_manager()
	jc = service.job_card_values(job_card, for_update=True)
	if not jc:
		frappe.throw(_("生产任务不存在。"))
	_manager(jc.company)
	block = _plan_block(jc, for_update=True)
	if block:
		frappe.throw(block.message)
	try:
		quantity = float(expected_qty)
	except (TypeError, ValueError):
		quantity = float("nan")
	if not isfinite(quantity) or flt(quantity, service.job_card_qty_precision()) != flt(jc.for_quantity, service.job_card_qty_precision()):
		frappe.throw(_("任务数量已变化，请刷新后重新确认。"))
	if (expected_work_order is not None and expected_work_order != jc.work_order
		or expected_operation_id is not None and expected_operation_id != jc.operation_id):
		frappe.throw(_("任务所属工单或工序已变化，请刷新后重新确认。"))
	plan = _plan_for_card(job_card, for_update=True)
	_assert_revision(plan, expected_revision)
	if plan:
		_manager_plan_scope(plan)
		if plan.status == "Activated":
			frappe.throw(_("任务已经开工，不能覆盖原安排。"))
	supervisor = supervisor or frappe.session.user
	service._assert_reviewer_scope(supervisor)
	service._assert_supervisor_company(supervisor, jc.company)
	rows = _normalize_allocations(allocations, jc.for_quantity)
	_validate_workers(rows, jc.company, supervisor, for_update=True)
	service._assert_reviewer_scope(supervisor, for_update=True)
	if not plan:
		plan = frappe.new_doc(PLAN)
	previous_users = [row.employee_user for row in (plan.get("workers") or [])]
	plan.update({"job_card": jc.name, "work_order": jc.work_order, "company": jc.company,
		"operation": jc.operation, "operation_id": jc.operation_id, "job_card_qty": jc.for_quantity,
		"status": "Planned", "supervisor": supervisor, "revision": (0 if plan.is_new() else cint(plan.revision)) + 1,
		"arranged_by": frappe.session.user, "arranged_at": now_datetime(), "workers": rows})
	_save_doc(plan)
	_queue_refresh(plan, previous_users)
	return _serialize(plan)


def cancel_plan(job_card, expected_revision):
	_manager()
	jc = service.job_card_values(job_card, for_update=True)
	if jc:
		frappe.db.get_value("Work Order", jc.work_order, "name", for_update=True)
	plan = _plan_for_card(job_card, for_update=True)
	if not plan:
		frappe.throw(_("安排不存在，请刷新后重试。"))
	_manager_plan_scope(plan)
	_assert_revision(plan, expected_revision)
	if plan.status == "Activated":
		frappe.throw(_("任务已经开工，请通过原派工入口处理。"))
	if plan.status != "Cancelled":
		plan.status = "Cancelled"
		plan.revision = cint(plan.revision) + 1
		plan.cancelled_by = frappe.session.user
		plan.cancelled_at = now_datetime()
		_save_doc(plan)
		_queue_refresh(plan)
	return _serialize(plan)


def _read_start_block(plan, jc):
	if not dispatch_pool.is_enabled():
		return _block("FEATURE_DISABLED", _("集中安排已停用，请联系主管。"))
	try:
		block = _plan_block(jc)
		if block:
			return block
		_assert_snapshot(plan, jc)
		_assert_supervisor(plan.supervisor, plan.company)
		_validate_workers(plan.workers, plan.company, plan.supervisor, check_snapshot=True)
		block = service.job_card_block(jc)
		if block:
			return block
		from process_simplification.production_workflow.service import get_work_order_dispatch_state
		# Native direct-consumption readiness reads the production pool. Workers
		# cannot read that pool, so evaluate this already-authorized plan under
		# its persisted supervisor, just as activation does. Only the resulting
		# gate is returned, and the worker's session is restored on every path.
		with _as_persisted_supervisor(plan.supervisor):
			state = get_work_order_dispatch_state(jc.work_order)
		if not state.can_dispatch:
			return _block(state.block_code, state.block_message)
	except (frappe.ValidationError, frappe.PermissionError) as exc:
		return _block("PLAN_NEEDS_REVIEW", str(exc))
	return None


def get_plan_context(job_cards):
	_manager()
	job_cards = frappe.parse_json(job_cards) if isinstance(job_cards, str) else job_cards
	if not isinstance(job_cards, list) or len(job_cards) > MAX_CARDS or any(not isinstance(value, str) for value in job_cards):
		frappe.throw(_("一次最多查看 20 个工序安排。"))
	result = {}
	for name in dict.fromkeys(job_cards):
		jc = service.job_card_values(name)
		if not jc:
			continue
		_manager(jc.company)
		plan = _plan_for_card(name)
		block = _plan_block(jc)
		if plan and not service.is_admin_reviewer() and plan.supervisor != frappe.session.user:
			result[name] = {"can_plan": False, "block_code": "OTHER_SUPERVISOR",
				"block_message": _("该安排由其他生产主管负责。"), "plan": None, "can_start": False,
				"default_roster": None}
			continue
		if not dispatch_pool.is_enabled():
			block = _block("FEATURE_DISABLED", _("集中安排尚未启用。"))
		start_block = _read_start_block(plan, jc) if plan and plan.status == "Planned" else block
		result[name] = {"can_plan": not block, "block_code": block.code if block else None,
			"block_message": block.message if block else None, "plan": _serialize(plan),
			"can_start": bool(plan and plan.status == "Planned" and not start_block),
			"start_block_code": start_block.code if start_block else None,
			"start_block_message": start_block.message if start_block else None,
			"default_roster": get_worker_defaults(jc.company, jc.operation,
				plan.supervisor if plan else None)}
	return {"cards": result}


def get_my_planned_tasks():
	service.require_worker()
	employee = service.employee_for_user()
	active = frappe.db.get_value("Job Card Work Report", {"employee": employee, "status": "In Progress"}, "name")
	rows = frappe.db.sql("""select p.name from `tabJob Card Prearrangement` p
		inner join `tabJob Card Prearrangement Worker` w on w.parent = p.name
		and w.parenttype = 'Job Card Prearrangement' and w.parentfield = 'workers'
		where p.status = 'Planned' and w.employee = %s and w.employee_user = %s
		order by p.work_order, p.operation, p.name""", (employee, frappe.session.user), as_dict=True)
	tasks = []
	for row in rows:
		plan = frappe.get_doc(PLAN, row.name)
		allocation = next(value for value in plan.workers if value.employee == employee and value.employee_user == frappe.session.user)
		jc = service.job_card_values(plan.job_card)
		block = _read_start_block(plan, jc)
		if not block and active:
			block = _block("ACTIVE_WORK_SESSION", _("请先结束当前正在进行的任务，再开始这项工作。"))
		item = frappe.db.get_value("Job Card", plan.job_card, "production_item")
		tasks.append({"name": plan.name, "revision": plan.revision, "job_card": plan.job_card,
			"work_order": plan.work_order, "company": plan.company, "operation": plan.operation,
			"production_item": item, "item_name": frappe.db.get_value("Item", item, "item_name") if item else None,
			"planned_qty": flt(allocation.assigned_qty), "for_quantity": flt(plan.job_card_qty),
			"supervisor": plan.supervisor, "supervisor_name": frappe.db.get_value("User", plan.supervisor, "full_name"),
			"notes": allocation.notes, "can_start": not block,
			"block_code": block.code if block else None, "block_message": block.message if block else None})
	return {"tasks": tasks}


@contextmanager
def _as_persisted_supervisor(supervisor):
	worker_user = frappe.session.user
	session_sid = frappe.session.sid
	session_data = frappe.session.data
	form_dict = frappe.local.form_dict
	try:
		frappe.set_user(supervisor)
		yield
	finally:
		frappe.set_user(worker_user)
		# set_user is designed for whole-request login and clears these fields.
		# A narrow delegation must preserve the worker's real browser session and
		# RPC arguments for transaction hooks, CSRF/session handling and retries.
		frappe.session.sid = session_sid
		frappe.session.data = session_data
		frappe.local.form_dict = form_dict


def start_planned_task(plan, request_id, expected_revision):
	"""Activate and start in the caller's transaction; errors must propagate.

	The RPC boundary rolls back assignment/reservation/plan/session together. No
	commit, catch-and-continue or background activation is permitted here.
	"""
	service.require_worker()
	request_id = service._require_request_id(request_id, _("开工"))
	worker_user = frappe.session.user
	employee = service.employee_for_user()
	card_name = frappe.db.get_value(PLAN, plan, "job_card")
	if not card_name:
		frappe.throw(_("生产安排不存在。"))
	jc = service.job_card_values(card_name, for_update=True)
	if not jc:
		frappe.throw(_("生产任务已不存在，请联系主管。"))
	frappe.db.get_value("Work Order", jc.work_order, "name", for_update=True)
	doc = frappe.get_doc(PLAN, plan, for_update=True)
	allocation = next((row for row in doc.workers if row.employee == employee and row.employee_user == worker_user), None)
	if not allocation:
		frappe.throw(_("只能开始分配给自己的生产安排。"), frappe.PermissionError)
	_assert_revision(doc, expected_revision)
	if doc.status == "Activated":
		# A successful request may be retried after the card was completed. The
		# existing service verifies request identity before normal draft checks.
		if not allocation.assignment:
			frappe.throw(_("正式派工关联缺失，请联系主管。"))
		report = service.start_work_session(allocation.assignment, request_id)
		return {"status": "started", "plan": doc.name, "assignment": allocation.assignment, "report": report}
	_enabled()
	if doc.status != "Planned":
		frappe.throw(_("该安排已取消，请刷新任务列表。"))
	_assert_snapshot(doc, jc)
	block = _plan_block(jc, for_update=True)
	if block:
		frappe.throw(block.message)
	# Do not lock Employee/User ahead of the formal service's stock locks. A
	# planning preview validates identity here; repeat the frozen-user comparison
	# after assign_workers has acquired its established WO/stock -> Employee ->
	# Assignment locks. Any drift then aborts this entire request transaction.
	_validate_workers(doc.workers, doc.company, doc.supervisor, check_snapshot=True)
	_assert_supervisor(doc.supervisor, doc.company)
	# The supervisor explicitly saved this exact roster and quantity. The worker
	# can execute that authority, never choose a reviewer, employee or quantity.
	with _as_persisted_supervisor(doc.supervisor):
		assignments = service.assign_workers(doc.job_card,
			[{"employee": row.employee, "assigned_qty": row.assigned_qty, "notes": row.notes} for row in doc.workers],
			doc.supervisor, first_only=True)
	_validate_workers(doc.workers, doc.company, doc.supervisor, for_update=True, check_snapshot=True)
	_assert_supervisor(doc.supervisor, doc.company, for_update=True)
	assignment_by_employee = {row.employee: row.name for row in assignments}
	for row in doc.workers:
		row.assignment = assignment_by_employee[row.employee]
	doc.status = "Activated"
	doc.activated_by = worker_user
	doc.activated_at = now_datetime()
	# Revision identifies the manager's plan and does not change on activation;
	# retries carrying the same preview revision must remain valid.
	_save_doc(doc)
	report = service.start_work_session(assignment_by_employee[employee], request_id)
	_queue_refresh(doc)
	return {"status": "started", "plan": doc.name, "assignment": assignment_by_employee[employee], "report": report}


def _default_key(company, operation, supervisor):
	return service._hash_key(company, operation, supervisor)


def _serialize_default(doc):
	if not doc:
		return None
	workers = frappe.parse_json(doc.workers_json or "[]")
	return {"name": doc.name, "revision": doc.revision, "company": doc.company,
		"operation": doc.operation, "supervisor": doc.supervisor,
		"workers": [{"employee": employee,
			"employee_name": frappe.db.get_value("Employee", employee, "employee_name") or employee} for employee in workers]}


def get_worker_defaults(company, operation, supervisor=None):
	_manager(company)
	supervisor = supervisor or frappe.session.user
	if not service.is_admin_reviewer() and supervisor != frappe.session.user:
		frappe.throw(_("只能查看自己负责的工序默认人员。"), frappe.PermissionError)
	name = frappe.db.get_value(DEFAULT, {"default_key": _default_key(company, operation, supervisor)}, "name")
	return _serialize_default(frappe.get_doc(DEFAULT, name)) if name else None


def save_worker_defaults(company, operation, workers, expected_revision=0, supervisor=None):
	_enabled()
	_manager(company)
	supervisor = supervisor or frappe.session.user
	service._assert_reviewer_scope(supervisor)
	service._assert_supervisor_company(supervisor, company)
	if not frappe.db.get_value("Operation", operation, "name", for_update=True):
		frappe.throw(_("工序不存在。"))
	workers = frappe.parse_json(workers) if isinstance(workers, str) else workers
	if not isinstance(workers, list) or len(workers) > 50:
		frappe.throw(_("默认人员必须是最多 50 人的列表。"))
	employees = [str(row.get("employee") or "").strip() if isinstance(row, dict) else str(row or "").strip() for row in workers]
	if any(not row for row in employees) or len(set(employees)) != len(employees):
		frappe.throw(_("默认人员不能为空或重复。"))
	_validate_workers([frappe._dict(employee=row) for row in employees], company, supervisor, for_update=True)
	service._assert_reviewer_scope(supervisor, for_update=True)
	key = _default_key(company, operation, supervisor)
	name = frappe.db.get_value(DEFAULT, {"default_key": key}, "name", for_update=True)
	doc = frappe.get_doc(DEFAULT, name, for_update=True) if name else None
	_assert_revision(doc, expected_revision)
	if not doc:
		doc = frappe.new_doc(DEFAULT)
	doc.update({"default_key": key, "company": company, "operation": operation, "supervisor": supervisor,
		"workers_json": json.dumps(employees, ensure_ascii=False), "revision": (0 if doc.is_new() else cint(doc.revision)) + 1})
	_save_doc(doc)
	_queue_refresh(doc)
	return _serialize_default(doc)


def validate_document(doc):
	if not doc.flags.get("prearrangement_action"):
		frappe.throw(_("请通过集中安排入口修改人员安排。"), frappe.PermissionError)


def prohibit_delete(doc):
	frappe.throw(_("人员安排为审计记录，请取消或修改，不能删除。"))
