from __future__ import annotations

import frappe

from process_simplification.defaults import configure_company_manufacturing_defaults
from process_simplification.patches.v0_0.add_worker_reporting_navigation import (
	execute as add_worker_reporting_navigation,
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
	add_worker_reporting_navigation()


def after_migrate():
	setup_worker_reporting()
	add_worker_reporting_navigation()
