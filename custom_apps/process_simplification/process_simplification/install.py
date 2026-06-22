from __future__ import annotations

import frappe

from process_simplification.defaults import configure_company_manufacturing_defaults


def set_default_language(language: str = "zh"):
	frappe.db.set_single_value("System Settings", "language", language)

	for user in ("Administrator", "Guest"):
		if frappe.db.exists("User", user):
			frappe.db.set_value("User", user, "language", language, update_modified=False)

	frappe.clear_cache()


def after_install():
	set_default_language()
	configure_company_manufacturing_defaults()
