"""Read-only dispatch load; no stock recalculation, timers or wage data."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from process_simplification.production_reporting.assignment_movement import (
	assignment_effective_qty,
	movement_totals,
)
from process_simplification.production_reporting.domain import job_card_qty_precision


def summarize_worker_loads(employees, assignments, reports, movements, *, current_job_card=None):
	"""Count distinct unfinished jobs, keeping paused sessions in the active group.

	A pending report takes precedence over queued work (which cannot start until
	review). Remaining quantities exclude pending/approved reports and released
	allocations, matching the worker dashboard. Queued does not imply stock ready.
	"""
	precision = job_card_qty_precision()
	result = {
		row.name: frappe._dict(
			employee=row.name, employee_name=row.employee_name,
			active_count=0, queued_count=0, pending_review_count=0, jobs=[],
		)
		for row in employees
	}
	report_by_assignment = {row.assignment: row for row in reports}
	jobs = {}
	for assignment in assignments:
		if assignment.employee not in result:
			continue
		report = report_by_assignment.get(assignment.name) or frappe._dict()
		if assignment.requires_attention and not report.active_count:
			continue
		remaining = max(flt(
			assignment_effective_qty(assignment, totals=movements)
			- flt(report.approved_qty) - flt(report.pending_qty), precision,
		), 0)
		state = ("active" if report.active_count else "pending_review" if report.pending_count or flt(report.pending_qty) > 0
			else "queued" if remaining > 0 else None)
		if not state:
			continue
		key = (assignment.employee, assignment.job_card)
		job = frappe._dict(
			job_card=assignment.job_card, work_order=assignment.work_order,
			operation=assignment.operation, production_item=assignment.production_item,
			item_name=assignment.item_name, stock_uom=assignment.stock_uom,
			state=state, paused=bool(report.active_count and report.paused_count == report.active_count),
			remaining_qty=remaining, pending_qty=flt(report.pending_qty),
			requires_attention=bool(assignment.requires_attention),
			is_current=assignment.job_card == current_job_card,
		)
		previous = jobs.get(key)
		if previous:
			job.remaining_qty += previous.remaining_qty
			job.pending_qty += previous.pending_qty
			if _state_rank(previous.state) < _state_rank(job.state):
				job.state, job.paused = previous.state, previous.paused
		jobs[key] = job
	for (employee, _job_card), job in jobs.items():
		result[employee][f"{job.state}_count"] += 1
		result[employee].jobs.append(job)
	for load in result.values():
		load.jobs.sort(key=lambda job: (_state_rank(job.state), job.job_card))
	return result


def _state_rank(state):
	return {"active": 0, "pending_review": 1, "queued": 2}[state]


def read_worker_loads(company, employees, *, current_job_card=None):
	"""Batch all visible employees; query count does not grow per employee/job."""
	if not employees:
		return {}
	assignments = frappe.db.sql(
		"""
		select a.name, a.employee, a.job_card, a.work_order, a.operation, a.assigned_qty,
		       jc.production_item, item.item_name, item.stock_uom,
		       (jc.docstatus != 0 or wo.docstatus != 1 or a.status != 'Active'
		        or wo.status in ('Completed', 'Closed', 'Stopped', 'Cancelled')) requires_attention
		from `tabJob Card Worker Assignment` a
		inner join `tabJob Card` jc on jc.name = a.job_card
		inner join `tabWork Order` wo on wo.name = a.work_order
		left join `tabItem` item on item.name = jc.production_item
		where a.company = %(company)s and jc.company = %(company)s and wo.company = %(company)s
		  and a.employee in %(employees)s
		  and ((a.status = 'Active' and jc.docstatus = 0 and wo.docstatus = 1
		        and wo.status not in ('Completed', 'Closed', 'Stopped', 'Cancelled'))
		       or exists (select 1 from `tabJob Card Work Report` active
		                  where active.assignment = a.name and active.status = 'In Progress'))
		""",
		{"company": company, "employees": tuple(row.name for row in employees)}, as_dict=True,
	)
	names = [row.name for row in assignments]
	reports = frappe.db.sql(
		"""
		select assignment,
		       sum(case when status = 'Approved' then completed_qty else 0 end) approved_qty,
		       sum(case when status = 'Pending Approval' then completed_qty else 0 end) pending_qty,
		       sum(case when status = 'Pending Approval' then 1 else 0 end) pending_count,
		       sum(case when status = 'In Progress' then 1 else 0 end) active_count,
		       sum(case when status = 'In Progress' and timer_paused_at is not null then 1 else 0 end) paused_count
		from `tabJob Card Work Report`
		where assignment in %(assignments)s and status in ('Approved', 'Pending Approval', 'In Progress')
		group by assignment
		""", {"assignments": tuple(names)}, as_dict=True,
	) if names else []
	return summarize_worker_loads(
		employees, assignments, reports, movement_totals(names), current_job_card=current_job_card,
	)


def get_worker_loads(job_card, employees):
	from process_simplification.production_reporting.service import (
		_assert_supervisor_company, job_card_values, require_reviewer,
	)
	from process_simplification.management_access import WORKER_INCOMPATIBLE_ROLES

	require_reviewer()
	jc = job_card_values(job_card)
	if not jc:
		frappe.throw(_("Job Card does not exist."))
	_assert_supervisor_company(frappe.session.user, jc.company)
	if not isinstance(employees, list) or len(employees) > 50 or any(not isinstance(name, str) for name in employees):
		frappe.throw(_("Select at most 50 employees per request."))
	names = tuple(dict.fromkeys(name for name in employees if name))
	rows = frappe.db.sql(
		"""
		select e.name, e.employee_name from `tabEmployee` e
		inner join `tabUser` u on u.name = e.user_id and u.enabled = 1
		where e.company = %(company)s and e.status = 'Active' and e.name in %(employees)s
		and exists (select 1 from `tabHas Role` r where r.parent = e.user_id and r.role = 'Production Worker')
		and not exists (select 1 from `tabHas Role` r where r.parent = e.user_id and r.role in %(incompatible)s)
		""", {"company": jc.company, "employees": names, "incompatible": tuple(sorted(WORKER_INCOMPATIBLE_ROLES))},
		as_dict=True,
	) if names else []
	loads = read_worker_loads(jc.company, rows, current_job_card=job_card)
	return {"employees": list(loads.values()), "checked_at": now_datetime().replace(microsecond=0)}


def worker_load_label(load):
	return _("进行中 {0} · 待做 {1} · 待审核 {2}").format(
		load.active_count, load.queued_count, load.pending_review_count,
	)
