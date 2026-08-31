const test = require("node:test");
const assert = require("node:assert/strict");

const {
	psExecutiveChangeMeta,
	psExecutiveFormatCurrency,
	psExecutiveInventoryChartData,
	psExecutiveEscape,
	psExecutiveShouldReloadCompany,
	psExecutiveDestroyCharts,
	psExecutiveChartOptions,
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

test("company initialization does not trigger a duplicate dashboard reload", () => {
	assert.equal(psExecutiveShouldReloadCompany(true, "公司 A", "公司 B"), false);
	assert.equal(psExecutiveShouldReloadCompany(false, "公司 A", "公司 A"), false);
	assert.equal(psExecutiveShouldReloadCompany(false, "公司 A", "公司 B"), true);
});

test("dashboard unload disconnects chart observers before the page DOM is removed", () => {
	const destroyed = [];
	const redrawn = [];
	const charts = [
		{ draw: () => redrawn.push("orders"), destroy: () => destroyed.push("orders") },
		{ draw: () => redrawn.push("inventory"), destroy: () => destroyed.push("inventory") },
	];
	const remaining = psExecutiveDestroyCharts(charts);
	charts.forEach((chart) => chart.draw());

	assert.deepEqual(destroyed, ["orders", "inventory"]);
	assert.deepEqual(redrawn, []);
	assert.deepEqual(remaining, []);
});

test("dashboard charts disable the SVG entry animation that races resize redraws", () => {
	assert.deepEqual(psExecutiveChartOptions({ type: "bar", animate: true }), {
		type: "bar",
		animate: false,
		disableEntryAnimation: true,
	});
});
