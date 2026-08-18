import frappe
from frappe import _
from frappe.model.document import Document


class JobCardWorkReport(Document):
	def validate(self):
		from process_simplification.production_reporting.service import validate_report_document

		validate_report_document(self)

	def on_trash(self):
		frappe.throw(_("Work reports are append-only audit records and cannot be deleted."))
