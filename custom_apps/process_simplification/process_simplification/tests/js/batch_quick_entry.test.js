const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { batchCreationPermissions, installBatchQuickEntry } = require("../../public/js/batch_quick_entry.js");

function fixture({ local = true, create = true } = {}) {
	const permissions = [{ create: Number(create), select: 1, read: 0, write: 0 }];
	const doc = { doctype: "Batch", name: local ? "new-batch-1" : "EXISTING", __islocal: Number(local), docstatus: 0 };
	const frappe = { perm: {}, provide() {}, ui: { form: {} } };
	// Reproduce the installed framework's permission decision, rather than
	// approximating it with a test implementation of the proposed workaround.
	const nativePermPath = path.resolve(__dirname,
		"../../../../../development/frappe-bench/apps/frappe/frappe/public/js/frappe/model/perm.js");
	vm.runInNewContext(fs.readFileSync(nativePermPath, "utf8"), {
		frappe, window: {}, $: { extend: Object.assign }, cint: (value) => Number(value) || 0, cur_frm: null,
	});
	frappe.perm.get_perm = () => permissions;
	const metadata = [
		{ fieldname: "batch_id", fieldtype: "Data", reqd: 1, depends_on: "eval:doc.__islocal" },
		{ fieldname: "item", fieldtype: "Link", reqd: 1, read_only_depends_on: "eval:!doc.__islocal" },
	];
	class QuickEntryForm {
		constructor() {
			this.doc = doc;
			this.fields_list = metadata.map((df) => ({ df, refresh() {
				this.status = this.df.get_status?.(this) || frappe.perm.get_field_display_status(this.df, doc, permissions);
			} }));
		}
		attach_doc_and_docfields(refresh) {
			this.fields_list.forEach((field, index) => {
				field.df = metadata[index];
				if (refresh) field.refresh();
			});
		}
	}
	frappe.ui.form.QuickEntryForm = QuickEntryForm;
	installBatchQuickEntry(frappe);
	return { frappe, permissions, doc, metadata, QuickEntryForm, Entry: frappe.ui.form.BatchQuickEntryForm };
}

test("create-only warehouse user can enter the required Batch ID without cached write permission", () => {
	const { frappe, permissions, doc, metadata, Entry } = fixture();
	assert.equal(frappe.perm.get_field_display_status(metadata[0], doc, permissions), "None");
	const entry = new Entry();
	entry.attach_doc_and_docfields(true);
	assert.deepEqual(entry.fields_list.map((field) => field.status), ["Write", "Write"]);
	assert.equal(entry.fields_list[0].df.reqd, 1);
	assert.equal(entry.fields_list[0].df.depends_on, "eval:doc.__islocal");
	assert.equal(metadata[0].get_status, undefined);
	assert.equal(permissions[0].write, 0);
	assert.equal(permissions[0].read, 0);
});

test("existing batches and users without create keep native field permissions", () => {
	for (const options of [{ local: false }, { create: false }]) {
		const { Entry, doc, permissions } = fixture(options);
		assert.equal(batchCreationPermissions(doc, permissions), permissions);
		const entry = new Entry();
		entry.attach_doc_and_docfields(true);
		assert.deepEqual(entry.fields_list.map((field) => field.status), ["None", "None"]);
	}
});

test("native read-only, hidden and higher permlevel restrictions still apply", () => {
	const { Entry } = fixture();
	const entry = new Entry();
	entry.attach_doc_and_docfields(true);
	const field = entry.fields_list[0];
	field.df.read_only = 1;
	assert.equal(field.df.get_status(field), "Read");
	field.df.hidden_due_to_dependency = 1;
	assert.equal(field.df.get_status(field), "None");
	field.df.hidden_due_to_dependency = field.df.read_only = 0;
	field.df.permlevel = 1;
	assert.equal(field.df.get_status(field), "None");
});

test("other quick-entry controllers and repeat installation remain unchanged", () => {
	const { frappe, permissions, QuickEntryForm, Entry } = fixture();
	assert.equal(batchCreationPermissions({ doctype: "Item", __islocal: 1 }, permissions), permissions);
	assert.equal(frappe.ui.form.QuickEntryForm, QuickEntryForm);
	const native = new QuickEntryForm();
	native.attach_doc_and_docfields(true);
	assert.deepEqual(native.fields_list.map((field) => field.status), ["None", "None"]);
	installBatchQuickEntry(frappe);
	assert.equal(frappe.ui.form.BatchQuickEntryForm, Entry);
});
