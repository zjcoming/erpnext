frappe.ui.form.on("Material Request", {
	refresh(frm) {
		if (frm.doc.docstatus === 1 && frm.doc.material_request_type === "Purchase" && !["Stopped", "Cancelled"].includes(frm.doc.status)) {
			frm.add_custom_button(__("采购跟进与采购进度"), () => frappe.set_route("purchase-supplier-allocation", { material_request: frm.doc.name }));
		}
	},
});
