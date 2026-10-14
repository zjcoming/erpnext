"""Finalize one approved batch and carry only its unfinished assignments forward."""

from __future__ import annotations

from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from process_simplification.production_reporting import assignment_movement as movements
from process_simplification.production_reporting.domain import (
	approved_report_qty,
	assignment_reported_qty,
	job_card_block,
	job_card_qty_precision,
	job_card_values,
	require_reviewer,
)


_CONTEXT_FLAG = "worker_reporting_continuation"


@contextmanager
def _continuation_context(source, remaining):
	previous = getattr(frappe.flags, _CONTEXT_FLAG, None)
	context = frappe._dict(source=source.name, work_order=source.work_order,
		completed_qty=flt(source.total_completed_qty), pending_qty=remaining, target=None)
	setattr(frappe.flags, _CONTEXT_FLAG, context)
	try:
		yield context
	finally:
		if previous is None:
			frappe.flags.pop(_CONTEXT_FLAG, None)
		else:
			setattr(frappe.flags, _CONTEXT_FLAG, previous)


def is_partial_submission(doc) -> bool:
	context = getattr(frappe.flags, _CONTEXT_FLAG, None)
	precision = job_card_qty_precision()
	return bool(context and context.source == doc.name and context.work_order == doc.work_order
		and flt(context.completed_qty, precision) == flt(doc.total_completed_qty, precision)
		and flt(context.pending_qty, precision) == flt(doc.pending_qty, precision)
		and flt(doc.pending_qty, precision) > 0)


def is_continuation_assignment(job_card: str) -> bool:
	context = getattr(frappe.flags, _CONTEXT_FLAG, None)
	return bool(context and context.target == job_card)


def _assignment_movement_rows(assignment, *, for_update=True):
	return [row for row in movements._movement_rows(assignments=[assignment], for_update=for_update)
		if row.movement_type == movements.CONTINUATION]


def assert_assignment_not_continued(assignment: str):
	if _assignment_movement_rows(assignment):
		frappe.throw(_("该派工已有分批承接记录，不能删除或重做原分配。请保留生产和工资关联。"))


def assert_card_not_continuation_target(job_card: str):
	assignment = frappe.qb.DocType("Job Card Worker Assignment")
	query = frappe.qb.from_(assignment).select(assignment.name).where(assignment.job_card == job_card)
	for row in query.for_update().run(as_dict=True):
		if any(m.target_assignment == row.name for m in _assignment_movement_rows(row.name)):
			frappe.throw(_("该任务承接了已确认批次的剩余数量，不能取消或丢弃；否则会丢失未完成任务。"))


def validate_continuation_movement(doc):
	context = getattr(frappe.flags, _CONTEXT_FLAG, None)
	if doc.is_new() and not (context and context.source == doc.job_card):
		frappe.throw(_("剩余任务只能由主管的分批确认操作承接。"))
	fields = ["job_card", "work_order", "company", "operation", "operation_id", "employee", "supervisor", "assigned_qty", "status"]
	source = frappe.db.get_value("Job Card Worker Assignment", doc.source_assignment, fields, as_dict=True)
	target = frappe.db.get_value("Job Card Worker Assignment", doc.target_assignment, fields, as_dict=True)
	if not source or not target or any(source.get(key) != target.get(key) for key in
		("work_order", "company", "operation", "operation_id", "employee", "supervisor")):
		frappe.throw(_("分批承接必须保持同一工单、公司、工序、工人和主管。"))
	if source.job_card != doc.job_card or target.job_card == source.job_card:
		frappe.throw(_("分批承接必须关联原任务及其新的剩余任务。"))
	if doc.reference_doctype != "Job Card" or doc.reference_name != target.job_card:
		frappe.throw(_("分批承接的目标任务关联不一致。"))
	if doc.is_new() and (target.status != "Active" or context.target != target.job_card
		or frappe.db.get_value("Job Card", source.job_card, "docstatus") != 1
		or frappe.db.get_value("Job Card", target.job_card, "docstatus") != 0):
		frappe.throw(_("分批承接的原任务和剩余任务状态不正确。"))
	if flt(doc.qty, job_card_qty_precision()) != flt(target.assigned_qty, job_card_qty_precision()):
		frappe.throw(_("承接数量必须与新派工的原始分配数量一致。"))


def _result(source, rows, *, reused):
	targets = {row.reference_name for row in rows}
	remaining = flt(sum(flt(row.qty) for row in rows), job_card_qty_precision())
	if len(targets) != 1 or remaining != flt(source.pending_qty, job_card_qty_precision()):
		frappe.throw(_("分批承接记录不完整，请联系管理员核对；本次没有创建新任务。"))
	target = next(iter(targets))
	if not frappe.db.exists("Job Card", target):
		frappe.throw(_("已登记的剩余任务不存在，请联系管理员核对。"))
	return {"source_job_card": source.name, "job_card": target,
		"completed_qty": flt(source.total_completed_qty), "remaining_qty": remaining,
		"assignments": [row.target_assignment for row in rows],
		"request_id": rows[0].request_key, "reused": reused}


def partial_completion_state(job_card, assignments, reports, work_order):
	"""Read-only button eligibility; mutation repeats every check under locks."""
	state = frappe._dict(can_finalize_partial=False, partial_completion_block_message=None)
	if not job_card or cint(job_card.docstatus) != 0 or not (
		0 < flt(job_card.total_completed_qty) < flt(job_card.for_quantity)
	):
		return state
	from process_simplification.production_reporting.domain import is_admin_reviewer

	message = None
	block = job_card_block(job_card)
	if block and block.code != "PREVIOUS_OPERATION_PENDING":
		message = block.message
	elif not assignments or any(row.status != "Active" for row in assignments):
		message = _("请先核对本任务的有效派工。")
	elif len({row.supervisor for row in assignments}) != 1 or (
		not is_admin_reviewer() and any(row.supervisor != frappe.session.user for row in assignments)
	):
		message = _("请由原派工主管确认本批完工。")
	elif any(row.status in {"In Progress", "Pending Approval"} for row in reports):
		message = _("请先结束或取消正在进行及暂停的作业，并审核所有待审报工。")
	elif work_order.get("transfer_material_against") == "Job Card" or flt(job_card.process_loss_qty):
		message = _("当前分批确认支持按工单领料且无工序损耗的任务。")
	elif frappe.db.exists("Production Exception Request", {"job_card": job_card.name,
		"status": ["not in", ["Rejected", "Withdrawn"]]}):
		message = _("本任务含退料、报废或损耗记录，暂不能分批确认；请先核对生产异常。")
	elif any(row.movement_type != movements.CONTINUATION
		for row in movements._movement_rows(job_card=job_card.name)):
		message = _("本任务含退料释放或二次派工历史，暂不能分批确认。")
	elif frappe.db.exists("Job Card", {"work_order": job_card.work_order,
		"operation_id": job_card.operation_id, "docstatus": ["<", 2],
		"custom_worker_reporting_enabled": 0, "name": ["!=", job_card.name]}):
		message = _("同工序存在标准报工任务，请先核对后再使用分批确认。")
	state.partial_completion_block_message = message
	state.can_finalize_partial = not bool(message)
	return state


@frappe.whitelist(methods=["POST"])
def finalize_partial_job_card(job_card: str, request_id: str, expected_completed_qty=None, expected_remaining_qty=None):
	"""Keep approved facts on the original card; submit and carry its balance atomically."""
	from erpnext.manufacturing.doctype.work_order.work_order import create_job_card
	from process_simplification.api.production_plan_adapter import _allow_generated_job_cards_for
	from process_simplification.production_reporting import service

	require_reviewer()
	request_id = str(request_id or "").strip()
	if not request_id or len(request_id) > 140:
		frappe.throw(_("分批确认需要有效的请求编号。"))
	jc = job_card_values(job_card, for_update=True)
	if not jc:
		frappe.throw(_("Job Card does not exist."))
	frappe.db.get_value("Work Order", jc.work_order, "name", for_update=True)
	service._assert_supervisor_company(frappe.session.user, jc.company)
	source = frappe.get_doc("Job Card", jc.name, for_update=True)
	source.check_permission("read")
	assignment = frappe.qb.DocType("Job Card Worker Assignment")
	assignments = (frappe.qb.from_(assignment).select(assignment.star)
		.where(assignment.job_card == source.name).orderby(assignment.name).for_update()).run(as_dict=True)
	if not assignments:
		frappe.throw(_("请先派工并审核本批报工，再确认分批完工。"))
	supervisors = {row.supervisor for row in assignments}
	if len(supervisors) != 1:
		frappe.throw(_("分批确认需要原派工使用同一主管。"))
	supervisor = next(iter(supervisors))
	service._assert_reviewer_scope(supervisor, for_update=True)
	require_reviewer(for_update=True)
	rows = movements._movement_rows(job_card=source.name, for_update=True)
	continued = [row for row in rows if row.movement_type == movements.CONTINUATION]
	if continued:
		return _result(source, continued, reused=True)
	if movements.redispatch_request_rows(request_id, for_update=True):
		frappe.throw(_("该请求编号已用于其他派工操作，请刷新后重试。"))
	work_order = frappe.get_doc("Work Order", source.work_order)
	if work_order.docstatus != 1 or work_order.status in {"Stopped", "Closed", "Completed", "Cancelled"}:
		frappe.throw(_("当前工单已经停止、关闭或完成，不能确认新批次。"))
	block = job_card_block(jc, for_update=True)
	if block and block.code != "PREVIOUS_OPERATION_PENDING":
		frappe.throw(block.message)
	if work_order.transfer_material_against == "Job Card" or flt(source.process_loss_qty):
		frappe.throw(_("当前分批确认支持按工单领料且无工序损耗的任务。"))
	precision = job_card_qty_precision()
	completed = flt(approved_report_qty(source.name, for_update=True), precision)
	remaining = flt(flt(source.for_quantity) - completed, precision)
	if completed <= 0 or remaining <= 0 or completed != flt(source.total_completed_qty, precision):
		frappe.throw(_("分批确认需要已有审核通过数量，并且仍有未完成数量。"))
	for expected, actual in ((expected_completed_qty, completed), (expected_remaining_qty, remaining)):
		if expected not in (None, "") and flt(expected, precision) != actual:
			frappe.throw(_("报工数量已经变化，请刷新后重新确认本批数量。"))
	if frappe.db.get_value("Job Card Work Report", {"job_card": source.name,
		"status": ["in", ["In Progress", "Pending Approval"]]}, "name", for_update=True):
		frappe.throw(_("请先结束或取消正在进行及暂停的作业，并审核所有待审报工。"))
	if frappe.db.get_value("Production Exception Request", {"job_card": source.name,
		"status": ["not in", ["Rejected", "Withdrawn"]]}, "name", for_update=True):
		frappe.throw(_("含有退料、报废或损耗记录的任务暂不支持分批确认，请先核对生产异常。"))
	if any(row.movement_type != movements.CONTINUATION for row in rows):
		frappe.throw(_("含有退料释放或二次派工历史的任务暂不支持分批确认。"))
	# A native card without managed reports cannot be included in the shared
	# report ledger used to enforce issued-material and predecessor budgets.
	other_cards = frappe.get_all("Job Card", filters={"work_order": source.work_order,
		"operation_id": source.operation_id, "docstatus": ["<", 2], "name": ["!=", source.name]},
		fields=["name", "custom_worker_reporting_enabled"], limit=0)
	if any(not cint(row.custom_worker_reporting_enabled) for row in other_cards):
		frappe.throw(_("同工序存在标准报工任务，请先核对后再使用分批确认。"))
	plan = []
	for row in assignments:
		if row.status != "Active":
			frappe.throw(_("原派工状态已经变化，请刷新后核对。"))
		qty = flt(movements.assignment_effective_qty(row, for_update=True)
			- assignment_reported_qty(row.name, for_update=True), precision)
		if qty < 0:
			frappe.throw(_("原派工的已报数量超过有效分配，不能继续承接。"))
		if qty:
			plan.append(frappe._dict(employee=row.employee, assigned_qty=qty, source_assignment=row.name,
				notes=_("承接任务 {0} 的未完成数量。").format(source.name)))
	if flt(sum(row.assigned_qty for row in plan), precision) != remaining:
		frappe.throw(_("原派工剩余数量与任务待完成数量不一致，不能继续承接。"))
	with _continuation_context(source, remaining) as context:
		with service._allow_work_order_update_for_approval(source.work_order):
			source.flags.ignore_permissions = True
			source.submit()
		work_order.reload()
		operation = next((row for row in work_order.operations if row.name == source.operation_id), None)
		if not operation:
			frappe.throw(_("原工序已不存在，分批确认已回滚。"))
		operation = frappe._dict(operation.as_dict())
		operation.job_card_qty = remaining
		with _allow_generated_job_cards_for(source.work_order):
			target = create_job_card(work_order, operation, auto_create=True)
		context.target = target.name
		targets = service.assign_workers(target.name, plan, supervisor)
		by_employee = {doc.employee: doc for doc in targets}
		for row in plan:
			target_assignment = by_employee[row.employee]
			movement = frappe.get_doc(dict(doctype="Job Card Assignment Movement",
				movement_type=movements.CONTINUATION,
				movement_key=movements._hash_key(movements.CONTINUATION, source.name, row.source_assignment),
				job_card=source.name, work_order=source.work_order, company=source.company,
				operation=source.operation, operation_id=source.operation_id,
				source_assignment=row.source_assignment, target_assignment=target_assignment.name,
				qty=row.assigned_qty, reason=_("本批完工确认后，剩余数量由原工人继续。"),
				reference_doctype="Job Card", reference_name=target.name, request_key=request_id,
				moved_by=frappe.session.user, moved_at=now_datetime()))
			movement.flags.assignment_movement_action = True
			movement.insert(ignore_permissions=True)
	return _result(source, [row for row in movements._movement_rows(job_card=source.name, for_update=True)
		if row.movement_type == movements.CONTINUATION], reused=False)
