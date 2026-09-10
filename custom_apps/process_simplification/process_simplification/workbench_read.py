"""Reuse immutable query snapshots during one workbench calculation only."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
from inspect import signature

import frappe


_read_cache = ContextVar("ps_workbench_read_cache", default=None)


def workbench_read_snapshots(namespace):
	"""Return calculation-local storage; callers copy rows before returning them.

	There is deliberately no storage outside a read calculation. Site and user
	remain part of the key even when a nested caller switches identity.
	"""
	cache = _read_cache.get()
	if cache is None:
		return None
	session = getattr(frappe.local, "session", None)
	key = (getattr(frappe.local, "site", None), getattr(session, "user", None), namespace)
	return cache.setdefault(key, {})


@contextmanager
def workbench_read_context():
	"""Nested read calculations share a scope; its owner always discards it.

	Keep this inside transaction retry wrappers, never around an entire request
	that may mutate data or retry. The next calculation must read fresh values.
	"""
	if _read_cache.get() is not None:
		yield
		return

	token = _read_cache.set({})
	try:
		yield
	finally:
		_read_cache.reset(token)


def workbench_read(function):
	"""Limit query reuse to a read endpoint's calculation, including nested reads."""
	@wraps(function)
	def wrapped(*args, **kwargs):
		with workbench_read_context():
			return function(*args, **kwargs)

	return wrapped


def reuse_workbench_read(function):
	"""Memoize a small query with scalar arguments only inside a workbench read.

	No scope means no caching, including direct calls from mutation services.
	Copies on both insertion and return keep caller-owned rows independent.
	"""
	parameters = signature(function)

	@wraps(function)
	def wrapped(*args, **kwargs):
		cache = _read_cache.get()
		if cache is None:
			return function(*args, **kwargs)

		bound = parameters.bind(*args, **kwargs)
		bound.apply_defaults()
		session = getattr(frappe.local, "session", None)
		key = (
			getattr(frappe.local, "site", None),
			getattr(session, "user", None),
			function,
			tuple(bound.arguments.items()),
		)
		if key not in cache:
			cache[key] = deepcopy(function(*args, **kwargs))
		return deepcopy(cache[key])

	return wrapped
