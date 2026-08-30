function wageSummaryMonthLabel(value) {
	const match = String(value || "").match(/^(\d{4})-(\d{2})/);
	return match ? `${match[1]}年${match[2]}月` : String(value || "");
}

function wageSummaryMonthOptions(today, { includeAll = false, count = 24 } = {}) {
	const [year, month] = String(today || "").slice(0, 7).split("-").map(Number);
	const options = includeAll ? [{ value: "", label: "全部月份" }] : [];
	if (!year || !month) return options;
	for (let offset = 0; offset < count; offset += 1) {
		const absoluteMonth = year * 12 + month - 1 - offset;
		const optionYear = Math.floor(absoluteMonth / 12);
		const optionMonth = (absoluteMonth % 12) + 1;
		const value = `${optionYear}-${String(optionMonth).padStart(2, "0")}-01`;
		options.push({ value, label: wageSummaryMonthLabel(value) });
	}
	return options;
}

function wageSummaryFilterMonthOptions(today, options = {}) {
	return wageSummaryMonthOptions(today, options).map((option) => ({
		value: option.value ? option.label : "",
		label: option.label,
	}));
}

function setupWageSummaryMonthFilter(listview) {
	if (listview.page.fields_dict?.wage_month) return;
	listview.page.add_field({
		fieldname: "wage_month",
		fieldtype: "Select",
		label: __("筛选工资月份"),
		options: wageSummaryFilterMonthOptions(frappe.datetime.get_today(), {
			includeAll: true,
		}),
		change() {
			listview.refresh();
		},
	});
}

if (typeof frappe !== "undefined") {
	frappe.listview_settings["Monthly Worker Wage Summary"] = {
		onload(listview) {
			setupWageSummaryMonthFilter(listview);
			listview.page.add_inner_button(__("计价规则"), () => {
				frappe.set_route("List", "Operation Wage Rate");
			});
			listview.page.add_inner_button(__("生成月度汇总"), async () => {
				const response = await frappe.call({
					method: "process_simplification.api.production_reporting.get_wage_management_context",
					freeze: true,
				});
				openMonthlyWorkerWageSummaryDialog(listview, response.message?.companies || []);
			});
		},
	};
}

function openMonthlyWorkerWageSummaryDialog(listview, companies) {
	if (!companies.length) {
		frappe.msgprint(__("没有可用公司，无法生成工资汇总。"));
		return;
	}
	const dialog = new frappe.ui.Dialog({
		title: __("生成月度工资汇总"),
		fields: [
			{
				fieldname: "company",
				fieldtype: "Select",
				label: __("公司"),
				options: companies.join("\n"),
				default: companies[0],
				reqd: 1,
				onchange: () => dialog.set_value("employee", ""),
			},
			{
				fieldname: "month_start",
				fieldtype: "Select",
				label: __("工资月份"),
				options: wageSummaryMonthOptions(frappe.datetime.get_today()),
				default: `${frappe.datetime.get_today().slice(0, 7)}-01`,
				description: __("按自然月汇总审核通过的生产报工。"),
				reqd: 1,
			},
			{
				fieldname: "employee",
				fieldtype: "Link",
				options: "Employee",
				label: __("工人姓名（留空为全部）"),
				get_query: () => ({
					query: "process_simplification.api.production_reporting.search_wage_employees",
					filters: { company: dialog.get_value("company") },
				}),
			},
		],
		primary_action_label: __("生成或刷新草稿"),
		primary_action: async (values) => {
			const button = dialog.get_primary_btn();
			button.prop("disabled", true).text(__("处理中..."));
			try {
				const result = await frappe.call({
					method: "process_simplification.api.production_reporting.build_monthly_summaries",
					type: "POST",
					args: values,
				});
				dialog.hide();
				frappe.show_alert({
					message: __("已生成或刷新 {0} 张月度汇总。", [
						(result.message?.summaries || []).length,
					]),
					indicator: "green",
				});
				await listview.refresh();
			} finally {
				button.prop("disabled", false).text(__("生成或刷新草稿"));
			}
		},
	});
	dialog.show();
}

if (typeof module !== "undefined") {
	module.exports = {
		wageSummaryMonthLabel,
		wageSummaryMonthOptions,
		wageSummaryFilterMonthOptions,
	};
}
