from frappe.model.document import Document


class MaterialHandlingRequest(Document):
	def validate(self):
		from process_simplification.production_exceptions.handling import validate_document

		validate_document(self)

	def on_trash(self):
		import frappe

		frappe.throw("物料处理单是审计记录，不能删除，请撤回未过账的申请。")


def on_doctype_update():
	import frappe

	frappe.db.add_index("Material Handling Request", ["source_stock_entry", "status"], "mhr_source_status")
	frappe.db.add_index("Material Handling Request", ["work_order", "status"], "mhr_work_order_status")
