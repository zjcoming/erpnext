from frappe.model.document import Document


class JobCardWorkerAssignment(Document):
	def validate(self):
		from process_simplification.production_reporting.service import validate_assignment_document

		validate_assignment_document(self)

	def on_trash(self):
		from process_simplification.production_reporting.service import validate_assignment_deletion

		validate_assignment_deletion(self)
