frappe.ui.form.on("Delivery Note", {
	before_submit(frm) {
		// Another page can submit while this page's confirmation stays open.
		if (Number(frm.doc.docstatus) === 0) return;
		frappe.validated = false;
		frappe.msgprint(__("该发货单状态已更新，请刷新后查看。"));
	},
});
