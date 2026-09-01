from frappe.model.document import Document


class JobCardAssignmentMovement(Document):
	def validate(self):
		from process_simplification.production_reporting.assignment_movement import (
			validate_movement_document,
		)

		validate_movement_document(self)

	def on_trash(self):
		from process_simplification.production_reporting.assignment_movement import (
			prevent_movement_delete,
		)

		prevent_movement_delete(self)
