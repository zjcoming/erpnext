const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { openWageAdjustmentDialog } = require("../../public/js/wage_slips.js");

global.__ = (text) => text;

function harness({ canRecord = true, companies = ["Factory", "Other"], save, initializeLinkDefaults = false } = {}) {
	const calls = [], messages = [], dialogs = [], defaultQueries = [];
	class Dialog {
		constructor(options) {
			this.options = options;
			this.values = Object.fromEntries(options.fields.filter((row) => row.fieldname).map((row) => [row.fieldname, row.default]));
			if (initializeLinkDefaults) {
				// Native FieldGroup validates Link defaults inside the constructor,
				// before `dialog = new Dialog(...)` assigns the outer reference.
				for (const field of options.fields.filter((row) => row.fieldtype === "Link" && row.default)) {
					defaultQueries.push({ fieldname: field.fieldname, query: field.get_query() });
				}
			}
			this.button = { prop: (_, value) => { this.disabled = value; return this.button; } };
			dialogs.push(this);
		}
		get_value(key) { return this.values[key]; }
		async set_value(key, value) { this.values[key] = value; }
		get_primary_btn() { return this.button; }
		show() { this.shown = true; }
		hide() { this.hidden = true; }
	}
	global.frappe = {
		ui: { Dialog },
		datetime: { get_today: () => "2026-10-17" },
		utils: { get_form_link: (doctype, name) => `<a>${doctype}/${name}</a>` },
		msgprint: (message) => messages.push(message),
		call: async (request) => {
			calls.push(request);
			if (request.method.endsWith("get_wage_management_context")) {
				return { message: { companies, can_record_adjustments: canRecord } };
			}
			return save ? save(request) : { message: { name: "WWA-001", docstatus: 1, remaining_amount: 100 } };
		},
	};
	const values = { company: "Factory", employee: "EMP-1", month_start: "2026-10-01", adjustment_type: "Deduction", amount: 100, reason: "更正上次计薪", source_report: "REPORT-1" };
	return { calls, messages, dialogs, values, defaultQueries };
}

test("Link default validation can query its company during construction without losing the selected worker", async () => {
	const h = harness({ initializeLinkDefaults: true });
	const dialog = await openWageAdjustmentDialog({ company: "Other", employee: "EMP-OTHER" });
	assert.deepEqual(h.defaultQueries, [{
		fieldname: "employee",
		query: { query: "process_simplification.api.production_reporting.search_wage_employees", filters: { company: "Other" } },
	}]);
	assert.equal(dialog.values.employee, "EMP-OTHER");
	const field = (name) => dialog.options.fields.find((row) => row.fieldname === name);
	field("company").onchange();
	assert.equal(dialog.values.employee, "EMP-OTHER");
	dialog.values.company = "";
	assert.equal(field("employee").get_query().filters.company, "");
});

test("a reader without wage-adjustment authority cannot open the write dialog", async () => {
	const h = harness({ canRecord: false });
	await openWageAdjustmentDialog();
	assert.equal(h.dialogs.length, 0);
	assert.equal(h.calls.length, 1);
	assert.ok(h.messages.length);
});

test("lost response retries retain one request ID and recover the same record", async () => {
	let attempts = 0;
	const h = harness({ save: async () => {
		if (++attempts === 1) throw new Error("response lost after commit");
		return { message: { name: "WWA-001", docstatus: 1, remaining_amount: 100 } };
	} });
	let saved;
	const dialog = await openWageAdjustmentDialog({ ...h.values, onSaved: (value) => { saved = value; } });
	await assert.rejects(dialog.options.primary_action(h.values), /response lost/);
	assert.equal(dialog.disabled, false);
	assert.notEqual(dialog.hidden, true);
	await dialog.options.primary_action(h.values);
	const writes = h.calls.filter((row) => row.method.endsWith("record_wage_adjustment"));
	assert.equal(writes.length, 2);
	assert.equal(writes[0].type, "POST");
	assert.ok(writes[0].args.request_id);
	assert.deepEqual(writes[0].args, writes[1].args);
	assert.equal(saved.name, "WWA-001");
	assert.equal(dialog.hidden, true);
	assert.ok(h.messages.some((row) => String(row.message).includes("WWA-001")));
});

test("an in-flight save blocks a second click and releases the button on completion", async () => {
	let finish;
	const h = harness({ save: () => new Promise((resolve) => { finish = resolve; }) });
	const dialog = await openWageAdjustmentDialog();
	const first = dialog.options.primary_action(h.values);
	assert.equal(dialog.disabled, true);
	await dialog.options.primary_action(h.values);
	assert.equal(h.calls.filter((row) => row.method.endsWith("record_wage_adjustment")).length, 1);
	finish({ message: { name: "WWA-002" } });
	await first;
	assert.equal(dialog.disabled, false);
});

test("invalid amounts never issue a wage write", async () => {
	const h = harness();
	const dialog = await openWageAdjustmentDialog();
	for (const amount of [0, -1, NaN, Infinity, "bad"]) {
		await dialog.options.primary_action({ ...h.values, amount });
	}
	assert.equal(h.calls.filter((row) => row.method.endsWith("record_wage_adjustment")).length, 0);
});

test("switching company clears the previous worker and report; report search stays scoped", async () => {
	const h = harness();
	const dialog = await openWageAdjustmentDialog(h.values);
	const field = (name) => dialog.options.fields.find((row) => row.fieldname === name);
	assert.deepEqual(field("source_report").get_query().filters, { company: "Factory", employee: "EMP-1", status: "Approved" });
	dialog.values.company = "Other";
	field("company").onchange();
	assert.equal(dialog.values.employee, "");
	assert.equal(dialog.values.source_report, "");
	assert.equal(field("employee").get_query().filters.company, "Other");
});

test("a separate adjustment starts a new identity without changing production quantities", async () => {
	const h = harness();
	const one = await openWageAdjustmentDialog();
	await one.options.primary_action(h.values);
	const two = await openWageAdjustmentDialog();
	await two.options.primary_action({ ...h.values, adjustment_type: "Supplement", amount: 20 });
	const writes = h.calls.filter((row) => row.method.endsWith("record_wage_adjustment"));
	assert.notEqual(writes[0].args.request_id, writes[1].args.request_id);
	assert.equal(writes[0].args.source_report, "REPORT-1");
	for (const row of writes) {
		assert.equal("completed_qty" in row.args, false);
		assert.equal("reported_minutes" in row.args, false);
		assert.equal("wage_amount" in row.args, false);
	}
});

function adjustmentFormHarness({ canRecord = true } = {}) {
	const api = "process_simplification.api.production_reporting.";
	const calls = [], comments = [], buttons = [];
	let controller;
	const frappeRef = {
		ui: { form: { on: (_, events) => { controller = events; } } },
		utils: { get_form_link: (doctype, name) => `<a>${doctype}/${name}</a>` },
		call: async (request) => {
			calls.push(request);
			if (request.method === `${api}get_wage_adjustment_balance`) {
				// frappe.call defaults to POST. Match the actual GET-only endpoint,
				// so loading the controller without an explicit GET must fail.
				if ((request.type || "POST") !== "GET") {
					throw Object.assign(new Error("PermissionError: balance endpoint only allows GET"), { status: 403 });
				}
				return { message: { original_amount: 100, applied_amount: 0, remaining_amount: 100, applications: [] } };
			}
			if (request.method === `${api}get_wage_management_context`) {
				return { message: { can_record_adjustments: canRecord } };
			}
			throw new Error(`Unexpected form request: ${request.method}`);
		},
	};
	const filename = path.join(__dirname, "../../process_simplification/doctype/worker_wage_adjustment/worker_wage_adjustment.js");
	vm.runInNewContext(fs.readFileSync(filename, "utf8"), {
		frappe: frappeRef, __: (text) => text, format_currency: (value) => Number(value).toFixed(2),
	}, { filename });
	const frm = {
		doc: { name: "WWA-2026-00002", docstatus: 1 },
		is_new: () => false,
		disable_save() {},
		page: { clear_primary_action() {}, clear_secondary_action() {} },
		dashboard: { clear_comment() {}, set_headline() {}, add_comment: (message) => comments.push(message) },
		add_custom_button: (label) => buttons.push(label),
	};
	return { api, calls, comments, buttons, frappeRef, frm, refresh: () => controller.refresh(frm) };
}

test("saved adjustment refresh uses the GET-only balance API and shows owner balances and controlled cancellation", async () => {
	const apiSource = fs.readFileSync(path.join(__dirname, "../../api/production_reporting.py"), "utf8");
	assert.match(apiSource, /@frappe\.whitelist\(methods=\["GET"\]\)\s+def get_wage_adjustment_balance\(name\)/);
	const h = adjustmentFormHarness();
	for (const type of [undefined, "POST"]) {
		await assert.rejects(h.frappeRef.call({ method: `${h.api}get_wage_adjustment_balance`, type }),
			(error) => error.status === 403 && /GET/.test(error.message));
	}
	h.calls.length = 0;
	await h.refresh();
	const reads = h.calls.filter((row) => row.method.endsWith("get_wage_adjustment_balance"));
	assert.equal(reads.length, 1);
	assert.equal(reads[0].type, "GET");
	assert.equal(reads[0].args.name, h.frm.doc.name);
	assert.ok(h.comments.some((text) => text.includes("原金额 100.00") && text.includes("已确认应用 0.00") && text.includes("未应用余额 100.00")));
	assert.ok(h.buttons.includes("撤销未结算补扣"));
});

test("saved adjustment refresh keeps balance visible to a read-only wage manager without a cancel action", async () => {
	const h = adjustmentFormHarness({ canRecord: false });
	await h.refresh();
	assert.ok(h.comments.some((text) => text.includes("未应用余额 100.00")));
	assert.equal(h.buttons.includes("撤销未结算补扣"), false);
});
