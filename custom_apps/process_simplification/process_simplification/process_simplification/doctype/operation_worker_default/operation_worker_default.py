from frappe.model.document import Document


class OperationWorkerDefault(Document):
	def validate(self):
		from process_simplification.production_reporting.prearrangement import validate_document

		validate_document(self)

	def on_trash(self):
		from process_simplification.production_reporting.prearrangement import prohibit_delete

		prohibit_delete(self)
