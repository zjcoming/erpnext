from __future__ import annotations

from functools import wraps

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt

from process_simplification.management_access import (
	OWNER_ROLE,
	PRODUCTION_MANAGER_ROLE,
	ROLE_DEFINITIONS,
	WAREHOUSE_OPERATOR_ROLE,
)
from process_simplification.production_exceptions.constants import (
	AWAITING_STOCK_ENTRY,
	MATERIAL_RETURN,
	MATERIAL_SCRAP,
	PROCESS_LOSS,
)


WAREHOUSE_RESPONSIBILITY = "库存处理"
PROCUREMENT_RESPONSIBILITY = "缺料采购"
PRODUCTION_DISPATCH_RESPONSIBILITY = "生产派工"
PURCHASE_RECEIPT_RESPONSIBILITY = "采购到货"
RESPONSIBILITIES = {
	WAREHOUSE_RESPONSIBILITY,
	PROCUREMENT_RESPONSIBILITY,
	PRODUCTION_DISPATCH_RESPONSIBILITY,
	PURCHASE_RECEIPT_RESPONSIBILITY,
}
RESPONSIBILITY_ROLES = {
	WAREHOUSE_RESPONSIBILITY: {WAREHOUSE_OPERATOR_ROLE, OWNER_ROLE},
	PROCUREMENT_RESPONSIBILITY: {WAREHOUSE_OPERATOR_ROLE, OWNER_ROLE},
	PRODUCTION_DISPATCH_RESPONSIBILITY: {PRODUCTION_MANAGER_ROLE, OWNER_ROLE},
	PURCHASE_RECEIPT_RESPONSIBILITY: {
		PRODUCTION_MANAGER_ROLE,
		OWNER_ROLE,
		WAREHOUSE_OPERATOR_ROLE,
	},
}
RESPONSIBILITY_ROLE_PRIORITY = {
	WAREHOUSE_RESPONSIBILITY: (WAREHOUSE_OPERATOR_ROLE, OWNER_ROLE),
	PROCUREMENT_RESPONSIBILITY: (WAREHOUSE_OPERATOR_ROLE, OWNER_ROLE),
	PRODUCTION_DISPATCH_RESPONSIBILITY: (PRODUCTION_MANAGER_ROLE, OWNER_ROLE),
	PURCHASE_RECEIPT_RESPONSIBILITY: (
		PRODUCTION_MANAGER_ROLE,
		OWNER_ROLE,
		WAREHOUSE_OPERATOR_ROLE,
	),
}
PURCHASE_RECEIPT_DEFAULT_ROLES = {PRODUCTION_MANAGER_ROLE, OWNER_ROLE}

APP_NAME = "process_simplification"
PROCESS_NOTIFICATION_REALTIME_EVENT = "process_simplification_notification"
STANDARD_MATERIAL_REQUEST_RECEIPT_NOTIFICATION = "Material Request Receipt Notification"
ASSIGNMENT_ROUTE = "/app/active-production-work"
REPORT_REVIEW_ROUTE = "/app/production-report-review"
REPORT_HISTORY_ROUTE = "/app/production-report-history"
EXCEPTION_REVIEW_ROUTE = "/app/production-exception-review"
SHORTAGE_ROUTE = "/app/shortage-purchase-planning"
PRODUCTION_WORKBENCH_ROUTE = "/app/production-workbench"

EXCEPTION_TYPE_LABELS = {
	MATERIAL_RETURN: "余料退库",
	MATERIAL_SCRAP: "物料报废",
	PROCESS_LOSS: "工序损耗",
}


def _notification_setting_enabled(fieldname: str, *, default: bool = True) -> bool:
	if not frappe.db.exists("DocType", "Process Simplification Settings"):
		return default
	values = frappe.db.sql(
		"""
		select value
		from `tabSingles`
		where doctype = 'Process Simplification Settings'
		  and field = %s
		limit 1
		""",
		fieldname,
		pluck=True,
	)
	return default if not values else bool(cint(values[0]))


def process_notifications_enabled() -> bool:
	return _notification_setting_enabled("enable_process_notifications")


def process_notification_sound_enabled() -> bool:
	return _notification_setting_enabled("enable_notification_sound")


def disable_standard_material_request_receipt_email() -> bool:
	"""Replace ERPNext's email-only receipt alert with our in-app notification."""
	if not frappe.db.exists("Notification", STANDARD_MATERIAL_REQUEST_RECEIPT_NOTIFICATION):
		return False
	if not frappe.db.get_value(
		"Notification",
		STANDARD_MATERIAL_REQUEST_RECEIPT_NOTIFICATION,
		"enabled",
	):
		return False

	frappe.db.set_value(
		"Notification",
		STANDARD_MATERIAL_REQUEST_RECEIPT_NOTIFICATION,
		"enabled",
		0,
		update_modified=False,
	)
	from frappe.email.doctype.notification.notification import clear_notification_cache

	clear_notification_cache()
	return True


def allowed_notification_role_profiles(responsibility: str) -> tuple[str, ...]:
	profiles_by_role = {
		definition["role"]: definition["profile"] for definition in ROLE_DEFINITIONS
	}
	return tuple(
		profiles_by_role[role]
		for role in RESPONSIBILITY_ROLE_PRIORITY.get(responsibility, ())
		if role in profiles_by_role
	)


def eligible_notification_users(company: str, responsibility: str):
	if not company or responsibility not in RESPONSIBILITIES:
		return []
	allowed_roles = RESPONSIBILITY_ROLES[responsibility]
	rows = frappe.get_all(
		"User",
		filters={"enabled": 1, "user_type": "System User"},
		fields=["name", "full_name"],
		order_by="full_name, name",
		limit=0,
	)
	return [
		row
		for row in rows
		if row.name not in {"Administrator", "Guest"}
		and allowed_roles.intersection(frappe.get_roles(row.name))
		and _user_matches_company(row.name, company)
	]


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

	seen_profiles = set()
	for row in settings.get("notification_role_recipients") or []:
		if row.responsibility not in RESPONSIBILITIES:
			frappe.throw(_("Select a valid notification responsibility."))
		key = (row.company, row.responsibility, row.role_profile)
		if key in seen_profiles:
			frappe.throw(_("The same additional notification role is configured more than once."))
		seen_profiles.add(key)
		if row.role_profile not in allowed_notification_role_profiles(row.responsibility):
			frappe.throw(
				_("Role Profile {0} is not eligible for {1} notifications.").format(
					row.role_profile,
					row.responsibility,
				)
			)


def _configured_recipients(company: str, responsibility: str) -> list[str]:
	"""Return valid additive user and Role Profile recipients."""
	users = set()
	if frappe.db.table_exists("Process Notification Recipient"):
		users.update(
			frappe.get_all(
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
		)
	if frappe.db.table_exists("Process Notification Role Recipient"):
		profiles = frappe.get_all(
			"Process Notification Role Recipient",
			filters={
				"parent": "Process Simplification Settings",
				"parenttype": "Process Simplification Settings",
				"parentfield": "notification_role_recipients",
				"company": company,
				"responsibility": responsibility,
			},
			pluck="role_profile",
		)
		valid_profiles = set(allowed_notification_role_profiles(responsibility))
		for profile in set(profiles).intersection(valid_profiles):
			users.update(
				frappe.get_all(
					"User Role Profile",
					filters={
						"parenttype": "User",
						"parentfield": "role_profiles",
						"role_profile": profile,
					},
					pluck="parent",
					limit=0,
				)
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


def _default_responsibility_recipients(company: str, responsibility: str) -> list[str]:
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
	if responsibility == PURCHASE_RECEIPT_RESPONSIBILITY:
		return sorted(
			user
			for user, roles in roles_by_user.items()
			if PURCHASE_RECEIPT_DEFAULT_ROLES.intersection(roles)
			and _user_matches_company(user, company)
		)
	for role in RESPONSIBILITY_ROLE_PRIORITY[responsibility]:
		matching = sorted(
			user
			for user, roles in roles_by_user.items()
			if role in roles and _user_matches_company(user, company)
		)
		if matching:
			return matching
	return []


def responsibility_recipients(company: str, responsibility: str) -> list[str]:
	if not company or responsibility not in RESPONSIBILITIES:
		return []
	return sorted(
		set(_default_responsibility_recipients(company, responsibility)).union(
			_configured_recipients(company, responsibility)
		)
	)


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
	if not process_notifications_enabled():
		return []
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


def publish_notification_sound(doc, method=None) -> None:
	"""Push a persisted app notification to its exact recipient for client consumption."""
	if doc.get("app") != APP_NAME or not doc.get("for_user"):
		return
	frappe.publish_realtime(
		PROCESS_NOTIFICATION_REALTIME_EVENT,
		{
			"notification_log": doc.name,
			"type": doc.get("type"),
			"subject": doc.get("subject"),
			"link": doc.get("link"),
			"document_type": doc.get("document_type"),
			"document_name": doc.get("document_name"),
			"play_sound": (
				process_notifications_enabled() and process_notification_sound_enabled()
			),
		},
		after_commit=True,
		user=doc.for_user,
	)


def _employee_label(doc) -> str:
	return escape_html(
		doc.get("employee_name")
		or frappe.db.get_value("Employee", doc.get("employee"), "employee_name")
		or doc.get("employee")
		or ""
	)


def _exception_label(doc) -> str:
	return EXCEPTION_TYPE_LABELS.get(doc.get("request_type"), doc.get("request_type") or "生产异常")


def _next_job_card(doc):
	if not doc.get("work_order") or not doc.get("name"):
		return None
	job_cards = frappe.get_all(
		"Job Card",
		filters={
			"work_order": doc.work_order,
			"is_corrective_job_card": 0,
			"docstatus": ["<", 2],
		},
		fields=["name", "operation", "sequence_id", "creation", "docstatus", "status"],
		order_by="sequence_id asc, creation asc",
		limit=0,
	)
	for index, row in enumerate(job_cards):
		if row.name != doc.name:
			continue
		candidates = [
			candidate
			for candidate in job_cards[index + 1 :]
			if not (
				int(candidate.get("docstatus") or 0) == 1
				and candidate.get("status") == "Completed"
			)
		]
		if not candidates:
			return None
		first = candidates[0]
		# A parallel sequence is a group, not one deterministic "next" card.
		# Fall back to the generic unfinished-operation message instead of
		# misleadingly naming only one worker task.
		if first.get("sequence_id") and any(
			candidate.get("sequence_id") == first.get("sequence_id")
			for candidate in candidates[1:]
		):
			return None
		return first
	return None


def _all_regular_job_cards_completed(work_order: str | None) -> bool:
	if not work_order:
		return False
	rows = frappe.get_all(
		"Job Card",
		filters={"work_order": work_order, "is_corrective_job_card": 0, "docstatus": ["<", 2]},
		fields=["docstatus", "status"],
		limit=0,
	)
	return bool(rows) and all(
		int(row.get("docstatus") or 0) == 1 and row.get("status") == "Completed"
		for row in rows
	)


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
def notify_worker_redispatch(doc, qty):
	return notify_users(
		[doc.employee_user],
		subject="收到二次派工：{0}".format(escape_html(doc.operation or doc.job_card)),
		description="生产工单 {0}、工序 {1} 新分配 {2} 件未完成任务。请进入“我的生产报工”查看。".format(
			escape_html(doc.work_order or ""),
			escape_html(doc.operation or ""),
			flt(qty),
		),
		document_type="Job Card Worker Assignment",
		document_name=doc.name,
		link=ASSIGNMENT_ROUTE,
	)


@_notification_event
def notify_worker_assignment_cancelled(doc):
	return notify_users(
		[doc.get("employee_user")],
		subject="派工已取消：{0}".format(escape_html(doc.get("operation") or doc.get("job_card") or "")),
		description=(
			"生产工单 {0}、工序 {1} 的派工已取消，该任务已从“我的生产报工”移除。"
		).format(
			escape_html(doc.get("work_order") or ""),
			escape_html(doc.get("operation") or ""),
		),
		document_type="Job Card Worker Assignment",
		document_name=doc.get("name"),
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
def notify_operation_completed(doc):
	"""Notify production dispatch whenever a managed Job Card is submitted."""
	company = doc.get("company") or frappe.db.get_value(
		"Work Order",
		doc.get("work_order"),
		"company",
	)
	if not company:
		return []

	completed_operation = escape_html(doc.get("operation") or doc.get("name") or "工序")
	work_order = escape_html(doc.get("work_order") or "")
	next_job_card = _next_job_card(doc)
	if next_job_card:
		next_operation = escape_html(next_job_card.operation or next_job_card.name)
		subject = "上一工序已完成，待派工：{0}".format(next_operation)
		description = (
			"生产工单 {0} 的工序 {1} 已完成。下一工序为 {2}（{3}），"
			"请进入生产计划中心查看并派工。"
		).format(
			work_order,
			completed_operation,
			next_operation,
			escape_html(next_job_card.name),
		)
	elif _all_regular_job_cards_completed(doc.get("work_order")):
		subject = "所有工序已完成，待申请入库：{0}".format(work_order)
		description = (
			"生产工单 {0} 的全部工序已经完成。请进入生产计划中心发起完工入库申请；"
			"库房提交入库单后库存才会增加。"
		).format(work_order)
	else:
		subject = "工序已完成：{0}".format(completed_operation)
		description = "生产工单 {0} 的工序 {1} 已完成，仍有其他工序未完成。".format(
			work_order, completed_operation
		)

	return notify_users(
		responsibility_recipients(company, PRODUCTION_DISPATCH_RESPONSIBILITY),
		subject=subject,
		description=description,
		document_type="Job Card",
		document_name=doc.name,
		link=PRODUCTION_WORKBENCH_ROUTE,
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


@_notification_event
def notify_material_request_received(doc, method=None):
	"""Notify production and factory management when purchased material arrives."""
	status = doc.get("status")
	if status not in {"Partially Received", "Received"}:
		return []

	before_save = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	if before_save and before_save.get("status") == status:
		return []

	company = doc.get("company")
	if not company:
		return []

	fully_received = status == "Received"
	subject = "物料需求已全部到货：{0}" if fully_received else "物料需求部分到货：{0}"
	description = (
		"物料需求单 {0} 已全部收货，采购入库库存已经更新。"
		if fully_received
		else "物料需求单 {0} 已部分收货，当前到货进度 {1}%。"
	)
	if fully_received:
		description = description.format(escape_html(doc.get("name") or ""))
	else:
		description = description.format(
			escape_html(doc.get("name") or ""),
			flt(doc.get("per_received"), 2),
		)
	description += "请查看物料需求单，并检查相关生产需求的齐套、发料和派工安排。"

	return notify_users(
		responsibility_recipients(company, PURCHASE_RECEIPT_RESPONSIBILITY),
		subject=subject.format(escape_html(doc.get("name") or "")),
		description=description,
		document_type="Material Request",
		document_name=doc.get("name"),
		link="/app/material-request/{0}".format(doc.get("name")),
	)


@_notification_event
def notify_production_stock_request(doc, *, request_type: str):
	label = "完工入库" if request_type == "receipt" else "生产发料"
	return notify_users(
		responsibility_recipients(doc.company, WAREHOUSE_RESPONSIBILITY),
		subject="待库房处理：{0} {1}".format(label, escape_html(doc.work_order or "")),
		description=(
			"生产主管已发起{0}申请，库存单 {1} 等待库房复核并提交。"
			"库存数量在提交前不会变化。"
		).format(label, escape_html(doc.name)),
		document_type="Stock Entry",
		document_name=doc.name,
		link="/app/stock-entry/{0}".format(doc.name),
	)


@_notification_event
def notify_production_stock_completed(doc, *, replenishment_result=None):
	action = doc.get("custom_process_workflow_action")
	if action not in {"Receipt Request", "Material Issue Request", "Replenishment Receipt"}:
		return []
	if action == "Material Issue Request":
		from process_simplification.production_workflow.service import (
			remaining_material_issue_qty,
		)

		work_order = frappe.get_doc("Work Order", doc.get("work_order"))
		# The Work Order header can still reflect an earlier gross issue after a
		# return. Notify "可派工" only from the current row-level net coverage.
		fully_issued = remaining_material_issue_qty(work_order) <= 1e-6
		if fully_issued:
			subject = "发料已完成，可派工：{0}".format(escape_html(doc.work_order or ""))
			description = "库房已提交发料单 {0}。请在生产计划中心正式派工并通知员工。".format(
				escape_html(doc.name)
			)
		else:
			subject = "部分发料已完成，仍待补料：{0}".format(escape_html(doc.work_order or ""))
			description = "库房已提交部分发料单 {0}。工单仍未齐套，补齐前不会开放正式派工。".format(
				escape_html(doc.name)
			)
	elif action == "Replenishment Receipt":
		result = frappe._dict(replenishment_result or {})
		reserved_qty = flt(result.get("reserved_qty"))
		received_qty = flt(result.get("received_qty"))
		target = escape_html(result.get("target_work_order") or "")
		if reserved_qty > 0 and reserved_qty + 1e-9 >= received_qty:
			subject = "补产已入库并定向预留：{0}".format(target)
			description = (
				"补产入库单 {0} 已提交，数量 {1} 已定向预留给原缺料工单。"
			).format(escape_html(doc.name), flt(reserved_qty, 6))
		elif reserved_qty > 0:
			subject = "补产已入库，部分定向预留：{0}".format(target)
			description = (
				"补产入库单 {0} 已提交；已定向预留 {1}，其余 {2} 作为普通库存。"
			).format(
				escape_html(doc.name),
				flt(reserved_qty, 6),
				flt(result.get("surplus_qty"), 6),
			)
		else:
			subject = "补产已入库，原缺口已变化：{0}".format(target)
			description = (
				"补产入库单 {0} 已提交，但原工单已结束、缺口已补齐或库存已被其他硬预留占用；"
				"本次产出作为普通库存，请在生产计划中心重新检查。"
			).format(escape_html(doc.name))
	else:
		subject = "入库已完成，待检查下游发料：{0}".format(escape_html(doc.work_order or ""))
		description = (
			"库房已提交完工入库单 {0}。请进入生产计划中心：已齐套的下游工单会显示"
			"“可申请发料”，仍缺料的工单会同时显示抢占来源和补产入口。"
		).format(escape_html(doc.name))
	company = doc.get("company") or frappe.db.get_value("Work Order", doc.get("work_order"), "company")
	return notify_users(
		responsibility_recipients(company, PRODUCTION_DISPATCH_RESPONSIBILITY),
		subject=subject,
		description=description,
		document_type="Stock Entry",
		document_name=doc.name,
		link=PRODUCTION_WORKBENCH_ROUTE,
	)


@_notification_event
def notify_production_stock_cancelled(doc):
	action = doc.get("custom_process_workflow_action")
	if action not in {"Receipt Request", "Material Issue Request", "Replenishment Receipt"}:
		return []
	label = {
		"Receipt Request": "完工入库",
		"Material Issue Request": "生产发料",
		"Replenishment Receipt": "补产入库",
	}[action]
	company = doc.get("company") or frappe.db.get_value("Work Order", doc.get("work_order"), "company")
	return notify_users(
		responsibility_recipients(company, PRODUCTION_DISPATCH_RESPONSIBILITY),
		subject="{0}已撤销，流程重新待处理：{1}".format(
			label, escape_html(doc.work_order or "")
		),
		description="库存单 {0} 已取消，请回到生产计划中心按最新库存重新处理。".format(
			escape_html(doc.name)
		),
		document_type="Stock Entry",
		document_name=doc.name,
		link=PRODUCTION_WORKBENCH_ROUTE,
	)


@_notification_event
def notify_production_stock_withdrawn(doc):
	action = doc.get("custom_process_workflow_action")
	if action not in {"Receipt Request", "Material Issue Request"}:
		return []
	label = "完工入库" if action == "Receipt Request" else "生产发料"
	return notify_users(
		responsibility_recipients(doc.get("company"), WAREHOUSE_RESPONSIBILITY),
		subject="{0}申请已撤回：{1}".format(label, escape_html(doc.get("work_order") or "")),
		description="生产主管已撤回库存草稿 {0}；该申请关联的库存预留已经释放。".format(
			escape_html(doc.get("name") or "")
		),
		document_type="Work Order",
		document_name=doc.get("work_order"),
		link=PRODUCTION_WORKBENCH_ROUTE,
	)
