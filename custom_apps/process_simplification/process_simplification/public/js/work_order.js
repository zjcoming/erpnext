frappe.ui.form.on("Work Order", {
	refresh(frm) {
		if (frappe.model.can_create("Stock Entry")) return;
		for (const label of ["Start", "Finish", "Return Components"]) {
			frm.remove_custom_button(__(label));
		}
		for (const label of ["Additional Material Transfer", "Material Consumption"]) {
			frm.remove_custom_button(__(label), __("Make"));
		}
		frm.remove_custom_button(__("Disassemble Order"), __("Create"));
	},
});
