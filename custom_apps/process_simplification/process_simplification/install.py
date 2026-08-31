from __future__ import annotations

import frappe

from process_simplification.defaults import configure_company_manufacturing_defaults
from process_simplification.management_access import (
	ensure_management_access,
	migrate_legacy_production_supervisor_roles,
	migrate_management_users_to_role_profiles,
	retire_legacy_management_roles,
)
from process_simplification.patches.v0_0.add_management_navigation import (
	execute as add_management_navigation,
)
from process_simplification.patches.v0_0.add_worker_reporting_navigation import (
	execute as add_worker_reporting_navigation,
)
from process_simplification.patches.v0_0.group_process_simplification_navigation import (
	execute as group_process_simplification_navigation,
)
from process_simplification.production_reporting.setup import setup_worker_reporting


def set_default_language(language: str = "zh"):
	frappe.db.set_single_value("System Settings", "language", language)

	for user in ("Administrator", "Guest"):
		if frappe.db.exists("User", user):
			frappe.db.set_value("User", user, "language", language, update_modified=False)

	frappe.clear_cache()


def after_install():
	set_default_language()
	configure_company_manufacturing_defaults()
	setup_worker_reporting()
	ensure_management_access()
	migrate_legacy_production_supervisor_roles()
	migrate_management_users_to_role_profiles()
	retire_legacy_management_roles()
	add_worker_reporting_navigation()
	add_management_navigation()
	group_process_simplification_navigation()


def after_migrate():
	setup_worker_reporting()
	ensure_management_access()
	migrate_legacy_production_supervisor_roles()
	migrate_management_users_to_role_profiles()
	retire_legacy_management_roles()
	add_worker_reporting_navigation()
	add_management_navigation()
	group_process_simplification_navigation()
