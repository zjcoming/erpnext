"use strict";

const WAGE_SLIP_API = "process_simplification.api.wage_slips.";

function wageSlipMonth(value) {
	const match = String(value || "").match(/^(\d{4})(?:-|年)(\d{2})(?:-|月|$)/);
	return match ? `${match[1]}-${match[2]}-01` : "";
}

function wageSlipMonthLabel(value) {
	const month = wageSlipMonth(value);
	return month ? `${month.slice(0, 4)}年${month.slice(5, 7)}月` : "";
}

function wageSlipSelection(selected) {
	if (!selected.length) return {};
	const company = selected[0].company;
	const month_start = wageSlipMonth(selected[0].month_start || selected[0].wage_month);
	if (
		!company ||
		!month_start ||
		selected.some(
			(row) =>
				row.company !== company || wageSlipMonth(row.month_start || row.wage_month) !== month_start
		)
	) {
		throw new Error("请勾选同一公司、同一月份的工资汇总后打印。");
	}
	return { company, month_start, summary_names: [...new Set(selected.map((row) => row.name))] };
}

async function printWageSlips(args, { frappeRef = frappe, windowRef = window } = {}) {
	// Open within the click event, before the request, so normal popup blockers
	// do not discard the print window after an asynchronous response.
	const popup = windowRef.open("", "_blank");
	if (!popup) {
		frappeRef.msgprint(__("请允许浏览器弹出窗口，然后重新点击打印。"));
		return false;
	}
	popup.opener = null;
	popup.document.write(
		'<!doctype html><meta charset="utf-8"><title>工资条</title><p>正在准备工资条...</p>'
	);
	try {
		const response = await frappeRef.call({ method: `${WAGE_SLIP_API}get_print_html`, args });
		if (popup.closed) return false;
		if (!response.message?.html) throw new Error("没有可打印的工资条。");
		popup.document.open();
		popup.document.write(response.message.html);
		popup.document.close();
		await popup.document.fonts?.ready;
		if (popup.closed) return false;
		popup.focus();
		popup.print();
		return true;
	} catch (error) {
		if (!popup.closed) popup.close();
		throw error;
	}
}

async function openWageSlipDialog({ selected = [], preferredMonth = "" } = {}) {
	let selection;
	try {
		selection = wageSlipSelection(selected);
	} catch (error) {
		frappe.msgprint(__(error.message));
		return;
	}
	const response = await frappe.call({ method: `${WAGE_SLIP_API}get_print_periods`, freeze: true });
	const periods = response.message || [];
	if (!periods.length) {
		frappe.msgprint(__("没有可打印的工资汇总，请先生成月度汇总。"));
		return;
	}
	const companies = [...new Set(periods.map((row) => row.company))];
	const company = selection.company || companies[0];
	const monthOptions = (value) =>
		[
			...new Set(
				periods.filter((row) => row.company === value).map((row) => wageSlipMonth(row.month_start))
			),
		].map((value) => ({ value, label: wageSlipMonthLabel(value) }));
	const initialMonths = monthOptions(company);
	const preferred = selection.month_start || wageSlipMonth(preferredMonth);
	const month =
		initialMonths.find((option) => option.value === preferred)?.value || initialMonths[0]?.value;
	if (selection.summary_names && (!companies.includes(company) || month !== selection.month_start)) {
		frappe.msgprint(__("所选汇总已失效或无读取权限，请刷新列表后重试。"));
		return;
	}
	let dialog;
	let employees = [];
	let loadedKey = "";
	let requestVersion = 0;
	let printing = false;
	const key = () => `${dialog.get_value("company")}|${dialog.get_value("month_start")}`;
	const selectedKey = `${selection.company}|${selection.month_start}`;
	const loadEmployees = async () => {
		const version = ++requestVersion;
		const requestKey = key();
		// Frappe may fire default-value onchange handlers after dialog construction.
		// Every load of the selected period must preserve the requested employees.
		const preselected = requestKey === selectedKey ? selection.summary_names || [] : [];
		loadedKey = "";
		employees = [];
		dialog.get_primary_btn().prop("disabled", true);
		await dialog.set_value("summary_names", []);
		if (version !== requestVersion || requestKey !== key()) return;
		dialog.fields_dict.print_hint.$wrapper.text(__("正在读取本月工资汇总..."));
		try {
			const result = await frappe.call({
				method: `${WAGE_SLIP_API}get_print_employees`,
				args: { company: dialog.get_value("company"), month_start: dialog.get_value("month_start") },
			});
			if (version !== requestVersion || requestKey !== key()) return;
			employees = result.message || [];
			if (preselected.some((name) => !employees.some((row) => row.name === name))) {
				employees = [];
				dialog.fields_dict.print_hint.$wrapper.text(
					__("部分勾选汇总已失效或无读取权限，请刷新列表后重试。")
				);
				return;
			}
			const control = dialog.fields_dict.summary_names;
			await control.set_options();
			if (version !== requestVersion || requestKey !== key()) return;
			await dialog.set_value("summary_names", preselected);
			if (version !== requestVersion || requestKey !== key()) return;
			loadedKey = requestKey;
			dialog.fields_dict.print_hint.$wrapper.text(
				employees.length
					? __("本月已有 {0} 人的工资汇总。每张 A4 纸排 5 条，沿虚线裁剪；草稿会标明待确认。", [
							employees.length,
					  ])
					: __("本月没有可打印的工资汇总，请先生成月度汇总。")
			);
		} catch (error) {
			if (version === requestVersion) {
				dialog.fields_dict.print_hint.$wrapper.text(__("读取失败，请重新选择月份或稍后重试。"));
			}
			throw error;
		} finally {
			if (version === requestVersion) {
				dialog
					.get_primary_btn()
					.prop("disabled", printing || loadedKey !== key() || !employees.length);
			}
		}
	};
	dialog = new frappe.ui.Dialog({
		title: __("打印工资条"),
		fields: [
			{
				fieldname: "company",
				fieldtype: "Select",
				label: __("公司"),
				options: companies,
				default: company,
				reqd: 1,
				onchange: async () => {
					if (!dialog) return;
					const options = monthOptions(dialog.get_value("company"));
					dialog.set_df_property("month_start", "options", options);
					const current = dialog.get_value("month_start");
					const next =
						options.find((option) => option.value === current)?.value || options[0]?.value;
					if (next !== current) await dialog.set_value("month_start", next);
					else await loadEmployees();
				},
			},
			{
				fieldname: "month_start",
				fieldtype: "Select",
				label: __("工资月份"),
				options: initialMonths,
				default: month,
				reqd: 1,
				onchange: () => {
					if (dialog) return loadEmployees();
				},
			},
			{
				fieldname: "scope",
				fieldtype: "Select",
				label: __("打印范围"),
				options: ["全部员工", "指定员工"],
				default: selection.summary_names ? "指定员工" : "全部员工",
				reqd: 1,
			},
			{
				fieldname: "summary_names",
				fieldtype: "MultiSelectList",
				label: __("选择员工"),
				depends_on: 'eval:doc.scope === "指定员工"',
				placeholder: __("搜索姓名或工号，可多选"),
				get_data: (text) =>
					employees
						.filter((row) =>
							`${row.employee_name || ""} ${row.employee}`
								.toLowerCase()
								.includes(String(text || "").toLowerCase())
						)
						.map((row) => ({
							value: row.name,
							label: frappe.utils.escape_html(row.employee_name || row.employee),
							description: `${frappe.utils.escape_html(row.employee)} · ${
								row.docstatus === 1 ? __("已确认") : __("草稿 · 待确认")
							}`,
						})),
			},
			{ fieldname: "print_hint", fieldtype: "HTML" },
		],
		primary_action_label: __("打印"),
		primary_action: async (values) => {
			if (printing || loadedKey !== key() || !employees.length) return;
			const args = { company: values.company, month_start: values.month_start };
			if (values.scope === "指定员工") {
				if (!values.summary_names?.length) {
					frappe.msgprint(__("请选择需要打印的员工。"));
					return;
				}
				args.summary_names = values.summary_names;
			}
			printing = true;
			dialog.get_primary_btn().prop("disabled", true);
			try {
				if (await printWageSlips(args)) dialog.hide();
			} finally {
				printing = false;
				dialog.get_primary_btn().prop("disabled", loadedKey !== key() || !employees.length);
			}
		},
	});
	const employeeControl = dialog.fields_dict.summary_names;
	const updateStatus = employeeControl.update_status.bind(employeeControl);
	employeeControl.update_status = function () {
		if (this.values.length > 1) this.set_status(__("已选择 {0} 名员工", [this.values.length]));
		else updateStatus();
	};
	dialog.show();
	await loadEmployees();
	return dialog;
}

if (typeof frappe !== "undefined") {
	frappe.provide("process_simplification");
	process_simplification.openWageSlipDialog = openWageSlipDialog;
}
if (typeof module !== "undefined")
	module.exports = { wageSlipMonth, wageSlipSelection, printWageSlips, openWageSlipDialog };
