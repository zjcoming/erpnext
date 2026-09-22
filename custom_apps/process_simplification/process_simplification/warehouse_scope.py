"""Refresh inherited warehouse permissions when the warehouse tree changes."""

from functools import partial

import frappe


def warehouse_scope_changed(doc, method=None, *args):
	if method == "on_update":
		previous = doc.get_doc_before_save()
		if previous and all(
			previous.get(field) == doc.get(field)
			for field in ("parent_warehouse", "company", "is_group", "disabled")
		):
			return

	# Tree edits are infrequent. Include all Warehouse-scoped users so moves,
	# deletions and renames also revoke cached access from the old branch.
	users = tuple(sorted(set(frappe.get_all(
		"User Permission", filters={"allow": "Warehouse"}, pluck="user", limit=0,
	))))
	if not users:
		return
	clear = partial(_clear_scope_cache, users)
	clear()
	# Other requests may refill Redis before this transaction commits. Also clear
	# on rollback, in case this request cached descendants that were never saved.
	frappe.db.after_commit.add(partial(_clear_scope_cache, users, notify=True))
	frappe.db.after_rollback.add(clear)


def _clear_scope_cache(users, notify=False):
	for user in users:
		frappe.cache.hdel_names(("user_permissions", "bootinfo", "user_perm_can_read"), user)
		if notify:
			frappe.publish_realtime("update_user_permissions", user=user)
