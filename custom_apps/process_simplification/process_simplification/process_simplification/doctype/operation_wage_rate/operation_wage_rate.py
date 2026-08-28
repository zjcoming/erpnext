from frappe.model.document import Document


class OperationWageRate(Document):
	def validate(self):
		from process_simplification.production_reporting.domain import validate_wage_rate

		validate_wage_rate(self)

	def on_trash(self):
		from process_simplification.production_reporting.domain import validate_wage_rate_delete

		validate_wage_rate_delete(self)
