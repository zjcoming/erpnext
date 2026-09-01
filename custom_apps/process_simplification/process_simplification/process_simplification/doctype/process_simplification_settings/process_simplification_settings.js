frappe.ui.form.on("Process Simplification Settings", {
	setup(frm) {
		frm.set_query("user", "notification_recipients", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return {
				query: "process_simplification.process_simplification.doctype.process_simplification_settings.process_simplification_settings.search_notification_users",
				filters: {
					company: row.company,
					responsibility: row.responsibility,
				},
			};
		});
		frm.set_query("role_profile", "notification_role_recipients", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return {
				query: "process_simplification.process_simplification.doctype.process_simplification_settings.process_simplification_settings.search_notification_role_profiles",
				filters: { responsibility: row.responsibility },
			};
		});
	},
});
