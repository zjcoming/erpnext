"""Read-only release gate: reject completed sites still booting the setup wizard.

Run from the bench directory with its Python runtime and pass the site name.
A newly installed, unconfigured site is allowed to show the native wizard.
"""
import json
import os
import sys

import frappe

site = sys.argv[1]
os.chdir("/home/frappe/frappe-bench/sites")
frappe.init(site=site)
frappe.connect()
frappe.set_user("Administrator")
try:
    from frappe.boot import add_home_page

    boot = frappe._dict()
    add_home_page(boot, [])
    complete = bool(frappe.is_setup_complete())
    default = frappe.db.get_default("desktop:home_page")
    result = dict(site=site, setup_complete=complete, desktop_default=default, boot_home_page=boot.home_page)
    print(json.dumps(result))
    if complete and (default == "setup-wizard" or boot.home_page == "setup-wizard"):
        raise SystemExit("Navigation gate failed: completed site still opens setup wizard")
finally:
    frappe.db.rollback()
    frappe.destroy()
