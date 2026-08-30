const test = require("node:test");
const assert = require("node:assert/strict");

const {
	psExecutiveChangeMeta,
	psExecutiveFormatCurrency,
	psExecutiveInventoryChartData,
	psExecutiveEscape,
} = require("../../process_simplification/page/executive_dashboard/executive_dashboard.js");

test("executive comparison labels positive and negative changes", () => {
	assert.deepEqual(psExecutiveChangeMeta(12.34), {
		label: "↑ 12.3% 较上一同期",
		tone: "positive",
	});
	assert.equal(psExecutiveChangeMeta(-4).tone, "negative");
	assert.equal(psExecutiveChangeMeta(null).tone, "muted");
});

test("inventory chart omits zero-value categories and keeps configured colors", () => {
	assert.deepEqual(
		psExecutiveInventoryChartData([
			{ label: "成品", stock_value: 100, color: "#1" },
			{ label: "半成品", stock_value: 0, color: "#2" },
		]),
		{ labels: ["成品"], values: [100], colors: ["#1"] }
	);
});

test("executive dashboard escapes server text", () => {
	assert.equal(psExecutiveEscape('<script>"x"</script>'), "&lt;script&gt;&quot;x&quot;&lt;/script&gt;");
});

test("currency formatting returns plain text instead of Frappe HTML markup", () => {
	const formatted = psExecutiveFormatCurrency(20703, "CNY");
	assert.match(formatted, /^CNY 20,703\.00$/);
	assert.doesNotMatch(formatted, /<[^>]+>/);
});
