frappe.ui.form.on("Company", {
	setup(frm) {
		frm.set_query("custom_default_semi_finished_warehouse", () => ({
			filters: {
				company: frm.doc.company_name || frm.doc.name,
				is_group: 0,
				disabled: 0,
			},
		}));
	},
});
