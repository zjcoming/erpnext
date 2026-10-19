from frappe.model.document import Document


class WorkerWageAdjustment(Document):
	def validate(self):
		from process_simplification.production_reporting.wage_adjustments import validate_adjustment_document

		validate_adjustment_document(self)

	def before_submit(self):
		from process_simplification.production_reporting.wage_adjustments import before_submit_adjustment

		before_submit_adjustment(self)

	def before_cancel(self):
		from process_simplification.production_reporting.wage_adjustments import before_cancel_adjustment

		before_cancel_adjustment(self)
