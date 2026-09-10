"""One transaction recovery for the audited HTTP Sales Order draft entry points.

This is deliberately not a Document.insert decorator: internal callers may own
earlier writes, and submitted orders and custom composite RPCs are not replayable.
The safety policy is tied to the pinned Frappe/ERPNext production code. Unknown
apps, controller overrides or configurable pre-insert effects keep normal errors.
"""

import json
import re
from contextlib import contextmanager
from copy import deepcopy

import frappe

_DESK_METHOD = "frappe.desk.form.save.savedocs"
_PRE_INSERT_EVENTS = {
	"before_insert", "before_naming", "autoname", "before_validate", "validate", "before_save",
}
_AUDITED_VALIDATE_HOOKS = {
	"erpnext.support.doctype.service_level_agreement.service_level_agreement.apply",
	"erpnext.setup.doctype.transaction_deletion_record.transaction_deletion_record.check_for_running_deletion_job",
}
_AUDITED_BEFORE_REQUEST_HOOKS = {
	"frappe.recorder.record", "frappe.monitor.start", "frappe.rate_limiter.apply",
	"frappe.integrations.oauth2.set_cors_for_privileged_requests",
}
_CALLBACKS = ("before_commit", "after_commit", "before_rollback", "after_rollback")
_REST_PATHS = {"/api/resource/Sales Order", "/api/v1/resource/Sales Order"}
_AUDITED_CHILD_TABLES = {
	"Sales Order Item", "Pricing Rule Detail", "Sales Taxes and Charges", "Packed Item",
	"Payment Schedule", "Sales Team", "Item Wise Tax Detail",
}
_MISSING = object()


def create_rest_draft(data):
	"""Called only by the checksum-pinned v1 create_doc hook; keep v1 construction."""
	original = deepcopy(data)
	return _run(
		lambda: frappe.new_doc("Sales Order", **deepcopy(original)).insert(),
		eligible=isinstance(data, dict) and data.get("docstatus", 0) in (0, "0", None),
		entry="rest",
	)


@frappe.whitelist(methods=["POST", "PUT"])
def savedocs(doc: str, action: str):
	"""Retain Desk's temporary names, attachments, onload and response formatting."""
	from frappe.desk.form import save

	original_savedocs = save.savedocs

	try:
		data = json.loads(doc)
	except (TypeError, ValueError):
		return original_savedocs(doc, action)
	eligible = (
		isinstance(data, dict)
		and data.get("doctype") == "Sales Order"
		and data.get("__islocal") == 1
		and data.get("docstatus", 0) in (0, "0", None)
		and action == "Save"
	)
	# Older development frameworks can still use this App without the production
	# patch; leave their native save untouched. The pinned helper emits telemetry
	# once outside the replayed body (telemetry uses Redis, not the transaction).
	if not eligible or not hasattr(save, "_savedocs"):
		return original_savedocs(doc, action)
	first = [frappe.get_doc(data)]
	save.capture_doc(first[0], action)
	return _run(
		lambda: save._savedocs(first.pop() if first else frappe.get_doc(json.loads(doc)), action),
		eligible=True, entry="desk",
	)


def _owns_clean_http_transaction(entry):
	request = getattr(frappe.local, "request", None)
	if not request or request.method not in {"POST", "PUT"}:
		return False
	if entry == "rest":
		if request.method != "POST" or request.path.rstrip("/") not in _REST_PATHS:
			return False
	elif (
		frappe.form_dict.get("cmd") != _DESK_METHOD
		or request.path.rstrip("/") not in {
			"", "/api/method/" + _DESK_METHOD, "/api/v1/method/" + _DESK_METHOD,
		}
	):
		return False
	return not (
		frappe.db.db_type != "mariadb"
		or frappe.db.transaction_writes
		or frappe.db._disable_transaction_control
		or frappe.flags.read_only
		or getattr(frappe.local, "ps_sales_order_creation_active", False)
		or getattr(frappe.local, "ps_page_changes", None)
		or any(getattr(frappe.db, name)._functions for name in _CALLBACKS)
	)


def _audited_configuration():
	from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
	from frappe.model.base_document import get_controller

	if set(frappe.get_installed_apps()) - {"frappe", "erpnext", "process_simplification"}:
		return False
	# Native instrumentation/rate limiting ran once before this endpoint, and
	# remain outside the replayed body. Custom authentication/request work does
	# not have that audited ownership contract.
	if set(frappe.get_hooks("before_request")) - _AUDITED_BEFORE_REQUEST_HOOKS or frappe.get_hooks("auth_hooks"):
		return False
	if get_controller("Sales Order") is not SalesOrder:
		return False
	children = {field.options for field in frappe.get_meta("Sales Order").get_table_fields()}
	if children - _AUDITED_CHILD_TABLES:
		return False
	for hook in ("override_doctype_class", "extend_doctype_class", "has_permission"):
		configured = frappe.get_hooks(hook) or {}
		if not isinstance(configured, dict) or any(configured.get(doctype) for doctype in {"*", "Sales Order", *children}):
			return False
	# Child naming hooks execute before the parent's INSERT too.
	events = frappe.get_hooks("doc_events") or {}
	if not isinstance(events, dict):
		return False
	for scopes, configured in events.items():
		scopes = scopes if isinstance(scopes, tuple) else (scopes,)
		if not {"*", "Sales Order", *children}.intersection(scopes):
			continue
		if not isinstance(configured, dict):
			return False
		for event, handlers in configured.items():
			if event in _PRE_INSERT_EVENTS:
				allowed = _AUDITED_VALIDATE_HOOKS if event == "validate" else set()
				handlers = [handlers] if isinstance(handlers, str) else handlers
				if not isinstance(handlers, (list, tuple)) or any(handler not in allowed for handler in handlers):
					return False
	# Do not trust cached configuration when deciding whether an attempt can be
	# replayed. These guards add three indexed existence reads to eligible saves.
	for doctype, filters in (
		("Server Script", {"disabled": 0}),
		("Webhook", {"enabled": 1}),
		("Notification", {"enabled": 1, "document_type": ["in", ["Sales Order", *sorted(children)]], "event": "Method",
			"method": ["in", sorted(_PRE_INSERT_EVENTS)]}),
	):
		if frappe.db.exists(doctype, filters):
			return False
	return True


@contextmanager
def _observe_transaction_boundary():
	"""Observe only this request's DB instance, never monkeypatch a shared class."""
	database = frappe.db
	previous = database.__dict__.get("execute_query", _MISSING)
	execute = database.execute_query
	state = {"intact": True}

	def observed(query, *args, **kwargs):
		# All other statements, including raw COMMIT/SET/CALL and DDL, make this
		# attempt ineligible. Do not block them or change the original operation.
		text = str(query).lstrip()
		if text.startswith("SET STATEMENT innodb_snapshot_isolation=OFF FOR "):
			text = text.removeprefix("SET STATEMENT innodb_snapshot_isolation=OFF FOR ")
			safe = bool(re.match(r"(?:INSERT INTO|UPDATE) `tabSeries`\s", text))
		else:
			safe = bool(re.match(r"(?:SELECT|INSERT|UPDATE|DELETE)\s", text, re.IGNORECASE))
		if not safe:
			state["intact"] = False
		result = execute(query, *args, **kwargs)
		# Once any Sales Order parent row was inserted successfully, later hooks
		# may have side effects (or insert a second order). Never replay that path.
		if re.match(r"\s*INSERT INTO `tabSales Order`\s*\(", text):
			state["intact"] = False
		return result

	database.execute_query = observed
	try:
		yield state
	finally:
		if previous is _MISSING:
			del database.execute_query
		else:
			database.execute_query = previous


def _parent_insert_snapshot_conflict(exc):
	"""Identify the pinned parent INSERT frame, not a translated error message."""
	from frappe.database.database import Database
	from frappe.model.base_document import BaseDocument

	if not isinstance(exc, frappe.QueryDeadlockError):
		return False
	cause = exc.__cause__ or (exc.args[0] if exc.args and isinstance(exc.args[0], Exception) else None)
	if not cause or not cause.args or cause.args[0] != 1020:
		return False
	parent = sql = False
	traceback = exc.__traceback__
	while traceback:
		frame = traceback.tb_frame
		if frame.f_code is BaseDocument.db_insert.__code__:
			doc = frame.f_locals.get("self")
			parent = doc.doctype == "Sales Order" and doc.docstatus == 0
		if frame.f_code is Database.sql.__code__:
			query = frame.f_locals.get("query", "")
			sql = bool(re.match(r"\s*INSERT INTO `tabSales Order`\s*\(", str(query)))
		traceback = traceback.tb_next
	return parent and sql


def _snapshot_request_state():
	# Identity/session/CSRF/request objects are deliberately retained. Mutable
	# response state and flags revert to their state before this endpoint began.
	result = {}
	for name in ("flags", "message_log", "error_log", "debug_log", "response", "form_dict"):
		value = getattr(frappe.local, name, _MISSING)
		result[name] = _MISSING if value is _MISSING else deepcopy(value)
	return result


def _discard_attempt(baseline):
	frappe.db.rollback()
	# rollback resets transaction callbacks and value_cache, and runs the App's
	# page-change cleanup. Explicitly reset request caches which rollback does not.
	for name in ("cache", "request_cache", "document_cache", "new_doc_templates", "role_permissions"):
		value = getattr(frappe.local, name, None)
		if value is not None:
			value.clear()
	frappe.local.user_perms = None
	frappe.local.ps_page_changes = set()
	for name, value in baseline.items():
		if value is _MISSING:
			if hasattr(frappe.local, name):
				delattr(frappe.local, name)
		else:
			setattr(frappe.local, name, deepcopy(value))


def _run(operation, *, eligible, entry):
	if not eligible or not _owns_clean_http_transaction(entry) or not _audited_configuration():
		return operation()
	# The configuration guard itself is read-only; reject unexpected side effects
	# introduced by a future customization before assuming transaction ownership.
	if not _owns_clean_http_transaction(entry):
		return operation()
	baseline = _snapshot_request_state()
	frappe.local.ps_sales_order_creation_active = True
	try:
		for attempt in range(2):
			try:
				with _observe_transaction_boundary() as boundary:
					return operation()
			except frappe.QueryDeadlockError as exc:
				if attempt or not boundary["intact"] or not _parent_insert_snapshot_conflict(exc):
					raise
				_discard_attempt(baseline)
				if not _audited_configuration():
					raise
				# Authentication has already completed; do not repeat login hooks or
				# reset session state, but honor disablement before the new attempt.
				if not frappe.db.get_value("User", frappe.session.user, "enabled"):
					raise frappe.PermissionError("User is disabled") from exc
	finally:
		frappe.local.ps_sales_order_creation_active = False
