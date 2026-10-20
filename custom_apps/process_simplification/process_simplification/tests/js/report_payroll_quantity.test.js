const test = require("node:test");
const assert = require("node:assert/strict");
const review = require("../../process_simplification/page/production_report_review/production_report_review.js");
const history = require("../../process_simplification/page/production_report_history/production_report_history.js");

const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
	"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[character]));
const helpers = {
	translate: (message) => message,
	escapeHtml,
	formatNumber: (value) => String(Number(value || 0)),
	formatDateTime: (value) => value || "-",
};
const report = {
	name: "REPORT-1", status: "Approved", wage_type: "Piecework", completed_qty: 120,
	wage_amount: 60, rate: 0.5, modified: "2026-10-19 10:00:00.123456", can_correct_payroll_qty: true, payroll_qty_corrected: 0, payroll_qty: 0,
};

function harness(save) {
	const calls = [], messages = [], dialogs = [], alerts = [];
	class Dialog {
		constructor(options) {
			this.options = options;
			this.values = Object.fromEntries(options.fields.filter((field) => field.fieldname).map((field) => [field.fieldname, field.default]));
			this.button = { prop: (_, value) => { this.disabled = value; return this.button; } };
			dialogs.push(this);
		}
		get_primary_btn() { return this.button; }
		show() { this.shown = true; }
		hide() { this.hidden = true; }
	}
	global.frappe = {
		ui: { Dialog },
		msgprint: (message) => messages.push(message),
		show_alert: (message) => alerts.push(message),
		call: async (request) => {
			calls.push(request);
			return save ? save(request) : { message: { report: { name: report.name, payroll_qty_corrected: 1, payroll_qty: request.args.payroll_qty }, summaries: [] } };
		},
	};
	return { calls, messages, dialogs, alerts };
}

test("payroll correction is exposed only for explicitly authorized approved piecework", () => {
	assert.equal(review.reviewCanCorrectPayrollQuantity(report), true);
	const h = harness();
	for (const row of [
		{ ...report, status: "Pending Approval" }, { ...report, status: "Rejected" },
		{ ...report, wage_type: "Time" }, { ...report, can_correct_payroll_qty: false },
		{ ...report, can_correct_payroll_qty: undefined }, { ...report, can_correct_payroll_qty: "true" },
	]) {
		assert.equal(review.reviewCanCorrectPayrollQuantity(row), false);
		assert.equal(review.openReviewPayrollQuantityDialog(row, { helpers }), null);
	}
	assert.equal(h.dialogs.length, 0);
	assert.equal(h.calls.length, 0);
});

test("review and worker history retain production quantity and escape the payroll audit trail", () => {
	const row = {
		...report, payroll_qty_corrected: true, payroll_qty: 100, payroll_amount: 50,
		payroll_correction_reason: '<script>alert("bad")</script>', payroll_corrected_by: "owner<@factory>",
		payroll_corrected_at: "2026-10-19 11:00:00",
	};
	for (const html of [review.reviewDetailsHtml(row, helpers), history.workerReportHistoryCardHtml(row, helpers)]) {
		assert.match(html, /原报工数量/);
		assert.match(html, />120</);
		assert.match(html, /核定计薪数量/);
		assert.match(html, />100</);
		assert.match(html, /计薪金额[^]*?>50</);
		assert.match(html, /核定原因/);
		assert.match(html, /&lt;script&gt;/);
		assert.doesNotMatch(html, /<script>/);
		assert.match(html, /owner&lt;@factory&gt;/);
		assert.match(html, /2026-10-19 11:00:00/);
		assert.match(html, /原生产记录保留/);
	}
	assert.match(review.reviewDetailsHtml(row, helpers), /原报工金额[^]*?>60</);
});

test("a zero payroll amount does not fall back to the original wage", () => {
	const row = { ...report, payroll_qty_corrected: true, payroll_qty: 0, payroll_amount: 0 };
	for (const html of [review.reviewDetailsHtml(row, helpers), history.workerReportHistoryCardHtml(row, helpers)]) {
		assert.match(html, /核定计薪数量[^]*?>0</);
		assert.match(html, /计薪金额[^]*?>0</);
		assert.doesNotMatch(html, /计薪金额<\/[^>]+><strong>60/);
	}
});

test("unchanged history keeps its existing quantity and wage labels", () => {
	const html = history.workerReportHistoryCardHtml(report, helpers);
	assert.match(html, /完成数量/);
	assert.match(html, />120</);
	assert.match(html, /计薪金额[^]*?>60</);
	assert.doesNotMatch(html, /核定原因|核定计薪数量/);
});

test("blocked payroll correction shows the server explanation as plain text", () => {
	const html = review.reviewDetailsHtml({
		...report, can_correct_payroll_qty: false, payroll_correction_block_message: "工资已确认 <locked>",
	}, helpers);
	assert.match(html, /工资已确认 &lt;locked&gt;/);
	assert.doesNotMatch(html, /<locked>/);
});

test("save sends a zero quantity, reason and optimistic revision, then refreshes the view", async () => {
	const h = harness();
	let saved;
	const dialog = review.openReviewPayrollQuantityDialog(report, { helpers, onSaved: (value) => { saved = value; } });
	assert.equal(dialog.values.payroll_qty, 120);
	assert.equal(dialog.options.fields.find((field) => field.fieldname === "reason").reqd, 1);
	await dialog.options.primary_action({ payroll_qty: 0, reason: "  实际未做，计薪核定为零  " });
	assert.equal(h.calls.length, 1);
	const request = h.calls[0];
	assert.equal(request.type, "POST");
	assert.equal(request.method, "process_simplification.api.production_reporting.set_report_payroll_quantity");
	assert.deepEqual(Object.keys(request.args).sort(), ["expected_modified", "payroll_qty", "reason", "report", "request_id"]);
	assert.equal(request.args.report, "REPORT-1");
	assert.equal(request.args.payroll_qty, 0);
	assert.equal(request.args.reason, "实际未做，计薪核定为零");
	assert.equal(request.args.expected_modified, report.modified);
	assert.ok(request.args.request_id);
	assert.equal(dialog.hidden, true);
	assert.equal(dialog.disabled, false);
	assert.equal(saved.report.payroll_qty, 0);
});

test("invalid quantity or missing reason issues no write", async () => {
	const h = harness();
	const dialog = review.openReviewPayrollQuantityDialog(report, { helpers });
	for (const payroll_qty of [undefined, null, "", " ", -1, NaN, Infinity, "invalid"]) {
		await dialog.options.primary_action({ payroll_qty, reason: "核实" });
	}
	await dialog.options.primary_action({ payroll_qty: 100, reason: "  " });
	assert.equal(h.calls.length, 0);
	assert.equal(dialog.hidden, undefined);
	assert.equal(h.messages.length, 9);
});

test("lost response preserves the dialog input and reuses the exact request for explicit retry", async () => {
	let attempts = 0, refreshed = 0;
	const h = harness(async () => {
		if (++attempts === 1) throw new Error("response lost");
		return { message: { report: { name: report.name, payroll_qty_corrected: 1, payroll_qty: 100 }, summaries: [] } };
	});
	const dialog = review.openReviewPayrollQuantityDialog(report, { helpers, onSaved: () => { refreshed++; } });
	dialog.values = { payroll_qty: 100, reason: "实际100件" };
	await assert.rejects(dialog.options.primary_action(dialog.values), /response lost/);
	assert.equal(h.calls.length, 1);
	assert.equal(dialog.hidden, undefined);
	assert.equal(dialog.disabled, false);
	assert.deepEqual(dialog.values, { payroll_qty: 100, reason: "实际100件" });
	assert.equal(refreshed, 0);
	await dialog.options.primary_action(dialog.values);
	assert.equal(h.calls.length, 2);
	assert.deepEqual(h.calls[0], h.calls[1]);
	assert.equal(refreshed, 1);
});

test("double click cannot create two in-flight corrections", async () => {
	let resolve;
	const h = harness(() => new Promise((done) => { resolve = done; }));
	const dialog = review.openReviewPayrollQuantityDialog(report, { helpers });
	const values = { payroll_qty: 100, reason: "实际100件" };
	const first = dialog.options.primary_action(values);
	await dialog.options.primary_action(values);
	assert.equal(h.calls.length, 1);
	assert.equal(dialog.disabled, true);
	resolve({ message: { report: { name: report.name, payroll_qty_corrected: 1, payroll_qty: 100 }, summaries: [] } });
	await first;
	assert.equal(dialog.disabled, false);
});

test("a subsequent correction starts from the latest payroll quantity and has a new request identity", async () => {
	const h = harness();
	const corrected = { ...report, payroll_qty_corrected: true, payroll_qty: 100, payroll_amount: 50 };
	const first = review.openReviewPayrollQuantityDialog(corrected, { helpers });
	assert.equal(first.values.payroll_qty, 100);
	await first.options.primary_action({ payroll_qty: 90, reason: "复核" });
	const second = review.openReviewPayrollQuantityDialog({ ...corrected, payroll_qty: 90 }, { helpers });
	await second.options.primary_action({ payroll_qty: 80, reason: "再次复核" });
	assert.notEqual(h.calls[0].args.request_id, h.calls[1].args.request_id);
});


test("an incomplete or mismatched receipt keeps inputs and the request identity until verified", async () => {
	for (const badReport of [undefined, { name: "OTHER", payroll_qty_corrected: 1, payroll_qty: 100 },
		{ name: report.name, payroll_qty_corrected: 0, payroll_qty: 100 },
		{ name: report.name, payroll_qty_corrected: 1, payroll_qty: 120 }]) {
		let attempts = 0, saved = 0;
		const h = harness(async () => ({ message: { report: ++attempts === 1 ? badReport : {
			name: report.name, payroll_qty_corrected: true, payroll_qty: 100,
		} } }));
		const dialog = review.openReviewPayrollQuantityDialog(report, { helpers, onSaved: () => { saved++; } });
		dialog.values = { payroll_qty: 100, reason: "核实100件" };
		await dialog.options.primary_action(dialog.values);
		assert.equal(dialog.hidden, undefined);
		assert.equal(dialog.disabled, false);
		assert.equal(saved, 0);
		assert.equal(h.calls.length, 1);
		assert.equal(h.messages.length, 1);
		await dialog.options.primary_action(dialog.values);
		assert.equal(dialog.hidden, true);
		assert.equal(saved, 1);
		assert.deepEqual(h.calls[0], h.calls[1]);
	}
});
