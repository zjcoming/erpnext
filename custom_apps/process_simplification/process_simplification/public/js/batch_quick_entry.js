"use strict";

function batchCreationPermissions(doc, permissions) {
	if (doc?.doctype !== "Batch" || !doc.__islocal || !permissions?.[0]?.create) return permissions;
	// This is a display permission for an unsaved creation dialog. Never change
	// cached role permissions or grant write access to an existing Batch.
	return permissions.map((permission, level) => level === 0
		? { ...permission, write: 1 }
		: permission);
}

function installBatchQuickEntry(frappeRef) {
	const forms = frappeRef.ui?.form;
	const Base = forms?.BatchQuickEntryForm || forms?.QuickEntryForm;
	if (!Base || Base.ps_batch_creation_permissions) return;
	class BatchQuickEntryForm extends Base {
		attach_doc_and_docfields(refresh) {
			const permissions = frappeRef.perm.get_perm("Batch", this.doc);
			if (batchCreationPermissions(this.doc, permissions) === permissions) {
				return super.attach_doc_and_docfields(refresh);
			}
			super.attach_doc_and_docfields(false);
			for (const field of this.fields_list) {
				// Native Layout binds controls to the model and replaces their df.
				// Its normal display check only understands read/write, not create.
				// Keep the fix on this dialog's controls and retain native dependency,
				// read-only and permlevel checks plus the native insert/callback flow.
				field.df = { ...field.df };
				if (!field.df.get_status) {
					field.df.get_status = (control) => frappeRef.perm.get_field_display_status(
						control.df, this.doc,
						batchCreationPermissions(this.doc, frappeRef.perm.get_perm("Batch", this.doc))
					);
				}
				if (refresh) field.refresh?.();
			}
		}
	}
	BatchQuickEntryForm.ps_batch_creation_permissions = true;
	forms.BatchQuickEntryForm = BatchQuickEntryForm;
}

if (typeof module !== "undefined" && module.exports) {
	module.exports = { batchCreationPermissions, installBatchQuickEntry };
}
if (typeof frappe !== "undefined") {
	if (frappe.ui?.form?.QuickEntryForm) installBatchQuickEntry(frappe);
	else frappe.require("form.bundle.js").then(() => installBatchQuickEntry(frappe));
}
