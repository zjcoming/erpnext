frappe.listview_settings["Monthly Worker Wage Summary"] = {
	onload(listview) {
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
				fieldtype: "Date",
				label: __("月份"),
				default: frappe.datetime.month_start(),
				reqd: 1,
			},
			{
				fieldname: "employee",
				fieldtype: "Link",
				options: "Employee",
				label: __("工人（留空为全部）"),
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
