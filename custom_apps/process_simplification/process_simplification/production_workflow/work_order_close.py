"""Authorize permanent closure without granting generic reservation cancellation."""

from __future__ import annotations

import frappe
from frappe import _

from erpnext.manufacturing.doctype.work_order.work_order import (
	close_work_order as native_close_work_order,
)

from process_simplification.management_access import (
	OWNER_ROLE,
	PRODUCTION_MANAGER_ROLE,
	user_company_scope,
)
from process_simplification.production_reporting.work_order import (
	_allow_unstarted_continuation_close,
	assert_no_managed_draft_job_cards,
	assert_work_order_close_allowed,
	cancel_unstarted_continuation_assignments,
	managed_draft_job_cards,
)
from process_simplification.production_workflow.stock_reservation import (
	_allow_work_order_close_reservations,
)


@frappe.whitelist(methods=["POST"])
def close_work_order(work_order: str, status: str):
	# ERPNext #59284 adds POST and record-level authorization to this native RPC.
	# Keep that boundary on our pinned version, then scope only the derived cancel.
	if status != "Closed":
		frappe.throw(_("此操作仅用于永久关闭工单；暂停或恢复请使用原工单操作。"))
	if not isinstance(work_order, str) or not work_order.strip():
		frappe.throw(_("请选择要关闭的工单。"))

	doc = frappe.get_doc("Work Order", work_order)
	doc.check_permission("write")
	app_manager = bool(set(frappe.get_roles()).intersection({OWNER_ROLE, PRODUCTION_MANAGER_ROLE}))
	if app_manager:
		_check_company(doc)

	# Preserve the existing reporting lock order (Job Card -> Work Order).
	# The native method still runs both its own guard and our Work Order mixin.
	managed_draft_job_cards(work_order, for_update=True)
	doc = frappe.get_doc("Work Order", work_order, for_update=True)
	doc.check_permission("write")
	if app_manager:
		_check_company(doc)
	if doc.docstatus != 1:
		frappe.throw(_("只能永久关闭已提交的工单。"))
	if doc.status == "Closed":
		return doc.status

	if not app_manager:
		assert_no_managed_draft_job_cards(work_order)
		return native_close_work_order(work_order, status)
	with _allow_unstarted_continuation_close(doc.name, doc.company):
		assignments = assert_work_order_close_allowed(doc.name)
		with _allow_work_order_close_reservations(doc.name, doc.company):
			result = native_close_work_order(work_order, status)
		cancel_unstarted_continuation_assignments(doc.name, assignments)
		return result


def _check_company(doc):
	companies = user_company_scope()
	if not doc.company or (companies is not None and doc.company not in companies):
		frappe.throw(_("没有该工单所属公司的关闭权限。"), frappe.PermissionError)
