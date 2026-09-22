function purchaseReceiptQuantityLabels(isReturn) {
	return isReturn ? { qty: "退货数量", received_qty: "退货总数", rejected_qty: "拒收数量" } :
		{ qty: "合格数量", received_qty: "到货总数", rejected_qty: "拒收数量" };
}

function labelPurchaseReceiptQuantities(frm) {
	const grid = frm.fields_dict.items?.grid;
	if (!grid) return;
	const isReturn = Boolean(Number(frm.doc.is_return));
	for (const [field, label] of Object.entries(purchaseReceiptQuantityLabels(isReturn))) {
		grid.update_docfield_property(field, "label", __(label));
	}
	grid.update_docfield_property("qty", "description", __(isReturn ?
		"按实际退货填写负数，并核对退货来源仓库。" : "合格入库数量。到货总数 = 合格数量 + 拒收数量。"));
	grid.update_docfield_property("rejected_qty", "description", __("拒收物料需选拒收仓，后续退换货单独办理。"));
}

if (typeof module !== "undefined" && module.exports) module.exports = { purchaseReceiptQuantityLabels, labelPurchaseReceiptQuantities };

if (typeof frappe !== "undefined") {
	frappe.ui.form.on("Purchase Receipt", {
		refresh(frm) {
			labelPurchaseReceiptQuantities(frm);
			if (Number(frm.doc.docstatus) === 1 && !Number(frm.doc.is_return) &&
				(frm.doc.items || []).some((item) => Number(item.rejected_qty) > 0) &&
				frappe.boot?.page_info?.["purchase-rejection-followup"]) {
				frm.add_custom_button(__("拒收与补货跟进"), () => {
					frappe.route_options = { receipt: frm.doc.name, company: frm.doc.company };
					frappe.set_route("purchase-rejection-followup");
				});
			}
		},
		is_return: labelPurchaseReceiptQuantities,
		validate(frm) {
			if (!Number(frm.doc.is_return) && frm.doc.docstatus === 0 && (frm.doc.items || []).some((item) => Number(item.rejected_qty) > 0)) {
				frappe.show_alert({ message: __("本单含拒收：到货总数 = 合格数量 + 拒收数量。请核对拒收仓；提交后仍需安排退换货。"), indicator: "orange" }, 7);
			}
		},
	});
}
