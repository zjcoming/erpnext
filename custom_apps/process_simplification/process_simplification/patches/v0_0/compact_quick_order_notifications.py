import re

import frappe
from process_simplification.notifications import APP_NAME, quick_order_shortage_description


def execute():
	"""Shorten the old generated alerts without resending or changing read state."""
	start = changed = 0
	while logs := frappe.get_all(
		"Notification Log",
		filters={
			"app": APP_NAME,
			"document_type": "Sales Order",
			"subject": ["like", "销售订单有缺料待采购：%"],
		},
		fields=["name", "description"],
		order_by="name",
		offset=start,
		limit=500,
	):
		for log in logs:
			match = re.search(r"本单缺料 (\d+) 项；影响后续订单 (\d+) 张。以下仅列本次新增采购缺口", log.description or "")
			if not match:
				continue
			description = quick_order_shortage_description(int(match[1]), int(match[2]))
			frappe.db.set_value("Notification Log", log.name, "description", description, update_modified=False)
			changed += 1
		start += len(logs)
	return changed
