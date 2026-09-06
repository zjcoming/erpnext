"""Retry database conflicts only at the owning Desk RPC boundary."""

from functools import wraps

import frappe
from frappe import _


def retry_request_transaction(function):
	"""Rebuild a failed request from fresh data without committing for its caller.

	Internal calls may have unrelated, uncommitted work. They must propagate the
	conflict to their transaction owner rather than rolling that work back here.
	"""
	command = f"{function.__module__}.{function.__name__}"

	@wraps(function)
	def wrapped(*args, **kwargs):
		request = getattr(frappe.local, "request", None)
		form = getattr(frappe.local, "form_dict", None) or {}
		if request is None or form.get("cmd") != command:
			return function(*args, **kwargs)

		messages = list(getattr(frappe.local, "message_log", None) or [])
		for attempt in range(3):
			try:
				return function(*args, **kwargs)
			except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
				# A deadlock may invalidate savepoints. Roll back the whole RPC and
				# recalculate permissions, stock and priority; never reuse its draft.
				frappe.db.rollback()
				frappe.local.message_log = list(messages)
				cache = getattr(frappe.local, "document_cache", None)
				if cache is not None:
					cache.clear()
				if attempt == 2:
					frappe.throw(_("数据正在被其他操作更新，本次操作未保存，请稍后刷新重试。"))

	return wrapped
