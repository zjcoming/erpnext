frappe.ui.form.on("Worker Wage Adjustment", {
	async refresh(frm) {
		frm.disable_save();
		frm.page.clear_primary_action();
		frm.page.clear_secondary_action();
		frm.dashboard.clear_comment();
		frm.dashboard.set_headline(__("工资补扣独立登记，不更改原报工数量或工时，也不代表付款。"), "blue");
		if (frm.is_new()) return;
		const name = frm.doc.name;
		const [response, context] = await Promise.all([
			frappe.call({
				method: "process_simplification.api.production_reporting.get_wage_adjustment_balance",
				type: "GET",
				args: { name },
			}),
			frappe.call({ method: "process_simplification.api.production_reporting.get_wage_management_context" }),
		]);
		if (frm.doc.name !== name) return;
		const balance = response.message;
		if (!balance) return;
		frm.dashboard.add_comment([
			`${__("原金额")} ${format_currency(balance.original_amount || 0)}`,
			`${__("已确认应用")} ${format_currency(balance.applied_amount || 0)}`,
			`${__("未应用余额")} ${format_currency(balance.remaining_amount || 0)}`,
		].join(" · "), "blue", true);
		const applications = balance.applications || [];
		if (applications.length) {
			frm.dashboard.add_comment(applications.map((row) =>
				`${frappe.utils.get_form_link("Monthly Worker Wage Summary", row.summary, true)} · ${Number(row.docstatus) === 1 ? __("已确认应用") : __("草稿预计")} ${format_currency(row.amount || 0)}`
			).join("<br>"), "blue", true);
		}
		frm.dashboard.add_comment(__("草稿应用仅为计划，不扣减未应用余额；扣回金额最多抵至当月工资为0，余款留待后续月份。"), "blue", true);
		if (context.message?.can_record_adjustments !== true || frm.doc.docstatus !== 1 || Number(balance.applied_amount || 0) > 0 || applications.some((row) => Number(row.docstatus) === 1)) return;
		let cancelling = false;
		frm.add_custom_button(__("撤销未结算补扣"), () => {
			frappe.confirm(__("确认撤销这笔尚未应用到已确认月度汇总的工资补扣？撤销后请重新生成相关月度草稿。"), async () => {
				if (cancelling) return;
				cancelling = true;
				try {
					await frappe.call({
						method: "process_simplification.api.production_reporting.cancel_wage_adjustment",
						type: "POST", args: { name }, freeze: true,
					});
					await frm.reload_doc();
				} finally {
					cancelling = false;
				}
			});
		});
	},
});
