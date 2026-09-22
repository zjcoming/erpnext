"""Warehouse master-data maintenance without deleting linked stock or permissions."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.rename_doc import rename_doc


# Native rename saves linked Single doctypes. Stock Settings validation also
# updates Property Setters, which requires System Manager. Do not impersonate
# Administrator or broaden a caller's permissions to make that cascade work.
WAREHOUSE_MANAGEMENT_ROLES = {"System Manager"}


def _warehouse_label(value: str, abbreviation: str) -> str:
	if not isinstance(value, str) or not value.strip():
		frappe.throw(_("请输入新的仓库名称。"))
	label = value.strip()
	if " - " in label:
		label, suffix = label.rsplit(" - ", 1)
		if suffix != abbreviation:
			frappe.throw(_("仓库所属公司不能通过改名更换，请保留当前公司后缀。"))
		label = label.strip()
	if not label or label.startswith("- ") or " - " in label or any(ord(character) < 32 for character in label):
		frappe.throw(_("请输入有效的仓库名称，名称中不要包含公司后缀或换行。"))
	return label


@frappe.whitelist(methods=["POST"])
def rename_warehouse(warehouse: str, new_name: str):
	"""Keep the same warehouse and its links; this endpoint never merges warehouses."""
	if frappe.session.user != "Administrator" and not WAREHOUSE_MANAGEMENT_ROLES.intersection(
		frappe.get_roles()
	):
		frappe.throw(_("只有系统管理员可以修改仓库名称，并且需要该仓库的修改权限。"), frappe.PermissionError)

	doc = frappe.get_doc("Warehouse", warehouse)
	doc.check_permission("write")
	if not doc.company or not doc.parent_warehouse:
		frappe.throw(_("公司根仓库不能通过此入口改名。"))
	company = doc.company
	abbreviation = frappe.get_cached_value("Company", doc.company, "abbr")
	if not abbreviation:
		frappe.throw(_("请先设置仓库所属公司的简称。"))
	label = _warehouse_label(new_name, abbreviation)
	target = f"{label} - {abbreviation}"
	if target == doc.name:
		frappe.throw(_("新名称与当前仓库名称相同。"))
	if frappe.db.exists("Warehouse", target):
		frappe.throw(_("已有同名仓库，请使用其他名称；此操作不会合并仓库。"))

	# Retain atomicity for internal callers as well as Frappe's HTTP transaction.
	savepoint = "warehouse_rename_" + frappe.generate_hash(length=8)
	frappe.db.savepoint(savepoint)
	realtime_before = list(getattr(frappe.local, "_realtime_log", []) or [])
	try:
		frappe.db.get_value("Warehouse", warehouse, "name", for_update=True)
		doc.reload()
		doc.check_permission("write")
		if doc.company != company or not doc.parent_warehouse:
			frappe.throw(_("仓库资料已变更，请刷新后重试。"))
		renamed = rename_doc(
			"Warehouse", warehouse, target, force=True, merge=False,
			ignore_permissions=False, show_alert=False, rebuild_search=False,
		)
		renamed_doc = frappe.get_doc("Warehouse", renamed)
		renamed_doc.warehouse_name = label
		renamed_doc.save()
		frappe.enqueue(
			"frappe.utils.global_search.rebuild_for_doctype",
			doctype="Warehouse", enqueue_after_commit=True,
		)
	except frappe.QueryDeadlockError:
		# InnoDB already rolled back the whole transaction and its savepoints.
		raise
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		if hasattr(frappe.local, "_realtime_log"):
			frappe.local._realtime_log = realtime_before
		frappe.clear_cache()
		raise

	return {"old_name": warehouse, "name": renamed, "warehouse_name": label}
