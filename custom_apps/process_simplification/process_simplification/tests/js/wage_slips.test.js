const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {
	wageSlipMonth,
	wageSlipSelection,
	printWageSlips,
	openWageSlipDialog,
} = require("../../public/js/wage_slips.js");

global.__ = (text, values = []) => text.replace(/\{(\d+)\}/g, (_, index) => values[index]);

test("checked rows retain exactly one company and month, including old history", () => {
	assert.equal(wageSlipMonth("2023年06月"), "2023-06-01");
	assert.equal(wageSlipMonth("2026-08-01"), "2026-08-01");
	assert.equal(wageSlipMonth("全部月份"), "");
	const row = { company: "Factory", month_start: "2023-06-01", name: "W1" };
	assert.deepEqual(wageSlipSelection([row, row, { ...row, name: "W2" }]), {
		company: "Factory",
		month_start: "2023-06-01",
		summary_names: ["W1", "W2"],
	});
	for (const change of [{ company: "Other" }, { month_start: "2023-07-01" }]) {
		assert.throws(() => wageSlipSelection([row, { ...row, ...change }]), /同一公司、同一月份/);
	}
});

function printHarness({ blocked = false, error = null, response = { html: "<html>工资条</html>" } } = {}) {
	const events = [];
	const popup = {
		closed: false,
		document: {
			open() {
				events.push("document.open");
			},
			write(value) {
				events.push(["write", value]);
			},
			close() {
				events.push("document.close");
			},
			fonts: { ready: Promise.resolve() },
		},
		focus() {
			events.push("focus");
		},
		print() {
			events.push("print");
		},
		close() {
			popup.closed = true;
			events.push("close");
		},
	};
	const dependencies = {
		windowRef: {
			open() {
				events.push("open");
				return blocked ? null : popup;
			},
		},
		frappeRef: {
			msgprint(message) {
				events.push(["message", message]);
			},
			async call(request) {
				events.push(["call", request]);
				if (error) throw error;
				return { message: response };
			},
		},
	};
	return { events, popup, dependencies };
}

test("opens the print window synchronously and prints the server document with the exact selection", async () => {
	const { events, popup, dependencies } = printHarness();
	const args = { company: "Factory", month_start: "2026-08-01", summary_names: ["W1", "W2"] };
	assert.equal(await printWageSlips(args, dependencies), true);
	assert.equal(events[0], "open");
	assert.deepEqual(events.find((event) => event[0] === "call")[1].args, args);
	assert.ok(events.indexOf("document.close") < events.indexOf("print"));
	assert.equal(popup.opener, null);
	assert.equal(popup.closed, false);
});

test("blocked popups give a retry hint before reading wages", async () => {
	const { events, dependencies } = printHarness({ blocked: true });
	assert.equal(await printWageSlips({}, dependencies), false);
	assert.equal(
		events.some((event) => event[0] === "call"),
		false
	);
	assert.match(events[1][1], /允许浏览器弹出窗口/);
});

test("failed or empty responses close the placeholder and never print", async () => {
	for (const scenario of [{ error: new Error("denied") }, { response: {} }]) {
		const { events, popup, dependencies } = printHarness(scenario);
		await assert.rejects(printWageSlips({}, dependencies));
		assert.equal(popup.closed, true);
		assert.equal(events.includes("print"), false);
	}
});

function dialogHarness() {
	const calls = [],
		messages = [],
		dialogs = [];
	let employeeResponse = [{ name: "W1", employee: "EMP1", employee_name: "张三", docstatus: 0 }];
	const periods = [
		{ company: "Factory", month_start: "2026-08-01" },
		{ company: "Factory", month_start: "2023-06-01" },
	];
	class Dialog {
		constructor(options) {
			this.options = options;
			this.values = {};
			this.fields_dict = {};
			for (const field of options.fields) {
				this.values[field.fieldname] = field.default;
				this.fields_dict[field.fieldname] = {
					df: field,
					update_status() {},
					set_options: async () => {},
					$wrapper: {
						text: (value) => {
							this.hint = value;
						},
					},
				};
			}
			this.button = {
				prop: (_, value) => {
					this.disabled = value;
					return this.button;
				},
			};
			dialogs.push(this);
		}
		get_value(key) {
			return this.values[key];
		}
		async set_value(key, value) {
			this.values[key] = value;
		}
		get_primary_btn() {
			return this.button;
		}
		show() {}
		hide() {
			this.hidden = true;
		}
	}
	global.frappe = {
		ui: { Dialog },
		utils: { escape_html: (value) => String(value).replaceAll("<", "&lt;").replaceAll(">", "&gt;") },
		msgprint: (text) => messages.push(text),
		call: async (request) => {
			calls.push(request);
			return { message: request.method.endsWith("get_print_periods") ? periods : employeeResponse };
		},
	};
	return {
		calls,
		messages,
		dialogs,
		setEmployees(value) {
			employeeResponse = value;
		},
	};
}

test("dialog brings checked employees and their historical month into the selection", async () => {
	dialogHarness();
	const dialog = await openWageSlipDialog({
		selected: [{ name: "W1", company: "Factory", month_start: "2023-06-01" }],
	});
	assert.equal(dialog.values.scope, "指定员工");
	assert.equal(dialog.values.month_start, "2023-06-01");
	assert.deepEqual(dialog.values.summary_names, ["W1"]);
	assert.equal(dialog.disabled, false);
});

test("empty specific selection never opens an all-employee print", async () => {
	const { calls, messages } = dialogHarness();
	const dialog = await openWageSlipDialog();
	await dialog.options.primary_action({ ...dialog.values, scope: "指定员工", summary_names: [] });
	assert.match(messages[0], /请选择需要打印的员工/);
	assert.equal(
		calls.some((request) => request.method.endsWith("get_print_html")),
		false
	);
});

test("stale checked employees block printing instead of dropping unavailable names", async () => {
	dialogHarness();
	const dialog = await openWageSlipDialog({
		selected: [{ name: "DELETED", company: "Factory", month_start: "2026-08-01" }],
	});
	assert.equal(dialog.disabled, true);
	assert.match(dialog.hint, /部分勾选汇总已失效/);
});

test("picker escapes employee labels and an empty month keeps print disabled", async () => {
	const harness = dialogHarness();
	harness.setEmployees([{ name: "W1", employee: "E<1>", employee_name: "<img>", docstatus: 0 }]);
	const dialog = await openWageSlipDialog();
	const options = dialog.fields_dict.summary_names.df.get_data("");
	assert.equal(options[0].label, "&lt;img&gt;");
	assert.match(options[0].description, /E&lt;1&gt;/);
	harness.setEmployees([]);
	await dialog.fields_dict.month_start.df.onchange();
	assert.equal(dialog.disabled, true);
	assert.match(dialog.hint, /本月没有可打印/);
});

function deferred() {
	let resolve, reject;
	const promise = new Promise((yes, no) => {
		resolve = yes;
		reject = no;
	});
	return { promise, resolve, reject };
}

test("Frappe default-value change events retain the requested employee selection", async () => {
	const harness = dialogHarness();
	const dialog = await openWageSlipDialog({
		selected: [{ name: "W1", company: "Factory", month_start: "2026-08-01" }],
	});
	await dialog.fields_dict.month_start.df.onchange();
	assert.deepEqual(dialog.values.summary_names, ["W1"]);
	assert.equal(dialog.disabled, false);
	harness.setEmployees([]);
	await dialog.fields_dict.month_start.df.onchange();
	assert.equal(dialog.disabled, true);
	assert.match(dialog.hint, /部分勾选汇总已失效/);
});

test("a late employee response cannot overwrite a newer month", async () => {
	dialogHarness();
	const dialog = await openWageSlipDialog();
	const oldRequest = deferred(),
		newRequest = deferred();
	frappe.call = ({ args }) => (args.month_start === "2026-08-01" ? oldRequest.promise : newRequest.promise);
	const oldLoad = dialog.fields_dict.month_start.df.onchange();
	await Promise.resolve();
	dialog.values.month_start = "2023-06-01";
	const newLoad = dialog.fields_dict.month_start.df.onchange();
	await Promise.resolve();
	newRequest.resolve({ message: [{ name: "NEW", employee: "New month" }] });
	await newLoad;
	oldRequest.resolve({ message: [{ name: "OLD", employee: "Old month" }] });
	await oldLoad;
	assert.deepEqual(
		dialog.fields_dict.summary_names.df.get_data("").map((row) => row.value),
		["NEW"]
	);
	assert.equal(dialog.disabled, false);
});

test("a month switch during picker rendering cannot restore an old preselection", async () => {
	const harness = dialogHarness();
	const rendering = deferred(),
		entered = deferred();
	const NativeDialog = frappe.ui.Dialog;
	frappe.ui.Dialog = class extends NativeDialog {
		constructor(options) {
			super(options);
			this.fields_dict.summary_names.set_options = () => {
				entered.resolve();
				return rendering.promise;
			};
		}
	};
	const opening = openWageSlipDialog({
		selected: [{ name: "W1", company: "Factory", month_start: "2026-08-01" }],
	});
	await entered.promise;
	const dialog = harness.dialogs[0];
	dialog.values.month_start = "2023-06-01";
	dialog.fields_dict.summary_names.set_options = async () => {};
	harness.setEmployees([{ name: "NEW", employee: "New month" }]);
	await dialog.fields_dict.month_start.df.onchange();
	rendering.resolve();
	await opening;
	assert.deepEqual(dialog.values.summary_names, []);
	assert.deepEqual(
		dialog.fields_dict.summary_names.df.get_data("").map((row) => row.value),
		["NEW"]
	);
});

test("repeated print actions create only one popup and a failed month reload keeps printing disabled", async () => {
	dialogHarness();
	const dialog = await openWageSlipDialog();
	const printing = deferred();
	const { events, dependencies } = printHarness();
	global.window = dependencies.windowRef;
	frappe.call = ({ method }) =>
		method.endsWith("get_print_html") ? printing.promise : Promise.reject(new Error("offline"));
	try {
		const firstPrint = dialog.options.primary_action(dialog.values);
		await dialog.options.primary_action(dialog.values);
		assert.equal(events.filter((event) => event === "open").length, 1);
		dialog.values.month_start = "2023-06-01";
		await assert.rejects(dialog.fields_dict.month_start.df.onchange(), /offline/);
		printing.reject(new Error("print failed"));
		await assert.rejects(firstPrint, /print failed/);
		assert.equal(dialog.disabled, true);
		assert.equal(events.includes("print"), false);
	} finally {
		delete global.window;
	}
});

test("list and saved form buttons pass their exact selection to the shared print dialog", () => {
	const calls = [],
		buttons = {};
	let formEvents;
	const app = path.resolve(__dirname, "../..");
	const doctype = path.join(app, "process_simplification/doctype/monthly_worker_wage_summary");
	const sandbox = {
		__: global.__,
		format_currency: String,
		process_simplification: { openWageSlipDialog: (args) => calls.push(args) },
		frappe: {
			listview_settings: {},
			datetime: { get_today: () => "2026-10-07" },
			ui: {
				form: {
					on: (_, events) => {
						formEvents = events;
					},
				},
			},
			user: { has_role: () => false },
			utils: { escape_html: String },
		},
	};
	vm.createContext(sandbox);
	for (const filename of ["monthly_worker_wage_summary_list.js", "monthly_worker_wage_summary.js"]) {
		vm.runInContext(fs.readFileSync(path.join(doctype, filename), "utf8"), sandbox);
	}
	const selected = [{ name: "W1", company: "Factory", month_start: "2023-06-01", docstatus: 0 }];
	const settings = sandbox.frappe.listview_settings["Monthly Worker Wage Summary"];
	assert.ok(settings.add_fields.includes("company") && settings.add_fields.includes("month_start"));
	settings.onload({
		get_checked_items: () => selected,
		page: {
			fields_dict: { wage_month: { get_value: () => "2023年06月" } },
			add_inner_button: (label, callback) => {
				buttons[label] = callback;
			},
		},
	});
	buttons["打印工资条"]();
	assert.equal(calls[0].selected, selected);
	assert.equal(calls[0].preferredMonth, "2023年06月");
	for (const [docstatus, isNew, expected] of [
		[0, false, true],
		[1, false, true],
		[2, false, false],
		[0, true, false],
	]) {
		delete buttons["打印工资条"];
		const doc = { ...selected[0], docstatus };
		formEvents.refresh({
			doc,
			is_new: () => isNew,
			disable_save() {},
			page: { clear_primary_action() {}, clear_secondary_action() {} },
			dashboard: { clear_comment() {}, set_headline() {}, add_comment() {} },
			add_custom_button: (label, callback) => {
				buttons[label] = callback;
			},
		});
		assert.equal(Boolean(buttons["打印工资条"]), expected);
		if (expected) {
			buttons["打印工资条"]();
			assert.equal(calls.at(-1).selected[0], doc);
		}
	}
});
