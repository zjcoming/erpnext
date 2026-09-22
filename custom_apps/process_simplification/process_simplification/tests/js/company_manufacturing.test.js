const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

test("semi-finished warehouse picker follows the company and accepts only enabled leaf warehouses", () => {
	let handlers;
	let query;
	const frappe = { ui: { form: { on(doctype, registered) {
		assert.equal(doctype, "Company");
		handlers = registered;
	} } } };
	vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../../public/js/company_manufacturing.js"), "utf8"), { frappe });
	const frm = { doc: { company_name: "Factory A", name: "Factory A" }, set_query(field, callback) {
		assert.equal(field, "custom_default_semi_finished_warehouse");
		query = callback;
	} };
	handlers.setup(frm);
	assert.equal(query().filters.company, "Factory A");
	assert.equal(query().filters.is_group, 0);
	assert.equal(query().filters.disabled, 0);
	frm.doc = { company_name: "Factory B", name: "Factory B" };
	assert.equal(query().filters.company, "Factory B");
});
