from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class ProductionExceptionRequest(Document):
	def validate(self):
		from process_simplification.production_exceptions.service import validate_request_document

		validate_request_document(self)

	def on_trash(self):
		frappe.throw(_("Production exception requests are append-only audit records and cannot be deleted."))
