import frappe
from frappe.utils import escape_html

from process_simplification.notifications import APP_NAME
from process_simplification.purchasing.receipts import EVENT, _notification_message


def execute():
	"""Reformat existing receipt alerts without resending or changing their read state."""
	start = 0
	changed = 0
	while logs := frappe.get_all(
		"Notification Log",
		filters={"app": APP_NAME, "document_type": EVENT, "name": ["like", "RCNL-%"]},
		fields=["name", "document_name", "subject", "title", "description", "link"],
		order_by="name",
		offset=start,
		limit=500,
	):
		events = {}
		for log in logs:
			if log.document_name not in events:
				events[log.document_name] = (
					frappe.get_doc(EVENT, log.document_name)
					if frappe.db.exists(EVENT, log.document_name) else None
				)
			event = events[log.document_name]
			if not event:
				continue
			subject, description = _notification_message(event)
			values = dict(
				subject=escape_html(subject), title=escape_html(subject), description=description,
				link=f"/desk/purchase-receipt-notice?name={event.name}",
			)
			if any(log.get(key) != value for key, value in values.items()):
				frappe.db.set_value("Notification Log", log.name, values, update_modified=False)
				changed += 1
		start += len(logs)
	return changed
