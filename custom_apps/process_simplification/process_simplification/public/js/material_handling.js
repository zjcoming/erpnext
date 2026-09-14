/* One factory-facing entry for terminal-order returns and downstream decisions. */
function materialHandlingRows(data = []) {
    return data.map(row => ({material_key: row.key, item_name: row.item_name || row.item_code,
        stock_uom: row.stock_uom, available: Number(row.requestable_qty || 0), qty: 0,
        route: `${row.source_warehouse} → ${row.return_warehouse || "待确认"}`}));
}

function createMaterialHandlingPanel(page, root) {
    const api = "process_simplification.api.production_exceptions.";
    const esc = frappe.utils.escape_html;
    let data = {requests: [], sources: [], labels: {}};
    const panel = $(`<section class="review-section material-handling-panel">
        <div class="worker-assignment-actions"><button class="btn btn-default mh-closeout">完工/停工退余料</button>
        <button class="btn btn-default mh-disposition">待检/报废处置</button></div>
        <p class="text-muted">在产任务由工人申请；收尾余料和退库后的处理从这里进入。</p>
        <details class="mh-queue"><summary>收尾与处置待办</summary><div class="mh-requests"></div></details><details class="mh-history"><summary>最近处理记录</summary><div class="mh-history-list"></div></details>
    </section>`).prependTo(root);
    async function call(method, args = {}, write = false) {
        return (await frappe.call({method: api + method, args, type: write ? "POST" : "GET"})).message;
    }
    async function load() {
        data = await call("get_material_handling_dashboard");
        const pending = data.requests.filter(r => ["Pending Approval", "Awaiting Stock Entry"].includes(r.status));
        panel.find(".mh-queue > summary").text(`收尾与处置待办（${pending.length}）`);
        if (pending.length) panel.find(".mh-queue").prop("open", true);
        panel.find(".mh-requests").html(pending.length ? pending.map(r => `<article class="worker-assignment-card" data-request="${esc(r.name)}">
            <strong>${esc(r.label)} · ${esc(r.work_order || r.source_stock_entry || "")}</strong><p>${r.status === "Pending Approval" ? "待主管确认" : "待库房核对并过账"}</p>
            <p>${esc(r.reason || "")}</p>${r.items.map(i => `<p>${esc(i.item_name || i.item_code)}：${Number(i.qty)} ${esc(i.stock_uom)}<br><small>${esc(i.source_warehouse)} → ${esc(i.target_warehouse || (r.action === "Supplier Return" ? "供应商" : "处置出库"))}</small></p>`).join("")}
            <div class="worker-assignment-actions">
            ${r.status === "Pending Approval" && data.can_review ? '<button class="btn btn-primary mh-approve">确认处理</button><button class="btn btn-default mh-reject">驳回</button>' : ""}
            ${r.status === "Awaiting Stock Entry" && data.can_post ? '<button class="btn btn-primary mh-post">核对实物并过账</button>' : ""}
            <button class="btn btn-default mh-open">查看单据</button>${data.can_review || r.requested_by === frappe.session.user ? '<button class="btn btn-link mh-withdraw">撤回重填</button>' : ""}
            </div></article>`).join("") : '<p class="text-muted">当前没有待办。</p>');
        const labels = {Completed:"已完成", Withdrawn:"已撤回", Rejected:"已驳回"};
        const history = data.requests.filter(r => labels[r.status]).slice(0, 30);
        panel.find(".mh-history-list").html(history.map(r => `<article class="worker-assignment-card" data-request="${esc(r.name)}"><strong>${esc(r.label)} · ${labels[r.status]}</strong><p>${esc(r.work_order || r.source_stock_entry || "")} · ${esc(r.reason || "")}</p><button class="btn btn-default mh-open">查看处理记录</button>${r.rework_order ? '<button class="btn btn-primary mh-rework">打开返工工单</button>' : ""}</article>`).join("") || '<p class="text-muted">暂无处理记录。</p>');
    }
    function openDialog(closeout) {
        if (!closeout && !data.sources.length) {
            frappe.msgprint({title:"暂无待处置物料",message:"当前没有已过账且有剩余待处置量的退料单。请先在“我的任务”申请待检或报废，主管确认并由库房完成收料后，再从这里处理。建立仓库本身不会产生待处置物料。"});
            return;
        }
        let options = null, generation = 0, dialog;
        const key = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
        dialog = new frappe.ui.Dialog({title: closeout ? "完工/停工退余料" : "待检与报废处置", size: "large",
            fields: [
                {fieldname: "work_order", fieldtype: "Link", options: "Work Order", label: "生产工单", hidden: !closeout, reqd: closeout,
                    get_query: () => ({filters: {docstatus: 1, status: ["in", ["Completed", "Closed", "Stopped"]]}}), onchange: refresh},
                {fieldname: "source_stock_entry", fieldtype: "Select", label: "选择待处理退料单", hidden: closeout, reqd: !closeout,
                    options: [{label: "请选择", value: ""}, ...data.sources.map(s => ({label: `${s.item_name || s.item_code || "退料"} · ${s.stock_entry}`, value: s.stock_entry}))], onchange: refresh},
                {fieldname: "action", fieldtype: "Select", label: "怎么处理", reqd: 1, options: [], onchange: actionChanged},
                {fieldname: "notice", fieldtype: "HTML"},
                {fieldname: "all_qty", fieldtype: "Button", label: "填入全部可处理数量", click: () => {
                    dialog.fields_dict.materials.df.data.forEach(r => r.qty = r.available);
                    dialog.fields_dict.materials.grid.refresh();
                }},
                {fieldname: "materials", fieldtype: "Table", label: "填写实际交回/处理数量（不处理的行留 0）", cannot_add_rows: true, cannot_delete_rows: true,
                    in_place_edit: true, data: [], fields: [
                        {fieldname: "material_key", fieldtype: "Data", hidden: 1},
                        {fieldname: "item_name", fieldtype: "Data", label: "物料", read_only: 1, in_list_view: 1, columns: 4},
                        {fieldname: "available", fieldtype: "Float", label: "最多", read_only: 1, in_list_view: 1, columns: 2},
                        {fieldname: "qty", fieldtype: "Float", label: "数量", in_list_view: 1, columns: 2},
                        {fieldname: "stock_uom", fieldtype: "Data", label: "单位", read_only: 1, in_list_view: 1, columns: 2},
                        {fieldname: "purchase_receipt_item", fieldtype: "Select", label: "原收货行（同物料多行时选择）", options: []},
                    ]},
                {fieldname: "purchase_receipt", fieldtype: "Link", options: "Purchase Receipt", label: "原采购收货单", hidden: 1,
                    get_query: () => ({filters: {docstatus: 1, is_return: 0, company: options?.company}}), onchange: async () => {
                        if (!dialog || !dialog.get_value("purchase_receipt") || !options) return;
                        const receipt = await call("get_handling_receipt_options", {receipt: dialog.get_value("purchase_receipt"), company: options.company});
                        const choices = receipt.map(r => ({label: `${r.idx} · ${r.item_code} · ${r.qty} ${r.uom}`, value: r.name}));
                        dialog.fields_dict.materials.grid.update_docfield_property("purchase_receipt_item", "options", [{label:"请选择", value:""}, ...choices]);
                        for (const row of dialog.fields_dict.materials.df.data) {
                            const route = options.materials.find(r => r.key === row.material_key);
                            const matching = receipt.filter(r => r.item_code === route?.item_code);
                            row.purchase_receipt_item = matching.length === 1 ? matching[0].name : "";
                        }
                        dialog.fields_dict.materials.grid.refresh();
                    }},
                {fieldname: "rework_bom", fieldtype: "Link", options: "BOM", label: "返工 BOM", hidden: 1,
                    get_query: () => ({filters: {docstatus: 1, is_active: 1, company: options?.company}})},
                {fieldname: "reason", fieldtype: "Small Text", label: "补充说明（选填）"},
            ], primary_action_label: "确认处理，交库房核对", primary_action: async values => {
                const items = (values.materials || []).filter(r => Number(r.qty) > 0).map(r => ({material_key:r.material_key, qty:r.qty, purchase_receipt_item:r.purchase_receipt_item}));
                if (!items.length) {frappe.msgprint("请填写至少一行实际数量。"); return;}
                dialog.get_primary_btn().prop("disabled", true);
                try {
                    await call("create_material_handling", {action: values.action, items, request_key:key,
                        work_order: closeout ? values.work_order : options.work_order, source_stock_entry: closeout ? null : values.source_stock_entry,
                        reason:values.reason, purchase_receipt:values.purchase_receipt, rework_bom:values.rework_bom}, true);
                    dialog.hide(); await load(); frappe.show_alert({message:"处理单已生成，请库房核对实物后过账。",indicator:"green"});
                } finally {dialog.get_primary_btn().prop("disabled", false);}
            }});
        function actionChanged() {
            if (!dialog) return;
            const action = dialog.get_value("action");
            dialog.set_df_property("purchase_receipt", "hidden", action !== "Supplier Return");
            dialog.set_df_property("purchase_receipt", "reqd", action === "Supplier Return");
            dialog.set_df_property("rework_bom", "hidden", action !== "Rework");
            dialog.set_df_property("rework_bom", "reqd", action === "Rework");
            const note = action === "Dispose" ? "这是最终处置出库，会减少报废仓数量和库存价值，请填写处置依据。" : action === "Rework" ? "系统生成转仓单和返工工单草稿。库房转仓后，再按正常生产流程提交返工工单。" : "系统核对来源、已用量及其他申请；确认后生成草稿，库房过账才改变库存。";
            dialog.set_df_property("reason", "reqd", action === "Dispose");
            dialog.set_df_property("notice", "options", `<p class="text-muted">${esc(note)}</p>`);
        }
        async function refresh() {
            if (!dialog) return;
            const value = dialog.get_value(closeout ? "work_order" : "source_stock_entry");
            const current = ++generation;
            options = null; dialog.get_primary_btn().prop("disabled", true);
            dialog.fields_dict.materials.df.data = []; dialog.fields_dict.materials.grid.refresh();
            if (!value) return;
            const result = await call(closeout ? "get_closeout_options" : "get_disposition_options", closeout ? {work_order:value} : {source_stock_entry:value});
            if (current !== generation) return;
            options = result;
            const actions = closeout ? ["Closeout Return", "Closeout Quarantine", "Closeout Scrap"] : options.actions;
            dialog.set_df_property("action", "options", actions.map(a => ({value:a,label:data.labels[a]})));
            await dialog.set_value("action", actions[0]);
            dialog.fields_dict.materials.df.data = materialHandlingRows(options.materials);
            dialog.fields_dict.materials.grid.refresh();
            dialog.get_primary_btn().prop("disabled", !options.materials.length);
            if (!options.materials.length) dialog.set_df_property("notice","options",'<p class="text-muted">当前已没有可处理余量，请核对已有申请或库存单。</p>');
        }
        dialog.show();dialog.get_primary_btn().prop("disabled", true);
    }
    panel.on("click", ".mh-closeout", () => openDialog(true));
    panel.on("click", ".mh-disposition", () => openDialog(false));
    panel.on("click", "article button", async event => {
        const button = $(event.currentTarget), row = data.requests.find(r => r.name === button.closest("article").data("request"));
        if (!row) return;
        if (button.hasClass("mh-rework")) {frappe.set_route("Form", "Work Order", row.rework_order);return;}
        if (button.hasClass("mh-open")) {frappe.set_route("Form", row.purchase_return ? "Purchase Receipt" : row.stock_entry ? "Stock Entry" : "Material Handling Request",row.purchase_return || row.stock_entry || row.name);return;}
        if (button.hasClass("mh-withdraw") || button.hasClass("mh-reject")) {frappe.prompt([{fieldname:"reason",fieldtype:"Small Text",label:"撤回原因",reqd:1}],async values => {await call("withdraw_material_handling",{request:row.name,reason:values.reason,reject:button.hasClass("mh-reject") ? 1 : 0},true);await load();},button.hasClass("mh-reject") ? "驳回处理单" : "撤回未过账处理单");return;}
        const post = button.hasClass("mh-post");
        let detail = row.items.map(r => `${esc(r.item_name || r.item_code)} ${Number(r.qty)} ${esc(r.stock_uom)}：${esc(r.source_warehouse)} → ${esc(r.target_warehouse || "出库")}`).join("<br>");
        if (post) {
            const preview = await call("get_handling_posting_preview", {request:row.name});
            detail = preview.items.map(r => `${esc(r.item_name || r.item_code)} ${Number(r.qty)} ${esc(r.stock_uom)}：${esc(r.source)} → ${esc(r.target || "出库")}<br>${r.lots.filter(l => l.batch_no || l.serial_no).map(l => `批次 ${esc(l.batch_no || "—")} / 序列号 ${esc(l.serial_no || "—")} · ${Number(l.qty)}`).join("<br>")}`).join("<hr>");
            if (row.action === "Dispose") detail += `<hr>费用科目：${[...new Set(preview.items.map(r => r.expense_account).filter(Boolean))].map(esc).join("、") || "请打开单据确认"}`;
            if (preview.supplier) detail += `<hr>供应商：${esc(preview.supplier)}<br>原收货单：${esc(preview.return_against)}<br>退货单合计：${Number(preview.grand_total)} ${esc(preview.currency)}<br>请一并核对原单带出的价格和附加费用。`;
        }
        frappe.confirm(`${detail}<p>${post ? "确认已核对实物和单据，执行库存过账？" : "确认此处理决定，生成库房单据？"}</p>`,async () => {
            button.prop("disabled", true);
            try {await call(post ? "post_material_handling" : "approve_material_handling",{request:row.name},true);await load();}
            finally {button.prop("disabled", false);}
        });
    });
    load(); return {load};
}
if (typeof module !== "undefined" && module.exports) module.exports = {materialHandlingRows};
