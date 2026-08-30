from __future__ import annotations

from functools import wraps

import frappe
from frappe import _
from frappe.utils import escape_html, flt

from process_simplification.management_access import OWNER_ROLE, WAREHOUSE_OPERATOR_ROLE
from process_simplification.production_exceptions.constants import (
	AWAITING_STOCK_ENTRY,
	MATERIAL_RETURN,
	MATERIAL_SCRAP,
	PROCESS_LOSS,
)


WAREHOUSE_RESPONSIBILITY = "库存处理"
PROCUREMENT_RESPONSIBILITY = "缺料采购"
RESPONSIBILITIES = {WAREHOUSE_RESPONSIBILITY, PROCUREMENT_RESPONSIBILITY}
RESPONSIBILITY_ROLES = {
	WAREHOUSE_RESPONSIBILITY: {WAREHOUSE_OPERATOR_ROLE, OWNER_ROLE},
	PROCUREMENT_RESPONSIBILITY: {WAREHOUSE_OPERATOR_ROLE, OWNER_ROLE},
}
RESPONSIBILITY_ROLE_PRIORITY = {
	WAREHOUSE_RESPONSIBILITY: (WAREHOUSE_OPERATOR_ROLE, OWNER_ROLE),
	PROCUREMENT_RESPONSIBILITY: (WAREHOUSE_OPERATOR_ROLE, OWNER_ROLE),
}

APP_NAME = "process_simplification"
ASSIGNMENT_ROUTE = "/app/active-production-work"
REPORT_REVIEW_ROUTE = "/app/production-report-review"
REPORT_HISTORY_ROUTE = "/app/production-report-history"
EXCEPTION_REVIEW_ROUTE = "/app/production-exception-review"
SHORTAGE_ROUTE = "/app/shortage-purchase-planning"

EXCEPTION_TYPE_LABELS = {
	MATERIAL_RETURN: "余料退库",
	MATERIAL_SCRAP: "物料报废",
	PROCESS_LOSS: "工序损耗",
}


def _notification_event(function):
	"""Keep an auxiliary notification failure from rolling back a business action."""

	@wraps(function)
	def wrapped(*args, **kwargs):
		try:
			return function(*args, **kwargs)
		except Exception:
			if frappe.in_test:
				raise
			try:
				frappe.log_error(
					title="Process Simplification notification failed",
					message=frappe.get_traceback(),
				)
			except Exception:
				pass
			return []

	return wrapped


def _enabled_system_user(user: str | None) -> bool:
	if not user or user in {"Administrator", "Guest"}:
		return False
	row = frappe.db.get_value("User", user, ["enabled", "user_type"], as_dict=True)
	return bool(row and row.enabled and row.user_type == "System User")


def _user_matches_company(user: str, company: str) -> bool:
	"""Apply explicit global Company permissions, then Employee company scope."""
	company_permissions = frappe.get_all(
		"User Permission",
		filters={
			"user": user,
			"allow": "Company",
			"apply_to_all_doctypes": 1,
		},
		pluck="for_value",
	)
	if company_permissions:
		return company in set(company_permissions)

	employee_companies = frappe.get_all(
		"Employee",
		filters={"user_id": user, "status": "Active"},
		pluck="company",
	)
	return not employee_companies or company in set(employee_companies)


def validate_notification_routes(settings) -> None:
	seen = set()
	for row in settings.get("notification_recipients") or []:
		if row.responsibility not in RESPONSIBILITIES:
			frappe.throw(_("Select a valid notification responsibility."))
		key = (row.company, row.responsibility, row.user)
		if key in seen:
			frappe.throw(_("The same notification recipient is configured more than once."))
		seen.add(key)
		if not _enabled_system_user(row.user):
			frappe.throw(_("Notification recipient {0} must be an enabled System User.").format(row.user))
		allowed_roles = RESPONSIBILITY_ROLES[row.responsibility]
		if not allowed_roles.intersection(frappe.get_roles(row.user)):
			frappe.throw(
				_("Notification recipient {0} does not hold a role allowed for {1}.").format(
					row.user,
					row.responsibility,
				)
			)
		if not _user_matches_company(row.user, row.company):
			frappe.throw(
				_("Notification recipient {0} is not within company {1}.").format(
					row.user,
					row.company,
				)
			)


def _configured_recipients(company: str, responsibility: str) -> list[str]:
	if not frappe.db.table_exists("Process Notification Recipient"):
		return []
	users = frappe.get_all(
		"Process Notification Recipient",
		filters={
			"parent": "Process Simplification Settings",
			"parenttype": "Process Simplification Settings",
			"parentfield": "notification_recipients",
			"company": company,
			"responsibility": responsibility,
		},
		pluck="user",
	)
	allowed_roles = RESPONSIBILITY_ROLES[responsibility]
	return sorted(
		{
			user
			for user in users
			if _enabled_system_user(user)
			and allowed_roles.intersection(frappe.get_roles(user))
			and _user_matches_company(user, company)
		}
	)


def responsibility_recipients(company: str, responsibility: str) -> list[str]:
	if not company or responsibility not in RESPONSIBILITIES:
		return []
	configured = _configured_recipients(company, responsibility)
	if configured:
		return configured

	candidates = frappe.get_all(
		"User",
		filters={"enabled": 1, "user_type": "System User"},
		pluck="name",
	)
	roles_by_user = {
		user: set(frappe.get_roles(user))
		for user in candidates
		if user not in {"Administrator", "Guest"}
	}
	for role in RESPONSIBILITY_ROLE_PRIORITY[responsibility]:
		matching = sorted(
			user
			for user, roles in roles_by_user.items()
			if role in roles and _user_matches_company(user, company)
		)
		if matching:
			return matching
	return []


def _recipient_emails(users: list[str] | tuple[str, ...] | set[str]) -> list[str]:
	user_names = sorted({str(user or "").strip() for user in users if str(user or "").strip()})
	if not user_names:
		return []
	rows = frappe.get_all(
		"User",
		filters={
			"name": ("in", user_names),
			"enabled": 1,
			"user_type": "System User",
		},
		fields=["name", "email"],
	)
	return sorted({row.email for row in rows if row.email})


def notify_users(
	users,
	*,
	subject: str,
	description: str,
	document_type: str,
	document_name: str,
	link: str,
) -> list[str]:
	"""Create durable, deduplicated alerts; Frappe pushes them after commit."""
	emails = _recipient_emails(users)
	if not emails:
		return []
	from frappe.desk.doctype.notification_log.notification_log import (
		enqueue_create_notification,
	)

	enqueue_create_notification(
		emails,
		{
			"type": "Alert",
			"subject": subject,
			"description": description,
			"document_type": document_type,
			"document_name": document_name,
			"link": link,
			"from_user": frappe.session.user,
			"app": APP_NAME,
		},
		dedupe_on=["type", "document_type", "document_name", "subject"],
	)
	return emails


def _employee_label(doc) -> str:
	return escape_html(
		doc.get("employee_name")
		or frappe.db.get_value("Employee", doc.get("employee"), "employee_name")
		or doc.get("employee")
		or ""
	)


def _exception_label(doc) -> str:
	return EXCEPTION_TYPE_LABELS.get(doc.get("request_type"), doc.get("request_type") or "生产异常")


@_notification_event
def notify_worker_assignment(doc):
	return notify_users(
		[doc.employee_user],
		subject="收到新派工：{0}".format(escape_html(doc.operation or doc.job_card)),
		description="工序 {0}，生产工单 {1}。请进入“我的生产报工”查看。".format(
			escape_html(doc.operation or ""),
			escape_html(doc.work_order or ""),
		),
		document_type="Job Card Worker Assignment",
		document_name=doc.name,
		link=ASSIGNMENT_ROUTE,
	)


@_notification_event
def notify_work_report_submitted(doc, supervisor: str | None = None):
	supervisor = supervisor or doc.get("supervisor")
	if not supervisor and doc.get("assignment"):
		supervisor = frappe.db.get_value(
			"Job Card Worker Assignment",
			doc.assignment,
			"supervisor",
		)
	return notify_users(
		[supervisor],
		subject="报工待审核：{0}".format(_employee_label(doc)),
		description="{0} 已提交工序 {1} 的报工，完成数量 {2}。".format(
			_employee_label(doc),
			escape_html(doc.operation or ""),
			flt(doc.completed_qty),
		),
		document_type="Job Card Work Report",
		document_name=doc.name,
		link=REPORT_REVIEW_ROUTE,
	)


@_notification_event
def notify_work_report_decision(doc):
	approved = doc.status == "Approved"
	return notify_users(
		[doc.employee_user],
		subject="报工已通过" if approved else "报工被驳回",
		description=(
			"工序 {0} 的报工已审核通过。" if approved else "工序 {0} 的报工被驳回，请在报工记录中查看原因。"
		).format(escape_html(doc.operation or "")),
		document_type="Job Card Work Report",
		document_name=doc.name,
		link=REPORT_HISTORY_ROUTE,
	)


@_notification_event
def notify_exception_submitted(doc):
	return notify_users(
		[doc.supervisor],
		subject="生产异常待审核：{0}".format(_exception_label(doc)),
		description="{0} 提交了 {1} 申请，数量 {2}。".format(
			_employee_label(doc),
			escape_html(_exception_label(doc)),
			flt(doc.qty),
		),
		document_type="Production Exception Request",
		document_name=doc.name,
		link=EXCEPTION_REVIEW_ROUTE,
	)


@_notification_event
def notify_exception_approved(doc):
	label = _exception_label(doc)
	worker_description = (
		"{0} 已审核通过。".format(label)
		if doc.request_type == PROCESS_LOSS
		else "{0} 已审核通过，正在等待库房处理。".format(label)
	)
	notified = notify_users(
		[doc.employee_user],
		subject="生产异常已通过：{0}".format(label),
		description=worker_description,
		document_type="Production Exception Request",
		document_name=doc.name,
		link=REPORT_HISTORY_ROUTE,
	)
	if doc.request_type in {MATERIAL_RETURN, MATERIAL_SCRAP} and doc.status == AWAITING_STOCK_ENTRY:
		notified.extend(
			notify_users(
				responsibility_recipients(doc.company, WAREHOUSE_RESPONSIBILITY),
				subject="待库存处理：{0}".format(label),
				description="申请 {0} 已审核，库存单 {1} 等待提交。".format(
					escape_html(doc.name),
					escape_html(doc.stock_entry or ""),
				),
				document_type="Production Exception Request",
				document_name=doc.name,
				link=EXCEPTION_REVIEW_ROUTE,
			)
		)
	return notified


@_notification_event
def notify_exception_rejected(doc):
	return notify_users(
		[doc.employee_user],
		subject="生产异常被驳回：{0}".format(_exception_label(doc)),
		description="申请已被驳回，请在异常记录中查看原因。",
		document_type="Production Exception Request",
		document_name=doc.name,
		link=REPORT_HISTORY_ROUTE,
	)


@_notification_event
def notify_stock_entry_completed(doc):
	return notify_users(
		[doc.employee_user],
		subject="库房处理已完成：{0}".format(_exception_label(doc)),
		description="库存单 {0} 已提交，申请处理完成。".format(escape_html(doc.stock_entry or "")),
		document_type="Production Exception Request",
		document_name=doc.name,
		link=REPORT_HISTORY_ROUTE,
	)


@_notification_event
def notify_stock_entry_cancelled(doc):
	worker_emails = notify_users(
		[doc.employee_user],
		subject="库房处理已撤销：{0}".format(_exception_label(doc)),
		description="原库存单已取消，申请正在等待库房重新处理。",
		document_type="Production Exception Request",
		document_name=doc.name,
		link=REPORT_HISTORY_ROUTE,
	)
	warehouse_emails = notify_users(
		responsibility_recipients(doc.company, WAREHOUSE_RESPONSIBILITY),
		subject="库存处理需重做：{0}".format(_exception_label(doc)),
		description="申请 {0} 的原库存单已取消，请重新处理。".format(escape_html(doc.name)),
		document_type="Production Exception Request",
		document_name=doc.name,
		link=EXCEPTION_REVIEW_ROUTE,
	)
	return worker_emails + warehouse_emails


@_notification_event
def notify_quick_order_shortage(sales_order: str, company: str, shortages) -> list[str]:
	shortage_count = len(shortages or [])
	if not shortage_count:
		return []
	return notify_users(
		responsibility_recipients(company, PROCUREMENT_RESPONSIBILITY),
		subject="销售订单有缺料待采购：{0}".format(sales_order),
		description="销售订单 {0} 存在 {1} 项采购缺口，请进入缺料采购计划处理。".format(
			escape_html(sales_order),
			shortage_count,
		),
		document_type="Sales Order",
		document_name=sales_order,
		link=SHORTAGE_ROUTE,
	)
