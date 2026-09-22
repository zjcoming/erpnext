// Rename keeps the warehouse's stock, defaults and permission references together.
frappe.ui.form.on("Warehouse", {
	refresh(frm) {
		const manager = frappe.session.user === "Administrator"
			|| frappe.user.has_role("System Manager");
		if (frm.is_new() || !frm.doc.parent_warehouse || !frm.doc.company
			|| !manager || !frm.has_perm("write")) return;

		frm.add_custom_button(__("修改名称"), () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("请先保存当前修改，再修改仓库名称。"));
				return;
			}
			const warehouse = frm.doc.name;
			const dialog = new frappe.ui.Dialog({
				title: __("修改仓库名称"),
				fields: [{
					fieldname: "new_name", fieldtype: "Data", label: __("新的仓库名称"),
					reqd: 1, default: frm.doc.warehouse_name,
					description: __("填写名称即可，公司后缀会保留；库存、默认仓库和人员权限关联会同步更新。"),
				}],
				primary_action_label: __("保存新名称"),
				async primary_action(values) {
					if (dialog.renaming) return;
					dialog.renaming = true;
					dialog.disable_primary_action();
					try {
						const response = await frappe.call({
							method: "process_simplification.api.warehouse_management.rename_warehouse",
							type: "POST", freeze: true, freeze_message: __("正在修改仓库名称…"),
							args: { warehouse, new_name: values.new_name },
						});
						if (!response.message?.name) return;
						dialog.hide();
						frappe.model.clear_doc("Warehouse", warehouse);
						// TreeFactory reuses cached views on back-navigation. Use its
						// native Refresh action to discard old node names, keeping filters.
						const treeview = frappe.views?.trees?.Warehouse;
						if (treeview?.tree) treeview.make_tree();
						await frappe.set_route("Form", "Warehouse", response.message.name);
						frappe.show_alert({ message: __("仓库名称已更新。"), indicator: "green" });
					} finally {
						dialog.renaming = false;
						dialog.enable_primary_action();
					}
				},
			});
			dialog.show();
		});
	},
});
