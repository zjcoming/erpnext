frappe.ui.form.on("Monthly Worker Wage Summary", {
	refresh(frm) {
		// This document deliberately has only Draft and Confirmed states. Even an
		// Administrator must use the controlled confirm API and must never see the
		// standard Submit/Cancel lifecycle actions.
		frm.disable_save();
		frm.page.clear_primary_action();
		frm.page.clear_secondary_action();
		// Frappe adds its native "Submit this document to confirm" banner before
		// this refresh handler. This DocType is confirmed only through the guarded
		// monthly-summary API, so replace that misleading native copy completely.
		frm.dashboard.clear_comment();
		frm.dashboard.set_headline(__("该单据是生产报工工资汇总，不代表已付款，也不生成会计凭证。"), "blue");
		frm.dashboard.add_comment(`${frappe.utils.escape_html(frm.doc.employee_name || frm.doc.employee || "")} · ${frappe.utils.escape_html(frm.doc.wage_month || "")} · ${format_currency(frm.doc.total_amount || 0)}`, "green", true);
		frm.add_custom_button(__("计价规则"), () => frappe.set_route("List", "Operation Wage Rate"));
		if (frappe.user.has_role("System Manager")) {
			frm.add_custom_button(__("返回报工审核"), () => frappe.set_route("production-report-review"));
		}
		const monthEnded = Boolean(frm.doc.month_end && frm.doc.month_end < frappe.datetime.get_today());
		if (frm.doc.docstatus === 0 && !frm.is_new() && monthEnded) {
			frm.add_custom_button(__("确认月度汇总"), () => openConfirmDialog(frm), __("工资汇总"));
			frm.dashboard.add_comment(__("本工资月份已结束。请先结束本月正在计时、暂停的作业并完成审核，重新生成草稿后，从「工资汇总 → 确认月度汇总」完成确认。"), "orange", true);
		} else if (frm.doc.docstatus === 0 && !frm.is_new()) {
			frm.dashboard.add_comment(__("当前自然月尚未结束，暂不显示确认按钮；月末结束后即可确认。确认前如有新报工，请重新生成草稿。"), "orange", true);
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
		fields: [{ fieldtype: "HTML", options: `<p>${__("确认前会检查本月未结束、暂停和待审核的作业。跨月作业归属于开始作业的日期，必须先结束并审核。确认后本月报工固定，工单仍可继续记录后续月份的新作业。")}</p><p><strong>${frappe.utils.escape_html(frm.doc.employee_name || frm.doc.employee || "")} · ${frappe.utils.escape_html(frm.doc.wage_month || "")} · ${format_currency(frm.doc.total_amount || 0)}</strong></p>` }],
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
