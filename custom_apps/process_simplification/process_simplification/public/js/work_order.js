frappe.ui.form.on("Work Order", {
	refresh(frm) {
		if (!frappe.model.can_create("Stock Entry")) {
			for (const label of ["Start", "Finish", "Return Components"]) {
				frm.remove_custom_button(__(label));
			}
			for (const label of ["Additional Material Transfer", "Material Consumption"]) {
				frm.remove_custom_button(__(label), __("Make"));
			}
			frm.remove_custom_button(__("Disassemble Order"), __("Create"));
		}
		const doc = frm.doc;
		const can_request = frappe.session.user === "Administrator"
			|| ["System Manager", "Process Simplification Owner", "Process Simplification Production Manager"]
				.some((role) => (frappe.user_roles || []).includes(role));
		const return_gap = (doc.required_items || []).some((row) =>
			row.include_item_in_manufacturing !== 0 && Number(row.returned_qty) > 0
			&& Math.max(Number(row.transferred_qty || 0) - Number(row.returned_qty || 0), 0)
				+ 1e-9 < Number(row.required_qty || 0));
		if (!can_request || !return_gap || doc.docstatus !== 1
			|| ["Completed", "Stopped", "Closed", "Cancelled"].includes(doc.status)
			|| doc.skip_transfer || doc.transfer_material_against === "Job Card" || doc.track_semi_finished_goods) return;
		frm.add_custom_button(__("退料后重新申请领料"), () => {
			frappe.confirm(__("按当前库存重新申请领料，允许部分领料；剩余缺口仍保留。生成申请后，请库房核对实物并提交出库。"), () => {
				frappe.call({
					method: "process_simplification.production_workflow.service.request_material_issue",
					type: "POST",
					args: { work_order: doc.name, allow_partial: 1 },
					freeze: true,
				}).then((response) => {
					const result = response.message || {};
					if (!result.stock_entry) return;
					frappe.show_alert({ message: __("请库房核对领料申请并提交出库。"), indicator: "green" });
					frappe.set_route("Form", "Stock Entry", result.stock_entry);
				});
			});
		});
	},
});
