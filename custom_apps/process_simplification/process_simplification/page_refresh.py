"""Committed change hints and permission-scoped, short-lived display caches.

Hints contain no business records. They are independent of Notification Log and
never authorize a read or a write. A missed socket event is repaired by versions.
"""

import json
import time
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import frappe
from frappe.utils import nowdate

from process_simplification.management_access import user_company_scope, user_has_capability

EVENT = "process_simplification_data_changed"
PREFIX = "ps:page-refresh:v1:"


def _cache():
	return frappe.cache


TOPICS = {
	"tasks": ("worker_reporting",),
	"review": ("production_review", "exception_stock"),
	"warehouse": ("warehouse_workbench", "exception_stock"),
	"orders": ("order_workbench", "sales_order"),
	"production": ("production_planning",),
	"purchase": ("shortage_purchase",),
	"dashboard": ("executive_dashboard",),
	"wages": ("wage_management", "worker_reporting"),
	"notifications": (),
}
DOCUMENT_TOPICS = {
	"Sales Order": {"orders", "production", "purchase", "dashboard"},
	"Production Plan": {"orders", "production", "purchase"},
	"Work Order": {"tasks", "review", "orders", "production", "purchase", "warehouse", "dashboard"},
	"Job Card": {"tasks", "review", "production", "orders"},
	"Job Card Worker Assignment": {"tasks", "review", "production"},
	"Job Card Assignment Movement": {"tasks", "review", "production"},
	"Job Card Work Report": {"tasks", "review", "production", "orders", "wages"},
	"Production Exception Request": {"tasks", "review", "warehouse", "production", "purchase"},
	"Stock Entry": {"tasks", "review", "warehouse", "orders", "production", "purchase", "dashboard"},
	"Stock Reservation Entry": {"orders", "production", "purchase", "warehouse"},
	"Stock Reconciliation": {"tasks", "warehouse", "orders", "production", "purchase", "dashboard"},
	"Delivery Note": {"warehouse", "orders", "production", "purchase", "dashboard"},
	"Material Request": {"purchase", "warehouse", "production"},
	"Purchase Order": {"purchase", "warehouse", "production"},
	"Purchase Receipt": {"purchase", "warehouse", "production", "orders", "dashboard", "tasks"},
	"Purchase Receipt Event": {"purchase"},
	"Purchase Allocation Batch": {"purchase"},
	"Sales Invoice": {"warehouse", "orders", "production", "purchase", "dashboard", "tasks"},
	"Purchase Invoice": {"warehouse", "orders", "production", "purchase", "dashboard", "tasks"},
	"Repost Item Valuation": {"warehouse", "orders", "production", "purchase", "tasks", "dashboard"},
	"Operation Wage Rate": {"tasks", "wages"},
	"Monthly Worker Wage Summary": {"tasks", "wages"},
	"BOM": {"orders", "production", "purchase"},
	"Item": {"orders", "production", "purchase", "warehouse", "tasks", "dashboard"},
	"Batch": {"orders", "production", "purchase", "warehouse", "tasks", "dashboard"},
	"Serial and Batch Bundle": {"orders", "production", "purchase", "warehouse", "tasks", "dashboard"},
	"Item Group": {"dashboard"},
	"Warehouse": {"orders", "production", "purchase", "warehouse", "tasks", "dashboard"},
	"Company": set(TOPICS) - {"notifications"},
	"Stock Settings": {"orders", "production", "purchase", "warehouse", "tasks", "dashboard"},
	"Manufacturing Settings": {"orders", "production", "purchase", "tasks"},
	"Process Simplification Settings": set(TOPICS),
	"Notification Log": {"notifications"},
	"Notification Settings": {"notifications"},
}
ACCESS_DOCTYPES = {
	"User",
	"User Permission",
	"Role",
	"Role Profile",
	"Custom DocPerm",
	"DocPerm",
	"Employee",
	"DocType",
	"DocShare",
	"Property Setter",
	"Custom Field",
	"Module Profile",
}


def _key(*parts):
	return (
		PREFIX
		+ sha256(json.dumps(parts, ensure_ascii=False, default=str, sort_keys=True).encode()).hexdigest()
	)


def _token(*parts):
	key = _key("version", *parts)
	value = _cache().get_value(key, use_local_cache=False)
	if value is None:
		value = uuid4().hex
		_cache().set_value(key, value)
	return value


def _access(user):
	stamp = _token("access")
	key = _key("access-scope", user, stamp)
	cached = _cache().get_value(key)
	if cached is None:
		companies = user_company_scope(user)
		cached = {
			# An absent explicit boundary must not lose native-role/shared-document
			# invalidations. Hints are content-free; endpoints still filter records.
			"companies": sorted(companies) if companies else None,
			"topics": [
				topic
				for topic, capabilities in TOPICS.items()
				if not capabilities
				or any(user_has_capability(capability, user) for capability in capabilities)
			],
		}
		_cache().set_value(key, cached, expires_in_sec=120)
	return {**cached, "stamp": stamp}


def _scopes(user, topic, access):
	if topic == "notifications":
		return [("all", topic), ("user", user, topic)]
	companies = access["companies"]
	return (
		[("all", topic)]
		+ ([("user", user, topic)] if topic == "tasks" else [])
		+ (
			[("any-company", topic)]
			if companies is None
			else [("company", company, topic) for company in companies]
		)
	)


def versions_for(user, topics, access=None):
	access = access or _access(user)
	return {
		topic: sha256(
			"|".join(
				[access["stamp"], nowdate(), *(_token(*scope) for scope in _scopes(user, topic, access))]
			).encode()
		).hexdigest()
		for topic in topics
		if topic in access["topics"]
	}


@frappe.whitelist(methods=["POST"])
def check_updates(topics=None, known=None):
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("请先登录。", frappe.PermissionError)
	requested = frappe.parse_json(topics) if isinstance(topics, str) else topics
	requested = requested if isinstance(requested, list) else []
	access = _access(user)
	allowed = sorted({topic for topic in requested if isinstance(topic, str) and topic in access["topics"]})
	current = versions_for(user, allowed, access)
	previous = frappe.parse_json(known) if isinstance(known, str) else known
	previous = previous if isinstance(previous, dict) else {}
	# One lease per user, with a union of active tab interests. No browser writes
	# business data; expiry bounds abandoned tabs and disabled accounts.
	watch_key = _key("watcher", user)
	watcher = _cache().get_value(watch_key) or {}
	interests = {key: expiry for key, expiry in watcher.get("interests", {}).items() if expiry > time.time()}
	interests.update({topic: time.time() + 300 for topic in allowed})
	_cache().set_value(watch_key, {"interests": interests, "access": access}, expires_in_sec=300)
	_cache().hset(PREFIX + "watchers", user, time.time() + 300)
	return {
		"versions": current,
		"changed": [topic for topic, value in current.items() if previous.get(topic) != value],
	}


def _worker_users(doc):
	user = doc.get("employee_user")
	employee = doc.get("employee")
	users = {user} if isinstance(user, str) and user else set()
	# Native Job Card.employee is a child table, whereas app reports and
	# assignments use a Link. Work-order resolution covers the native table.
	employees = {employee} if isinstance(employee, str) and employee else set()
	if employees:
		users.update(frappe.get_all("Employee", filters={"name": ["in", list(employees)]}, pluck="user_id"))
	return users - {None, ""}


def document_changed(doc, method=None):
	"""DocType hooks collect changes once per transaction, including deletions."""
	if frappe.flags.in_migrate or frappe.flags.in_install:
		return
	if doc.doctype in ACCESS_DOCTYPES:
		queue_changes({("access",)})
		return
	topics = DOCUMENT_TOPICS.get(doc.doctype)
	if not topics:
		return
	changes = set()
	company = doc.get("company")
	for topic in topics:
		if topic == "notifications" and doc.doctype in {"Notification Log", "Notification Settings"}:
			user = doc.get("for_user") or doc.get("user") or doc.name
			changes.add(("user", user, topic))
		elif topic == "tasks":
			work_order = doc.name if doc.doctype == "Work Order" else doc.get("work_order")
			if work_order:
				changes.add(("work-order-workers", work_order))
			users = _worker_users(doc)
			if users:
				changes.update(("user", user, topic) for user in users)
			elif not work_order and doc.doctype not in {
				"Job Card Worker Assignment",
				"Job Card Work Report",
				"Job Card Assignment Movement",
			}:
				# Stock receipts/master changes may unlock tasks in other work orders.
				changes.update(
					{("company", company, topic), ("any-company", topic)} if company else {("all", topic)}
				)
		elif company:
			changes.update({("company", company, topic), ("any-company", topic)})
		else:
			changes.add(("all", topic))
	queue_changes(changes)


def queue_changes(changes):
	if not getattr(frappe.local, "ps_page_changes", None):
		frappe.local.ps_page_changes = set()
		frappe.db.after_commit.add(flush_changes)
		frappe.db.after_rollback.add(clear_changes)
	frappe.local.ps_page_changes.update(changes)


def clear_changes():
	frappe.local.ps_page_changes = set()


def flush_changes():
	changes = getattr(frappe.local, "ps_page_changes", set())
	clear_changes()
	if not changes:
		return
	frappe.local.ps_delivered_changes = getattr(frappe.local, "ps_delivered_changes", set()) | changes
	try:
		work_orders = [scope[1] for scope in changes if scope[0] == "work-order-workers"]
		changes = {scope for scope in changes if scope[0] != "work-order-workers"}
		if work_orders:
			employees = frappe.get_all(
				"Job Card Worker Assignment",
				filters={"work_order": ["in", work_orders], "status": "Active"},
				pluck="employee",
			)
			if employees:
				users = frappe.get_all(
					"Employee", filters={"name": ["in", sorted(set(employees))]}, pluck="user_id"
				)
				changes.update(("user", user, "tasks") for user in users if user)
		for scope in changes:
			_cache().set_value(_key("version", *scope), uuid4().hex)
		for raw_user, expiry in _cache().hgetall(PREFIX + "watchers").items():
			user = raw_user.decode() if isinstance(raw_user, bytes) else raw_user
			if expiry < time.time():
				_cache().hdel(PREFIX + "watchers", user)
				continue
			watcher = _cache().get_value(_key("watcher", user))
			if not watcher:
				continue
			access = watcher["access"]
			interested = [topic for topic, expiry in watcher["interests"].items() if expiry > time.time()]
			affected = [
				topic
				for topic in interested
				if ("access",) in changes or changes.intersection(_scopes(user, topic, access))
			]
			if affected:
				frappe.publish_realtime(EVENT, {"topics": affected}, user=user)
	except Exception:
		# The transaction has committed. A delivery failure must not pretend the
		# business operation failed; TTL/foreground refresh repairs display state.
		frappe.log_error(title="Page refresh change delivery failed")


def after_request(request, response):
	try:
		_after_request(request, response)
	except Exception:
		frappe.log_error(title="Page refresh RPC catch-up failed")


def _after_request(request, response):
	"""Cover successful RPCs whose native bulk updates bypass Document hooks."""
	if request.method != "POST" or response.status_code >= 400:
		return
	command = str(frappe.form_dict.get("cmd") or request.path.rsplit("/api/method/", 1)[-1])
	if command.startswith("frappe.desk.doctype.notification_") and command.rsplit(".", 1)[-1] in {
		"mark_as_read",
		"mark_all_as_read",
		"set_seen_value",
	}:
		changes = {("user", frappe.session.user, "notifications")}
		frappe.local.ps_page_changes = changes
		flush_changes()
	elif command.startswith("process_simplification.api.production_reporting.") or command.startswith(
		"process_simplification.api.production_exceptions."
	):
		if command.rsplit(".", 1)[-1].startswith(("get_", "search_")):
			return
		for argument, doctype in (
			("report", "Job Card Work Report"),
			("assignment", "Job Card Worker Assignment"),
			("job_card", "Job Card"),
			("request", "Production Exception Request"),
		):
			name = frappe.form_dict.get(argument)
			if name and frappe.db.exists(doctype, name):
				document_changed(frappe.get_doc(doctype, name))
		frappe.local.ps_page_changes = getattr(frappe.local, "ps_page_changes", set()) - getattr(
			frappe.local, "ps_delivered_changes", set()
		)
		flush_changes()


@frappe.whitelist(methods=["POST"])
def get_notifications():
	from frappe.desk.doctype.notification_settings.notification_settings import is_notifications_enabled

	from process_simplification.notifications import (
		APP_NAME,
		process_notification_sound_enabled,
		process_notifications_enabled,
	)

	user = frappe.session.user
	if user == "Guest":
		frappe.throw("请先登录。", frappe.PermissionError)
	if not is_notifications_enabled(user):
		return {
			"enabled": False,
			"notification_logs": [],
			"unread_count": 0,
			"seen": 1,
			"user_info": {},
			"latest_process_notification": None,
			"play_sound": False,
		}
	logs = frappe.get_list(
		"Notification Log",
		filters={"for_user": user},
		fields=["*"],
		order_by="creation desc, name desc",
		limit_page_length=20,
	)
	latest = frappe.get_list(
		"Notification Log",
		filters={"for_user": user, "app": APP_NAME},
		fields=["name", "creation", "read"],
		order_by="creation desc, name desc",
		limit_page_length=1,
	)
	user_info = frappe._dict()
	for sender in {row.from_user for row in logs if row.from_user}:
		frappe.utils.add_user_info(sender, user_info)
	return {
		"enabled": True,
		"notification_logs": logs,
		"user_info": user_info,
		"unread_count": frappe.db.count("Notification Log", {"for_user": user, "read": 0}),
		"seen": frappe.db.get_value("Notification Settings", user, "seen") or 0,
		"latest_process_notification": latest[0] if latest else None,
		"play_sound": process_notifications_enabled() and process_notification_sound_enabled(),
	}


@contextmanager
def display_read_context():
	previous = getattr(frappe.local, "ps_display_read", False)
	frappe.local.ps_display_read = True
	try:
		yield
	finally:
		frappe.local.ps_display_read = previous


def cached_display(topic, callback, arguments, ttl=30):
	"""Cache read endpoints only. All mutation/service calls bypass this cache."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("请先登录。", frappe.PermissionError)
	if frappe.flags.in_test and not frappe.flags.test_page_refresh_cache:
		with display_read_context():
			return callback(**arguments)
	access = _access(user)
	version = versions_for(user, [topic], access)
	if topic not in version:
		# Preserve the original endpoint's permission contract for native roles.
		return callback(**arguments)
	key = _key("display", user, topic, arguments, version)
	value = _cache().get_value(key, use_local_cache=False)
	if value is not None:
		return deepcopy(value)
	# A bounded lock prevents simultaneous tabs from duplicating expensive work.
	lock = _cache().lock(_cache().make_key(_key("compute", user, topic)), timeout=120)
	if not lock.acquire(blocking=False):
		return {"_refresh_pending": True}
	try:
		value = _cache().get_value(key, use_local_cache=False)
		if value is None:
			with display_read_context():
				value = callback(**arguments)
			current_access = _access(user)
			if current_access["stamp"] != access["stamp"]:
				return {"_refresh_pending": True}
			if versions_for(user, [topic], current_access) == version:
				_cache().set_value(key, value, expires_in_sec=ttl)
			else:
				# A changing factory must not leave an initially empty page blank.
				# Show the read snapshot, keep the dirty indicator, and do not cache it.
				value = {**value, "_refresh_stale": True}
		return deepcopy(value)
	finally:
		try:
			lock.release()
		except Exception:
			frappe.log_error(title="Page refresh cache lock expired")


@frappe.whitelist()
def fulfillment_overview(page=1, page_size=20, filters=None):
	from process_simplification.api.workbench import get_fulfillment_overview, paginate_workbench_rows

	frappe.has_permission("Sales Order", "read", throw=True)
	data = cached_display(
		"orders",
		get_fulfillment_overview,
		{
			"page": 1,
			"page_size": 0,
			"filters": frappe.parse_json(filters) if isinstance(filters, str) else filters,
		},
	)
	if data.get("_refresh_pending"):
		return data
	data["orders"], data["pagination"] = paginate_workbench_rows(
		data["orders"], page=page, page_size=page_size
	)
	return data


@frappe.whitelist()
def production_overview(page=1, page_size=20, filters=None):
	from process_simplification.api.production import (
		attach_visible_worker_assignment_counts,
		get_production_overview,
	)
	from process_simplification.api.workbench import paginate_workbench_rows

	frappe.has_permission("Sales Order", "read", throw=True)
	frappe.has_permission("Work Order", "read", throw=True)
	data = cached_display(
		"production",
		get_production_overview,
		{
			"page": 1,
			"page_size": 0,
			"filters": frappe.parse_json(filters) if isinstance(filters, str) else filters,
			"include_assignment_counts": False,
		},
	)
	if data.get("_refresh_pending"):
		return data
	data["demands"], data["pagination"] = paginate_workbench_rows(
		data["demands"], page=page, page_size=page_size
	)
	attach_visible_worker_assignment_counts(data["demands"])
	return data


@frappe.whitelist()
def company_shortages(company=None):
	from process_simplification.api.shortage import check_all_shortages

	frappe.has_permission("Material Request", "read", throw=True)
	return cached_display("purchase", check_all_shortages, {"company": company})


@frappe.whitelist()
def executive_dashboard(company=None, from_date=None, to_date=None):
	from process_simplification.api.executive_dashboard import get_dashboard
	from process_simplification.management_access import require_owner_access

	require_owner_access()
	return cached_display(
		"dashboard", get_dashboard, {"company": company, "from_date": from_date, "to_date": to_date}, ttl=120
	)
