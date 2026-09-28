const test = require("node:test");
const assert = require("node:assert/strict");
const { classifyMobileReportFilters, reportFilterHasValue } = require("../../public/js/mobile_desk.js");

function field(fieldname, value = "", options = {}) {
	return {
		df: { fieldname, fieldtype: "Data", ...options },
		wrapper: { style: {} },
		get_value: () => value,
	};
}

test("collapsed reports keep required filters and all active optional filters visible", () => {
	const fields = [
		field("company", "", { reqd: 1 }),
		field("from_date", "2026-09-01", { reqd: 1 }),
		field("to_date", "2026-09-28", { reqd: 1 }),
		field("warehouse", [] , { fieldtype: "MultiSelectList" }),
		field("item_code", ["RM-01"], { fieldtype: "MultiSelectList" }),
		field("batch_no"),
		field("include_cancelled", 1, { fieldtype: "Check" }),
	];
	assert.deepEqual(classifyMobileReportFilters(fields).filter((item) => item.extra).map(({ filter }) => filter.df.fieldname), ["warehouse", "batch_no"]);
	assert.equal(classifyMobileReportFilters(fields)[0].wide, true);
});

test("check defaults are empty while zero-valued numeric filters remain active", () => {
	assert.equal(reportFilterHasValue(field("check", "0", { fieldtype: "Check" })), false);
	assert.equal(reportFilterHasValue(field("check", 0, { fieldtype: "Check" })), false);
	assert.equal(reportFilterHasValue(field("quantity", 0, { fieldtype: "Float" })), true);
	for (const value of [null, undefined, "", "   ", []]) {
		assert.equal(reportFilterHasValue(field("empty", value)), false);
	}
});

test("reports without required fields retain their first three available filters", () => {
	const hidden = field("internal", "", { hidden: 1 });
	const dependent = field("dependent");
	dependent.wrapper.style.display = "none";
	const fields = [hidden, dependent, ...["one", "two", "three", "four"].map((name) => field(name))];
	assert.deepEqual(classifyMobileReportFilters(fields).filter((item) => item.extra).map(({ filter }) => filter.df.fieldname), ["four"]);
});

test("clearing an optional filter makes it collapsible again without changing its value", () => {
	let value = "WAREHOUSE-01";
	const warehouse = field("warehouse");
	warehouse.get_value = () => value;
	const fields = [field("company", "", { reqd: 1 }), warehouse];
	assert.equal(classifyMobileReportFilters(fields)[1].extra, false);
	value = "";
	assert.equal(classifyMobileReportFilters(fields)[1].extra, true);
	assert.equal(value, "");
});
