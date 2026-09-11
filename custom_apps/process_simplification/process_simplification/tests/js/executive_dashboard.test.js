const test = require("node:test");
const assert = require("node:assert/strict");

const {
	psExecutiveChangeMeta,
	psExecutiveFormatAmount,
	psExecutiveFormatCurrency,
	psExecutiveFormatInteger,
	psExecutiveEscape,
	psExecutiveDestroyCharts,
	psExecutiveChartOptions,
	psExecutiveCompactAmount,
	psExecutivePeriod,
	psExecutivePeriodPreset,
	psExecutiveProgress,
	psExecutiveOverdueDays,
	ProcessSimplificationExecutiveDashboard,
} = require("../../process_simplification/page/executive_dashboard/executive_dashboard.js");

test("executive comparison labels positive and negative changes", () => {
	assert.deepEqual(psExecutiveChangeMeta(12.34), {
		label: "↑ 12.3% 较上期",
		tone: "positive",
	});
	assert.equal(psExecutiveChangeMeta(-4).tone, "negative");
	assert.equal(psExecutiveChangeMeta(null).tone, "muted");
});

test("executive dashboard escapes server text", () => {
	assert.equal(psExecutiveEscape('<script>"x"</script>'), "&lt;script&gt;&quot;x&quot;&lt;/script&gt;");
});

test("currency formatting returns plain text instead of Frappe HTML markup", () => {
	const formatted = psExecutiveFormatCurrency(20703, "CNY");
	assert.match(formatted, /^CNY 20,703\.00$/);
	assert.doesNotMatch(formatted, /<[^>]+>/);
});

test("headline order amount omits the currency code while keeping exact decimals", () => {
	assert.equal(psExecutiveFormatAmount(999900), "999,900.00");
	assert.equal(psExecutiveFormatAmount(Number.NaN), "0.00");
});

test("headline counts stay plain text and left aligned by the card", () => {
	assert.equal(psExecutiveFormatInteger(1200), "1,200");
	assert.equal(psExecutiveFormatInteger(Number.NaN), "0");
	assert.doesNotMatch(psExecutiveFormatInteger(1), /<[^>]+>/);
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

test("compact money handles units, negatives and invalid values without losing exact amount", () => {
	assert.deepEqual(psExecutiveCompactAmount(1249700), { value: "124.97", unit: "万" });
	assert.deepEqual(psExecutiveCompactAmount(9999.99), { value: "9,999.99", unit: "" });
	assert.deepEqual(psExecutiveCompactAmount(-200000000), { value: "-2", unit: "亿" });
	assert.deepEqual(psExecutiveCompactAmount(Infinity), { value: "0", unit: "" });
	global.__ = (text) => text;
	const dashboard = Object.create(ProcessSimplificationExecutiveDashboard.prototype);
	dashboard.data = { currency: "CNY" };
	const html = dashboard.metric({ label: "订单额", amount: 1249700 });
	assert.match(html, />124\.97<\/strong>/);
	assert.match(html, /class="ps-exec-exact">1,249,700\.00 元<\/div>/);
	delete global.__;
});

test("quick periods cross years and leap months correctly and detect custom ranges", () => {
	assert.deepEqual(psExecutivePeriod("last_month", "2026-01-11"), { from_date: "2025-12-01", to_date: "2025-12-31" });
	assert.deepEqual(psExecutivePeriod("last_month", "2024-03-11"), { from_date: "2024-02-01", to_date: "2024-02-29" });
	assert.deepEqual(psExecutivePeriod("month", "2026-09-11"), { from_date: "2026-09-01", to_date: "2026-09-11" });
	assert.deepEqual(psExecutivePeriod("year", "2026-09-11"), { from_date: "2026-01-01", to_date: "2026-09-11" });
	assert.equal(psExecutivePeriodPreset({ from_date: "2026-08-01", to_date: "2026-08-31" }, "2026-09-11"), "last_month");
	assert.equal(psExecutivePeriodPreset({ from_date: "2026-09-03", to_date: "2026-09-10" }, "2026-09-11"), "custom");
});

test("delivery progress and overdue days have safe bounds", () => {
	assert.equal(psExecutiveProgress(-5), 0);
	assert.equal(psExecutiveProgress(120), 100);
	assert.equal(psExecutiveProgress("37.5"), 37.5);
	assert.equal(psExecutiveProgress("invalid"), 0);
	assert.equal(psExecutiveOverdueDays("2026-09-02", "2026-09-11 19:19:47"), 9);
	assert.equal(psExecutiveOverdueDays("2026-09-12", "2026-09-11"), 0);
	assert.equal(psExecutiveOverdueDays(null, "2026-09-11"), 0);
});

function dashboardHarness() {
	const outputs = {};
	const root = {
		attr() { return this; },
		find(selector) {
			return {
				prop() { return this; }, addClass() { return this; }, removeClass() { return this; },
				text(value) { outputs[selector] = value; return this; },
				html(value) { outputs[selector] = value; return this; },
			};
		},
	};
	const dashboard = Object.create(ProcessSimplificationExecutiveDashboard.prototype);
	dashboard.root = root;
	dashboard.filters = { company: "公司 A", from_date: "2026-09-01", to_date: "2026-09-11" };
	dashboard.data = { company: "公司 A" };
	dashboard.render = () => {};
	return { dashboard, outputs };
}

test("failed filter requests keep the displayed company and period and permit a retry", async () => {
	global.__ = (text) => text;
	const { dashboard, outputs } = dashboardHarness();
	const original = { ...dashboard.filters };
	global.frappe = { ps_read_page: async () => { throw new Error("network down"); } };
	try {
		assert.equal(await dashboard.load({ filters: { company: "公司 B" } }), false);
		assert.deepEqual(dashboard.filters, original);
		assert.equal(dashboard.data.company, "公司 A");
		assert.equal(dashboard.loading, false);
		assert.match(outputs["[data-error]"], /仍显示上次成功的数据/);
		global.frappe.ps_read_page = async (_page, options) => {
			assert.equal(options.args.company, "公司 B");
			options.apply({ message: { company: "公司 B", period: { from_date: "2026-08-01", to_date: "2026-08-31" } } });
			return true;
		};
		assert.equal(await dashboard.load({ filters: { company: "公司 B" } }), true);
		assert.deepEqual(dashboard.filters, { company: "公司 B", from_date: "2026-08-01", to_date: "2026-08-31" });
	} finally { delete global.frappe; delete global.__; }
});

test("only one dashboard read is active and background failures propagate", async () => {
	global.__ = (text) => text;
	const { dashboard } = dashboardHarness();
	let resolveRead;
	let reads = 0;
	global.frappe = { ps_read_page: () => { reads++; return new Promise((resolve) => { resolveRead = resolve; }); } };
	try {
		const pending = dashboard.load();
		assert.equal(await dashboard.load(), false);
		assert.equal(reads, 1);
		resolveRead(true);
		await pending;
		global.frappe.ps_read_page = async () => { throw new Error("offline"); };
		await assert.rejects(dashboard.load({ background: true }), /offline/);
		assert.equal(dashboard.loading, false);
	} finally { delete global.frappe; delete global.__; }
});

test("inventory includes other and zero balances, escapes labels and avoids negative shares", () => {
	global.__ = (text) => text;
	const { dashboard, outputs } = dashboardHarness();
	dashboard.data = { currency: "CNY", inventory: { total_stock_value: 100, categories: [
		{ key: "other", label: "<其他>", stock_value: 100, item_count: 1, color: "#64748b" },
		{ key: "semi_finished", label: "半成品", stock_value: 0, item_count: 0, color: "#7c3aed" },
	] } };
	try {
		dashboard.render_inventory();
		assert.match(outputs["[data-inventory-list]"], /&lt;其他&gt;/);
		assert.match(outputs["[data-inventory-list]"], /半成品/);
		assert.match(outputs["[data-inventory-list]"], /100\.00 元/);
		dashboard.data.inventory.categories[0].stock_value = -100;
		dashboard.render_inventory();
		assert.match(outputs["[data-inventory-list]"], /-100\.00 元/);
		assert.match(outputs["[data-inventory-list]"], /不计算占比/);
		assert.doesNotMatch(outputs["[data-inventory-list]"], /%/);
	} finally { delete global.__; }
});

test("overdue rows distinguish quantity progress from money and safely link to an order", () => {
	global.__ = (text) => text;
	const { dashboard, outputs } = dashboardHarness();
	dashboard.data = {
		currency: "CNY", checked_at: "2026-09-11 19:19:47",
		order_health: { overdue_orders: 2 },
		overdue_orders: [
			{ name: "SO/001", customer_name: "<客户>", delivery_date: "2026-09-02", per_delivered: 37.5, pending_amount: 900 },
			{ name: "SO-002", customer_name: "客户 B", delivery_date: "2026-09-05", per_delivered: 0, pending_amount: 100 },
		],
	};
	try {
		dashboard.render_overdue_orders();
		assert.match(outputs["[data-overdue-list]"], /sales_order=SO%2F001/);
		assert.match(outputs["[data-overdue-list]"], /&lt;客户&gt;/);
		assert.match(outputs["[data-overdue-list]"], /已交付 37\.5%/);
		assert.match(outputs["[data-overdue-list]"], /尚未交付/);
		assert.match(outputs["[data-overdue-list]"], /逾期 9 天/);
		assert.match(outputs["[data-overdue-list]"], /900\.00 元/);
		dashboard.data.overdue_orders = [];
		dashboard.render_overdue_orders();
		assert.match(outputs["[data-overdue-list]"], /当前没有逾期未交付订单/);
	} finally { delete global.__; }
});
