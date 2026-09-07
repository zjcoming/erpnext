"""Durable, transaction-bound notifications for individual receipt actions."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from functools import partial

import frappe
from frappe.desk.doctype.notification_settings.notification_settings import is_notifications_enabled
from frappe.utils import add_to_date, cint, escape_html, flt, get_datetime, now_datetime

from process_simplification.notifications import (
	APP_NAME,
	PURCHASE_RECEIPT_RESPONSIBILITY,
	_enabled_system_user,
	_user_matches_company,
	process_notifications_enabled,
	responsibility_recipients,
)

EVENT = "Purchase Receipt Event"
SETTINGS = "Process Simplification Settings"
RETRY_MINUTES = (1, 2, 5, 10, 30)
LABELS = {
	"Arrival": "采购到货",
	"Return": "采购退货更正",
	"Receipt Cancelled": "采购收货已撤销",
	"Return Cancelled": "采购退货已撤销",
}


def ensure_defaults():
	"""Enable only future document actions; never scan or replay historical receipts."""
	frappe.db.add_index("Purchase Allocation Batch", ["material_request"])
	frappe.db.add_index(EVENT, ["status", "occurred_at"])
	frappe.db.add_index(EVENT, ["receipt", "occurred_at"])
	if not events_enabled():
		frappe.db.set_single_value(SETTINGS, "purchase_receipt_notifications_started_at", now_datetime())


def events_enabled():
	started_at = frappe.db.get_single_value(SETTINGS, "purchase_receipt_notifications_started_at")
	# Frappe casts an unset Datetime single field to datetime.min, which is truthy.
	return bool(started_at and started_at != datetime.min)


def lock_material_requests(names):
	# Current reads after this lock also serialize independent receipts for one demand.
	table = frappe.qb.DocType("Material Request")
	for name in sorted(set(filter(None, names))):
		frappe.qb.from_(table).select(table.name).where(table.name == name).for_update().run()


def lock_receipt_sources(doc, method=None):
	if cint(doc.docstatus) == 1 or method == "before_cancel":
		lock_material_requests(row.material_request for row in doc.items)


def event_name(receipt, event_type):
	return "RCN-" + hashlib.sha256(f"{receipt}:{event_type}".encode()).hexdigest()[:32]


def request_progress(names):
	"""Use accepted stock quantities, including signed returns, per exact source row."""
	result = []
	for name in sorted(set(filter(None, names))):
		items = frappe.db.sql(
			"""select name, item_code, item_name, stock_qty, stock_uom, warehouse
			from `tabMaterial Request Item` where parent=%s order by idx for update""",
			name,
			as_dict=True,
		)
		received = defaultdict(float)
		# FOR UPDATE is intentional: a waiting concurrent receipt must see the latest commit.
		for row in frappe.db.sql(
			"""select i.material_request_item, i.stock_qty
			from `tabPurchase Receipt Item` i join `tabPurchase Receipt` p on p.name=i.parent
			where i.material_request=%s and p.docstatus=1 for update""",
			name,
			as_dict=True,
		):
			received[row.material_request_item] += flt(row.stock_qty)
		for row in items:
			row.received_qty = flt(received[row.name], 6)
			row.remaining_qty = max(flt(flt(row.stock_qty) - row.received_qty, 6), 0)
		result.append(
			{"material_request": name, "complete": all(r.remaining_qty == 0 for r in items), "items": items}
		)
	return result


def record_receipt_event(doc, method=None):
	if not events_enabled():
		return
	if method == "on_cancel":
		kind = "Return Cancelled" if doc.is_return else "Receipt Cancelled"
	else:
		if cint(doc.docstatus) != 1:
			return
		kind = "Return" if doc.is_return else "Arrival"
	name = event_name(doc.name, kind)
	if frappe.db.exists(EVENT, name):
		return name
	lock_material_requests(row.material_request for row in doc.items)
	snapshot = {
		"posting_date": str(doc.posting_date),
		"return_against": doc.return_against,
		"amended_from": doc.amended_from,
		"items": [
			{
				key: row.get(key)
				for key in (
					"item_code",
					"item_name",
					"qty",
					"received_qty",
					"rejected_qty",
					"uom",
					"stock_qty",
					"stock_uom",
					"conversion_factor",
					"warehouse",
					"rejected_warehouse",
					"purchase_order",
					"purchase_order_item",
					"material_request",
					"material_request_item",
				)
			}
			for row in doc.items
		],
		"requests": request_progress(row.material_request for row in doc.items),
	}
	enabled = process_notifications_enabled()
	recipients = (
		sorted(set(responsibility_recipients(doc.company, PURCHASE_RECEIPT_RESPONSIBILITY)))
		if enabled
		else []
	)
	event = frappe.get_doc(
		{
			"doctype": EVENT,
			"name": name,
			"receipt": doc.name,
			"event_type": kind,
			"company": doc.company,
			"supplier": doc.supplier,
			"actor": frappe.session.user,
			"occurred_at": now_datetime(),
			"snapshot": json.dumps(snapshot, ensure_ascii=False, default=str),
			"status": ("Pending" if recipients else "No Recipients") if enabled else "Suppressed",
			"last_error": ("未配置有效接收人" if enabled else "事件发生时流程通知已关闭")
			if not recipients
			else None,
			"deliveries": [{"recipient": user, "status": "Pending", "attempts": 0} for user in recipients],
		}
	)
	event.insert(ignore_permissions=True)
	# Event and inventory commit together. Redis/worker availability cannot lose the event.
	if recipients:
		frappe.db.after_commit.add(partial(enqueue_event, name))
	return name


def enqueue_event(name):
	try:
		frappe.enqueue(
			"process_simplification.purchasing.receipts.deliver_event", event_name=name, queue="short"
		)
	except Exception:
		frappe.logger("purchase_receipt_events", allow_site=True).exception(
			"Receipt event remains pending: %s", name
		)


def retry_pending_events():
	# This scheduled method runs on the existing worker; no separate service is required.
	for name in frappe.get_all(
		EVENT,
		filters={"status": ["in", ["Pending", "Partial"]]},
		pluck="name",
		order_by="occurred_at asc, name asc",
		limit=200,
	):
		try:
			deliver_event(name)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.logger("purchase_receipt_events", allow_site=True).exception(
				"Receipt retry failed: %s", name
			)


def _notification_message(event):
	"""Keep the notification inbox brief; the immutable snapshot powers the full detail."""
	snapshot = json.loads(event.snapshot)
	supplier = str(event.supplier or "")
	if len(supplier) > 24:
		supplier = supplier[:23] + "…"
	subject = f"{LABELS[event.event_type]} · {supplier}"
	items = snapshot.get("items", [])
	count = len({row.get("item_code") or row.get("item_name") for row in items})
	if event.event_type == "Arrival":
		summary = f"本批到货 {count} 项物料"
		requests = snapshot.get("requests", [])
		if requests:
			summary += " · 相关申请已全部到货" if all(row["complete"] for row in requests) else " · 相关申请尚未到齐"
		if any(flt(row.get("rejected_qty")) for row in items):
			summary += "（含拒收）"
	elif event.event_type == "Return":
		summary = f"本批退货涉及 {count} 项物料，请核对缺料"
	else:
		summary = f"本次撤销涉及 {count} 项物料，请核对最新进度"
	receipt = str(event.receipt or "")
	if len(receipt) > 40:
		receipt = receipt[:39] + "…"
	return subject, f"{escape_html(summary)}<br>{escape_html(receipt)} · 点击查看详情"


def _message(event):
	snapshot = json.loads(event.snapshot)
	subject = f"{LABELS[event.event_type]}：{event.supplier} · {event.receipt}"
	if (
		event.event_type == "Arrival"
		and snapshot["requests"]
		and all(r["complete"] for r in snapshot["requests"])
	):
		subject += "（相关申请已全部到货）"
	parts = [f"统计时点：{escape_html(str(event.occurred_at))}。"]
	if event.event_type in {"Receipt Cancelled", "Return Cancelled"}:
		parts.append("以下数量为本次撤销涉及的原单数量，库存及需求进度已按撤销更正。")
	if (
		event.event_type == "Arrival"
		and frappe.db.get_value("Purchase Receipt", event.receipt, "docstatus") == 2
	):
		parts.append("注意：此收货随后已撤销，请以当前需求及库存为准。")
	for row in snapshot["items"]:
		parts.append(
			"{0}：送达 {1:g} {2}，合格 {3:g} {2}，拒收 {4:g} {2}；合格库存 {5:g} {6}。".format(
				escape_html(row["item_name"] or row["item_code"]),
				abs(flt(row["received_qty"])),
				escape_html(row["uom"]),
				abs(flt(row["qty"])),
				abs(flt(row["rejected_qty"])),
				abs(flt(row["stock_qty"])),
				escape_html(row["stock_uom"]),
			)
		)
	for request in snapshot["requests"]:
		parts.append(
			escape_html(request["material_request"])
			+ ("：申请已全部到货。" if request["complete"] else "：申请尚未到齐。")
		)
		for row in request["items"]:
			parts.append(
				"{0}：净合格 {1:g} / {2:g} {3}，剩余 {4:g} {3}。".format(
					escape_html(row["item_name"] or row["item_code"]),
					row["received_qty"],
					flt(row["stock_qty"]),
					escape_html(row["stock_uom"]),
					row["remaining_qty"],
				)
			)
	parts.append("请在生产工作台检查其他物料、齐套及发料安排。")
	return subject, "<br>".join(parts)


def deliver_event(event_name):
	# Serialize workers on the durable parent. Notification + delivered flag share one transaction.
	table = frappe.qb.DocType(EVENT)
	if not frappe.qb.from_(table).select(table.name).where(table.name == event_name).for_update().run():
		return
	event = frappe.get_doc(EVENT, event_name)
	if event.status in {"Delivered", "Suppressed", "No Recipients", "Skipped", "Failed"}:
		return
	# A later correction must not overtake an earlier pending event for this receipt.
	if frappe.db.exists(
		EVENT,
		{
			"receipt": event.receipt,
			"occurred_at": ["<", event.occurred_at],
			"status": ["in", ["Pending", "Partial", "Failed"]],
		},
	):
		return
	subject, description = _notification_message(event)
	eligible = set(responsibility_recipients(event.company, PURCHASE_RECEIPT_RESPONSIBILITY))
	for row in event.deliveries:
		if row.status not in {"Pending", "Retry"} or (
			row.next_attempt and get_datetime(row.next_attempt) > now_datetime()
		):
			continue
		if not process_notifications_enabled() or not is_notifications_enabled(row.recipient):
			row.status, row.last_error = "Skipped", "投递时通知已关闭，不在恢复开关后补发"
			continue
		if (
			row.recipient not in eligible
			or not _enabled_system_user(row.recipient)
			or not _user_matches_company(row.recipient, event.company)
		):
			row.status, row.last_error = "Skipped", "接收人已停用、失去岗位或公司权限"
			continue
		row.attempts = cint(row.attempts) + 1
		frappe.db.savepoint("receipt_delivery")
		realtime_count = len(getattr(frappe.local, "_realtime_log", []))
		try:
			log_name = "RCNL-" + hashlib.sha256(f"{event.name}:{row.recipient}".encode()).hexdigest()[:32]
			if not frappe.db.exists("Notification Log", log_name):
				log = frappe.get_doc(
					{
						"doctype": "Notification Log",
						"type": "Alert",
						"app": APP_NAME,
						"for_user": row.recipient,
						"from_user": event.actor,
						"subject": escape_html(subject),
						"title": escape_html(subject),
						"description": description,
						"document_type": EVENT,
						"document_name": event.name,
						"link": f"/desk/purchase-receipt-notice?name={event.name}",
					}
				)
				log.insert(ignore_permissions=True, set_name=log_name)
			row.notification_log, row.status = log_name, "Delivered"
			row.last_error, row.next_attempt = None, None
		except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			raise
		except Exception:
			frappe.db.rollback(save_point="receipt_delivery")
			if hasattr(frappe.local, "_realtime_log"):
				del frappe.local._realtime_log[realtime_count:]
			row.last_error = frappe.get_traceback()[-1800:]
			row.status = "Retry" if row.attempts <= len(RETRY_MINUTES) else "Failed"
			row.next_attempt = (
				add_to_date(now_datetime(), minutes=RETRY_MINUTES[row.attempts - 1])
				if row.status == "Retry"
				else None
			)
	statuses = {row.status for row in event.deliveries}
	if statuses & {"Pending", "Retry"}:
		event.status = "Partial" if "Delivered" in statuses else "Pending"
	elif "Failed" in statuses:
		event.status = "Failed"
	else:
		event.status = "Delivered" if "Delivered" in statuses else "Skipped"
	event.last_error = next((row.last_error for row in event.deliveries if row.status == "Failed"), None)
	event.save(ignore_permissions=True)


def _can_manage(company):
	from process_simplification.management_access import OWNER_ROLE

	return (
		frappe.session.user == "Administrator"
		or bool({OWNER_ROLE, "System Manager"} & set(frappe.get_roles()))
	) and _user_matches_company(frappe.session.user, company)


def _can_open_receipt(name):
	# Avoid Frappe's nested, noisy denial when a summary reader lacks native receipt access.
	return bool(
		frappe.has_permission("Purchase Receipt", "read")
		and frappe.has_permission("Purchase Receipt", "read", doc=frappe.get_doc("Purchase Receipt", name))
	)


def _require_summary_access(event):
	user = frappe.session.user
	if not _user_matches_company(user, event.company):
		frappe.throw("无权查看其他公司的到货信息。", frappe.PermissionError)
	if _can_manage(event.company):
		return
	if not _enabled_system_user(user):
		frappe.throw("当前用户不能查看到货信息。", frappe.PermissionError)
	if user in responsibility_recipients(event.company, PURCHASE_RECEIPT_RESPONSIBILITY):
		return
	if _can_open_receipt(event.receipt):
		return
	frappe.throw("没有此到货通知的查看权限。", frappe.PermissionError)


@frappe.whitelist()
def get_notice(name):
	event = frappe.get_doc(EVENT, name)
	_require_summary_access(event)
	subject, description = _message(event)
	result = {
		key: event.get(key)
		for key in ("name", "receipt", "event_type", "company", "supplier", "occurred_at", "status")
	}
	result.update(
		subject=subject,
		description=description,
		snapshot=json.loads(event.snapshot),
		current_docstatus=frappe.db.get_value("Purchase Receipt", event.receipt, "docstatus"),
		can_open_receipt=_can_open_receipt(event.receipt),
		can_retry=_can_manage(event.company),
	)
	if result["can_retry"]:
		result["deliveries"] = [
			{key: row.get(key) for key in ("recipient", "status", "attempts", "last_error")}
			for row in event.deliveries
		]
		result["last_error"] = event.last_error
	return result


@frappe.whitelist()
def list_notices():
	# Scope rows before exposing even identifiers or status counts.
	result = []
	for row in frappe.get_all(
		EVENT,
		fields=["name", "company", "receipt", "supplier", "event_type", "status", "occurred_at"],
		order_by="occurred_at desc",
		limit=300,
	):
		if _can_manage(row.company):
			result.append(row)
	return result


@frappe.whitelist(methods=["POST"])
def retry_notice(name):
	event = frappe.get_doc(EVENT, name)
	if not _can_manage(event.company):
		frappe.throw("仅本公司管理人员可以重试通知。", frappe.PermissionError)
	table = frappe.qb.DocType(EVENT)
	frappe.qb.from_(table).select(table.name).where(table.name == name).for_update().run()
	event.reload()
	if event.status == "Suppressed":
		frappe.throw("此事件在通知关闭时被抑制，不能补发。")
	if event.status == "No Recipients":
		for user in responsibility_recipients(event.company, PURCHASE_RECEIPT_RESPONSIBILITY):
			event.append("deliveries", {"recipient": user, "status": "Pending"})
	for row in event.deliveries:
		if row.status in {"Failed", "Retry"}:
			row.status, row.attempts, row.next_attempt = "Pending", 0, None
	if any(row.status == "Pending" for row in event.deliveries):
		event.status, event.last_error = "Pending", None
		event.save(ignore_permissions=True)
		frappe.db.after_commit.add(partial(enqueue_event, name))
	return event.status
