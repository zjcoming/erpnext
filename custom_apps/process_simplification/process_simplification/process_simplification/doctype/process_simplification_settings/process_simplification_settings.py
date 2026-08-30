from frappe.model.document import Document


class ProcessSimplificationSettings(Document):
	def validate(self):
		from process_simplification.notifications import validate_notification_routes

		validate_notification_routes(self)
