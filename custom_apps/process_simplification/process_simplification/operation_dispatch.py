"""Expose production views according to existing roles and explicit site overrides."""

from __future__ import annotations

import frappe

from process_simplification.management_access import (
	CAPABILITY_PRODUCTION_REVIEW,
	CAPABILITY_WAREHOUSE_WORKBENCH,
	user_has_capability,
)


def boot_session(bootinfo):
	from process_simplification.production_reporting.dispatch_pool import is_enabled

	active = is_enabled() and frappe.session.user not in (None, "Guest")
	can_review = bool(active and user_has_capability(CAPABILITY_PRODUCTION_REVIEW))
	bootinfo["enable_operation_dispatch_pool"] = can_review
	bootinfo["enable_production_materials"] = bool(
		active and (can_review or user_has_capability(CAPABILITY_WAREHOUSE_WORKBENCH))
	)
