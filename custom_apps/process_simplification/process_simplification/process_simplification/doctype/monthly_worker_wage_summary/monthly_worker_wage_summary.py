from frappe.model.document import Document


class MonthlyWorkerWageSummary(Document):
	def validate(self):
		from process_simplification.production_reporting.summary import validate_summary_document

		validate_summary_document(self)

	def before_submit(self):
		from process_simplification.production_reporting.summary import before_submit_summary

		before_submit_summary(self)

	def on_submit(self):
		from process_simplification.production_reporting.summary import on_submit_summary

		on_submit_summary(self)

	def before_cancel(self):
		from process_simplification.production_reporting.summary import before_cancel_summary

		before_cancel_summary(self)
