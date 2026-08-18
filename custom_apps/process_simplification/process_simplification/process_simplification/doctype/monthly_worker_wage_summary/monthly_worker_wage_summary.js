frappe.ui.form.on("Monthly Worker Wage Summary", {
	refresh(frm) {
		// This document deliberately has only Draft and Confirmed states. Even an
		// Administrator must use the controlled confirm API and must never see the
		// standard Submit/Cancel lifecycle actions.
		frm.disable_save();
		frm.page.clear_primary_action();
		frm.page.clear_secondary_action();
		frm.dashboard.set_headline(__("该单据是生产报工工资汇总，不代表已付款，也不生成会计凭证。"), "blue");
		frm.add_custom_button(__("计价规则"), () => frappe.set_route("List", "Operation Wage Rate"));
		if (frappe.user.has_role("System Manager")) {
			frm.add_custom_button(__("返回报工审核"), () => frappe.set_route("production-report-review"));
		}
		const monthEnded = Boolean(frm.doc.month_end && frm.doc.month_end < frappe.datetime.get_today());
		if (frm.doc.docstatus === 0 && !frm.is_new() && monthEnded) {
			frm.add_custom_button(__("确认月度汇总"), () => openConfirmDialog(frm), __("工资汇总"));
		} else if (frm.doc.docstatus === 0 && !frm.is_new()) {
			frm.dashboard.add_comment(__("当前自然月尚未结束，月末后才能确认；确认前如有新报工，请重新生成草稿。"), "orange", true);
		}
	},
});

function setSummaryDialogBusy(dialog, busy, label) {
	dialog.get_primary_btn().prop("disabled", busy).text(busy ? __("处理中...") : label);
}

function openConfirmDialog(frm) {
	const label = __("确认汇总");
	const dialog = new frappe.ui.Dialog({
		title: __("确认月度工资汇总"),
		fields: [{ fieldtype: "HTML", options: `<p>${__("确认后，本月全部已通过报工将固定进入该汇总；Job Card 可以仍在继续报工。报工状态仍保持“已通过”。")}</p><p><strong>${frappe.utils.escape_html(frm.doc.employee || "")} · ${frappe.utils.escape_html(frm.doc.month_start || "")} · ${format_currency(frm.doc.total_amount || 0)}</strong></p>` }],
		primary_action_label: label,
		primary_action: async () => {
			setSummaryDialogBusy(dialog, true, label);
			try {
				await frappe.call({
					method: "process_simplification.api.production_reporting.confirm_monthly_summary",
					type: "POST",
					args: { summary_name: frm.doc.name },
				});
				dialog.hide();
				await frm.reload_doc();
			} finally {
				setSummaryDialogBusy(dialog, false, label);
			}
		},
	});
	dialog.show();
}
