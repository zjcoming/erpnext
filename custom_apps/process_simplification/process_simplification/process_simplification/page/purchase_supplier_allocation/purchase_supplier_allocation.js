function allocationStockTotal(item, rows) {
	return rows.reduce((total, row) => {
		const factor = row.uom === item.stock_uom ? 1 : Number((item.uoms.find((entry) => entry.uom === row.uom) || {}).conversion_factor || 0);
		return total + Number(row.qty || 0) * factor;
	}, 0);
}

function purchaseOrderStage(order) {
	if (order.docstatus === 2 || order.status === "Cancelled") return { label: "已取消", tone: "muted", active: false };
	if (order.status === "Closed") return { label: "已关闭", tone: "muted", active: false };
	if (order.docstatus === 0) return { label: "待提交", tone: "warning", active: true };
	if (["Completed", "To Bill"].includes(order.status)) return { label: "已收货", tone: "success", active: false };
	if (order.status === "On Hold") return { label: "已暂停", tone: "muted", active: false };
	if (Number(order.per_received) > 0 && Number(order.per_received) < 100) return { label: "部分到货", tone: "warning", active: true };
	return { label: "待收货", tone: "info", active: true };
}

function purchaseOverview(model) {
	const drafts = model.orders.filter((order) => purchaseOrderStage(order).label === "待提交").length;
	const waiting = model.orders.filter((order) => ["待收货", "部分到货"].includes(purchaseOrderStage(order).label)).length;
	const unallocated = model.items.filter((item) => item.available_qty > 0).length;
	const received = model.items.filter((item) => item.stock_qty > 0 && item.received_qty >= item.stock_qty).length;
	let title, hint;
	if (drafts) {
		title = `${drafts} 张采购单待提交`;
		hint = (model.can_submit || model.orders.some((order) => order.docstatus === 0 && order.can_submit)
			? "打开采购单，核对供应商、数量和价格后提交。"
			: "草稿已生成，待有采购单提交权限的负责人核对并提交。") + "草稿已占用申请数量，无需重复分配。";
	} else if (unallocated) {
		title = `${unallocated} 项物料待分配供应商`;
		hint = "先选供应商，再确认数量和单价。同一物料可拆给多个供应商。";
	} else if (waiting) {
		title = `${waiting} 张采购单待收货`;
		hint = "每到一批货，登记并提交一张收货单，系统会通知负责人。";
	} else if (model.items.length && received === model.items.length) {
		title = "本次申请已到齐";
		hint = "可在到货通知中查看每批收货记录。";
	} else {
		title = "查看采购进度";
		hint = "按物料查看分配、下单和到货数量。";
	}
	return { drafts, waiting, unallocated, received, title, hint };
}

function supplierAllocationProgress(item, target, rows) {
	const selected = rows.filter((row) => String(row.supplier || "").trim());
	const total = allocationStockTotal(item, selected);
	return { total, remaining: Number(target) - total, missingSupplier: rows.some((row) => Number(row.qty) > 0 && !String(row.supplier || "").trim()) };
}

function purchaseFollowupHtml(orders, { esc, number }) {
	return orders.map((po) => {
		const state = purchaseOrderStage(po);
		const href = `/desk/purchase-order/${encodeURIComponent(po.name)}`;
		return `<details class="purchase-followup-card"${state.active ? " open" : ""}><summary><div class="purchase-order"><div><strong>${esc(po.supplier)}</strong><a href="${href}">${esc(po.name)}</a></div><span class="purchase-badge ${state.tone}">${state.label}</span><a class="btn btn-default" href="${href}">${po.docstatus === 0 && po.can_submit ? "核对并提交" : "查看采购单"}</a>${po.can_receive ? `<button class="btn btn-primary" data-receive-purchase-order="${esc(po.name)}">登记收货</button>` : ""}</div></summary>
		<table class="purchase-table purchase-item-table"><thead><tr><th>物料 / 单位</th><th>订购</th><th>订单已收</th><th>累计退货</th><th>订单待收</th><th>承诺到货日</th></tr></thead><tbody>${(po.items || []).map((item) => `<tr><td><strong>${esc(item.item_name || item.item_code)}</strong><small>${esc(item.item_code)} · ${esc(item.uom)}</small>${item.material_request ? `<a href="/desk/purchase-supplier-allocation?material_request=${encodeURIComponent(item.material_request)}">${esc(item.material_request)}</a>` : ""}</td>${[["订购", item.qty], ["订单已收", item.received_qty], ["累计退货", item.returned_qty], ["订单待收", item.pending_qty]].map(([label, value]) => `<td data-label="${label}">${number(value)}</td>`).join("")}<td data-label="承诺到货日">${esc(item.schedule_date || "未设置")}${item.overdue_days ? `<strong class="purchase-error">逾期 ${number(item.overdue_days)} 天</strong>` : ""}</td></tr>`).join("")}</tbody></table><p class="purchase-footnote">待收以采购单剩余数量为准；累计退货单独列示。数量按每行采购单位显示。</p></details>`;
	}).join("");
}

if (typeof module !== "undefined" && module.exports) module.exports = { allocationStockTotal, purchaseOrderStage, purchaseOverview, supplierAllocationProgress, purchaseFollowupHtml };

if (typeof frappe !== "undefined") {
frappe.pages["purchase-supplier-allocation"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("供应商分配"), single_column: true });
	const api = "process_simplification.purchasing.allocation.";
	const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
	const number = (value) => Number(value || 0).toLocaleString("zh-CN", { maximumFractionDigits: 6 });
	const href = (doctype, name) => `/desk/${doctype}/${encodeURIComponent(name)}`;
	const requestHref = (name) => `/desk/purchase-supplier-allocation?material_request=${encodeURIComponent(name)}`;
	let model, selections, rows, requestKey, referenceOptions = {}, generation = 0;
	const listState = { tab: "requests", view: "pending", search: "", overdue_only: false, start: 0, history: [] };
	const key = () => window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
	const call = async (method, args = {}, freeze = false) => (await frappe.call({ method: api + method, args, type: "POST", freeze })).message;
	const field = (parent, label, fieldtype, value, change, options) => {
		const holder = $('<div class="form-group">').appendTo(parent);
		const id = "purchase-field-" + key();
		$("<label>").attr("for", id).text(__(label)).appendTo(holder);
		let input;
		if (fieldtype === "Select") {
			input = $('<select class="form-control">');
			(options || "").split("\n").forEach((option) => input.append($("<option>").val(option).text(__(option))));
		} else {
			const type = fieldtype === "Date" ? "date" : ["Float", "Currency"].includes(fieldtype) ? "number" : "text";
			input = $('<input class="form-control">').attr({ type, step: "any" });
			if (type === "number") input.attr({ min: 0, inputmode: "decimal" });
		}
		input.attr("id", id).val(value ?? "").appendTo(holder);
		if (fieldtype === "Link") {
			const list = $("<datalist>").attr("id", id + "-options").appendTo(holder);
			(referenceOptions[options] || []).forEach((name) => list.append($("<option>").val(name)));
			input.attr("list", id + "-options");
		}
		const read = () => ["Float", "Currency"].includes(fieldtype) ? Number(input.val()) : input.val();
		input.on("input change", () => { requestKey = key(); change?.(read()); });
		return { get_value: read };
	};

	function progress(item, card) {
		const target = Number(selections[item.name] || 0);
		const value = supplierAllocationProgress(item, target, rows[item.name]);
		let text = `已分配 ${number(value.total)} / ${number(target)} ${item.stock_uom}`;
		if (!target) text = "本次不采购";
		else if (value.missingSupplier) text += " · 待选择供应商";
		else if (Math.abs(value.remaining) > 0.000001) text += ` · ${value.remaining < 0 ? "超出" : "尚差"} ${number(Math.abs(value.remaining))}`;
		else text += " · 数量已分配完整";
		card.find(".purchase-progress").text(text).toggleClass("purchase-error", target > 0 && (value.missingSupplier || Math.abs(value.remaining) > 0.000001));
		page.main.find(".purchase-selected-count").text(`本次已选 ${Object.values(selections).filter((qty) => qty > 0).length} 项物料`);
	}

	function renderSupplierRows(item, card) {
		const host = card.find(".purchase-suppliers").empty();
		rows[item.name].forEach((row, index) => {
			const block = $('<div class="purchase-allocation-row">').appendTo(host);
			const fields = $('<div class="purchase-fields">').appendTo(block);
			field(fields, "供应商", "Link", row.supplier, (v) => { row.supplier = v; progress(item, card); }, "Supplier");
			field(fields, "采购数量", "Float", row.qty, (v) => { row.qty = v; progress(item, card); });
			field(fields, "采购单位", "Select", row.uom, (v) => { row.uom = v; progress(item, card); }, [...new Set([item.stock_uom, ...item.uoms.map((r) => r.uom)])].join("\n"));
			field(fields, `单价（${row.currency || model.currency}）`, "Currency", row.rate, (v) => { row.rate = v; });
			field(fields, "需要日期", "Date", row.schedule_date, (v) => { row.schedule_date = v; });
			const bottom = $('<div class="purchase-row-options">').appendTo(block);
			const advanced = $('<details><summary>币种、税费与条款</summary><div class="purchase-fields"></div></details>').appendTo(bottom).find(".purchase-fields");
			field(advanced, "币种", "Link", row.currency, (v) => { row.currency = v; fields.find("label").eq(3).text(`单价（${v}）`); }, "Currency");
			field(advanced, "税费模板", "Link", row.taxes_and_charges, (v) => { row.taxes_and_charges = v; }, "Purchase Taxes and Charges Template");
			field(advanced, "条款模板", "Link", row.tc_name, (v) => { row.tc_name = v; }, "Terms and Conditions");
			field(advanced, "付款条件", "Link", row.payment_terms_template, (v) => { row.payment_terms_template = v; }, "Payment Terms Template");
			$('<button class="btn btn-sm btn-default">移除</button>').attr("aria-label", `移除供应商 ${index + 1}`).appendTo(bottom).on("click", () => {
				rows[item.name].splice(index, 1); requestKey = key(); renderSupplierRows(item, card);
			});
		});
		progress(item, card);
	}

	function newRow(item, supplier = "", qty = 0) {
		return { material_request_item: item.name, supplier, qty, uom: item.stock_uom, rate: 0,
			currency: model.currency, schedule_date: !item.schedule_date || item.schedule_date < frappe.datetime.nowdate() ? frappe.datetime.nowdate() : item.schedule_date,
			warehouse: item.warehouse };
	}

	function render() {
		const openItems = new Set(page.main.find(".purchase-editor[open]").map((_, node) => node.dataset.item).get());
		page.main.empty();
		const root = $('<div class="process-simplification-page purchase-allocation">').appendTo(page.main);
		const summary = purchaseOverview(model);
		root.append(`<header class="purchase-hero"><div><span class="purchase-eyebrow">采购执行台</span><h1>供应商分配</h1><p><a href="${href("material-request", model.material_request)}">${esc(model.material_request)}</a><span class="purchase-meta"> · ${esc(model.company)}</span></p></div><a class="btn btn-default" href="/desk/purchase-supplier-allocation">切换采购申请</a></header>`);
		root.append(`<div class="purchase-next-action ${summary.drafts ? "is-warning" : ""}"><div><strong>${esc(summary.title)}</strong><p>${esc(summary.hint)}</p></div></div>`);
		root.append(`<div class="purchase-stats">${[[summary.unallocated, "待分配物料", "项"], [summary.drafts, "待提交采购单", "张"], [summary.waiting, "待收货采购单", "张"], [summary.received, "已到齐物料", "项"]].map(([value, label, unit]) => `<div><span>${label}</span><strong>${value}<small>${unit}</small></strong></div>`).join("")}</div>`);
		if (!model.items.some((item) => item.stock_qty > 0)) root.append('<div class="purchase-notice-warning">此申请的采购数量为 0，不能分配供应商。请切换到有实际采购数量的申请。</div>');
		if (model.orders.length) {
			const orders = $('<section class="purchase-section"><div class="purchase-section-heading"><h2>采购单</h2><span class="purchase-meta">按供应商分别下单、收货</span></div><div class="purchase-order-list"></div></section>').appendTo(root).find(".purchase-order-list");
			orders.html(purchaseFollowupHtml([...model.orders].sort((a, b) => (a.docstatus !== 0) - (b.docstatus !== 0)), { esc, number }));
		}
		if (model.can_create && summary.unallocated) renderEditors(root, openItems);
		const section = $('<section class="purchase-section"><div class="purchase-section-heading"><h2>物料采购进度</h2><span class="purchase-meta">数量按各物料的库存单位显示</span></div></section>').appendTo(root);
		section.append(`<table class="purchase-table purchase-item-table"><thead><tr><th>物料 / 单位</th><th>申请数量</th><th>待分配</th><th>草稿占用</th><th>待收货</th><th>已到货</th></tr></thead><tbody>${model.items.map((item) => `<tr><td><strong>${esc(item.item_name || item.item_code)}</strong><small>${esc(item.item_code)} · ${esc(item.stock_uom)}</small></td>${[["申请数量", item.stock_qty], ["待分配", item.available_qty], ["草稿占用", item.draft_qty], ["待收货", item.ordered_pending_qty], ["已到货", item.received_qty]].map(([label, value]) => `<td data-label="${label}" class="${value > 0 ? "has-quantity" : "is-zero"}">${number(value)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
		section.append('<p class="purchase-footnote">草稿占用：已分配但未提交的采购单。已到货：合格收货扣除退货后的数量。</p>');
		if (model.can_create && summary.unallocated) page.set_primary_action(__("预览采购单"), preview);
		else page.clear_primary_action();
	}

	function renderEditors(root, openItems) {
		const section = $('<section class="purchase-section"><div class="purchase-section-heading"><h2>分配供应商</h2><span class="purchase-selected-count"></span></div><div class="purchase-bulk"></div></section>').appendTo(root);
		const bulk = section.find(".purchase-bulk");
		const supplier = field(bulk, "批量选择供应商", "Link", "", null, "Supplier");
		$('<button class="btn btn-default">应用到已选物料</button>').appendTo(bulk).on("click", () => {
			if (!supplier.get_value()) return frappe.msgprint("请先选择供应商。");
			model.items.forEach((item) => {
				if (selections[item.name] > 0) rows[item.name] = [newRow(item, supplier.get_value(), selections[item.name])];
			});
			requestKey = key(); render();
		});
		bulk.append('<span class="purchase-meta">应用后将替换所选物料的供应商分配；可再展开逐项拆分。</span>');
		model.items.filter((item) => item.available_qty > 0).forEach((item, index) => {
			const card = $('<details class="purchase-editor">').attr("data-item", item.name).prop("open", openItems.has(item.name) || (!openItems.size && index === 0)).appendTo(section);
			card.append(`<summary><div><strong>${esc(item.item_name || item.item_code)}</strong><span class="purchase-meta">${esc(item.item_code)} · 可分配 ${number(item.available_qty)} ${esc(item.stock_uom)}</span></div><span class="purchase-progress"></span></summary>`);
			const body = $('<div class="purchase-editor-body">').appendTo(card);
			const head = $('<div class="purchase-editor-controls">').appendTo(body);
			const checkLabel = $('<label class="purchase-check">').appendTo(head);
			const check = $('<input type="checkbox">').prop("checked", selections[item.name] > 0).appendTo(checkLabel);
			checkLabel.append(document.createTextNode("本次采购此物料"));
			field(head, `本次采购数量（${item.stock_uom}）`, "Float", selections[item.name] || 0, (v) => { selections[item.name] = v; check.prop("checked", v > 0); progress(item, card); });
			check.on("change", () => {
				selections[item.name] = check.prop("checked") ? item.available_qty : 0;
				head.find('input[type="number"]').val(selections[item.name]);
				requestKey = key(); progress(item, card);
			});
			head.append(`<span class="purchase-meta">${esc(item.warehouse || "")}${item.sales_order ? ` · ${esc(item.sales_order)}` : ""}</span>`);
			body.append('<div class="purchase-suppliers"></div>');
			renderSupplierRows(item, card);
			$('<button class="btn btn-default">＋ 添加供应商</button>').appendTo(body).on("click", () => {
				const remaining = Math.max(0, Number(selections[item.name]) - allocationStockTotal(item, rows[item.name]));
				rows[item.name].push(newRow(item, "", remaining)); requestKey = key(); renderSupplierRows(item, card);
			});
		});
		const footer = $('<div class="purchase-editor-footer"><p>确认数量和单价后预览，系统会按供应商生成采购单草稿。</p></div>').appendTo(section);
		$('<button class="btn btn-primary">预览采购单</button>').appendTo(footer).on("click", preview);
	}

	async function preview() {
		const quantities = Object.fromEntries(Object.entries(selections).filter(([, qty]) => qty > 0));
		if (!Object.keys(quantities).length) return frappe.msgprint("请至少选择一项本次采购的物料。");
		const data = { company: model.company, quantities, rows: Object.keys(quantities).flatMap((name) => rows[name]) };
		const source = model;
		const previewKey = requestKey;
		const result = await call("preview_allocation", { material_request: source.material_request, data }, true);
		const dialog = new frappe.ui.Dialog({ title: `将生成 ${result.groups.length} 张采购单草稿`, size: "large", fields: [{ fieldname: "preview", fieldtype: "HTML" }],
			primary_action_label: "确认生成草稿", async primary_action() {
				dialog.disable_primary_action();
				try {
					const created = await call("create_purchase_orders", { material_request: source.material_request, request_key: previewKey, data }, true);
					dialog.hide();
					frappe.show_alert({ message: `已生成 ${created.orders.length} 张采购单草稿`, indicator: "green" });
					await load();
				} finally { dialog.enable_primary_action(); }
			} });
		dialog.fields_dict.preview.$wrapper.html(result.groups.map((group) => `<section class="purchase-card"><h4>${esc(group.supplier)} · ${esc(group.currency)}</h4>
			<p class="purchase-meta">${esc(group.reason)}<br>税费：${esc(group.taxes_and_charges || "公司默认")} · 条款：${esc(group.tc_name || "公司默认")} · 付款：${esc(group.payment_terms_template || "默认")}</p>
			${group.rows.map((row) => { const item = source.items.find((entry) => entry.name === row.material_request_item); return `<p>${esc(item.item_name || item.item_code)}：${number(row.qty)} ${esc(row.uom)} × ${number(row.rate)} ${esc(row.currency)} · ${esc(row.schedule_date)} · ${esc(row.warehouse)}</p>`; }).join("")}</section>`).join(""));
		dialog.show();
	}

	function renderRequests() {
		page.main.html('<div class="process-simplification-page purchase-allocation"><header class="purchase-hero"><div><span class="purchase-eyebrow">采购执行台</span><h1>供应商分配</h1><p class="purchase-meta">选择采购申请，分配供应商并跟进每批到货。</p></div><a class="btn btn-default" href="/desk/shortage-purchase-planning">缺料采购</a></header><section class="purchase-section"><div class="purchase-section-heading"><h2>已建采购申请</h2><span class="purchase-request-count"></span></div><div class="purchase-request-search"></div><div class="purchase-request-list"></div></section></div>');
		const labels = { Pending: "待下单", "Partially Ordered": "部分下单", Ordered: "已下单", "Partially Received": "部分到货", Received: "已到货", Transferred: "已转移", Issued: "已领用" };
		const controls = page.main.find(".purchase-request-search").addClass("purchase-list-filters");
		if (frappe.model.can_read("Purchase Order")) {
			field(controls, "查看内容", "Select", listState.tab === "requests" ? "采购申请" : "供应商待收货", (value) => { listState.tab = value === "采购申请" ? "requests" : "suppliers"; listState.start = 0; listState.history = []; renderRequests(); }, "采购申请\n供应商待收货");
		}
		if (listState.tab === "requests") {
			const views = { 待处理: "pending", 已处理历史: "history", 全部: "all" };
			field(controls, "申请范围", "Select", Object.keys(views).find((key) => views[key] === listState.view), (value) => { listState.view = views[value]; reload(); }, Object.keys(views).join("\n"));
		} else {
			field(controls, "到货范围", "Select", listState.overdue_only ? "仅逾期" : "全部待收", (value) => { listState.overdue_only = value === "仅逾期"; reload(); }, "全部待收\n仅逾期");
		}
		let timer;
		field(controls, listState.tab === "requests" ? "搜索申请、公司、状态或物料" : "搜索供应商、物料、采购单或申请", "Data", listState.search, (value) => { listState.search = value; clearTimeout(timer); timer = setTimeout(reload, 250); });
		const pager = $('<div class="purchase-list-pager">').appendTo(page.main.find(".purchase-section"));
		function reload() { listState.start = 0; listState.history = []; show(); }
		page.purchase_refresh = show;
		async function show(options = {}) {
			if (!controls.get(0).isConnected) return false;
			const current = ++generation;
			if (!options.background) {
				pager.empty();
				page.main.find(".purchase-request-list").html('<div class="purchase-empty">正在查询…</div>');
			}
			try {
				return await frappe.ps_read_page(page, {
					method: api + (listState.tab === "requests" ? "get_request_page" : "get_supplier_followup"),
					args: { start: listState.start, page_length: 20, search: listState.search, ...(listState.tab === "requests" ? { view: listState.view } : { overdue_only: Number(listState.overdue_only) }) },
					background: options.background,
					apply: ({ message: result }) => {
				if (!controls.get(0).isConnected) return false;
				pager.empty();
				const matches = result.rows || [];
				page.main.find(".purchase-section-heading h2").text(listState.tab === "requests" ? "采购申请" : "供应商待收货");
				page.main.find(".purchase-request-count").text(`第 ${listState.history.length + 1} 页 · 本页 ${matches.length} 张`);
				page.main.find(".purchase-request-list").html(!matches.length ? '<div class="purchase-empty">当前范围没有匹配记录，可切换范围或修改搜索条件。</div>' : listState.tab === "suppliers" ? purchaseFollowupHtml(matches, { esc, number }) : `<table class="purchase-table purchase-request-table"><thead><tr><th>采购申请</th><th>公司</th><th>日期</th><th>状态</th><th></th></tr></thead><tbody>${matches.map((row) => `<tr><td><a href="${requestHref(row.name)}">${esc(row.name)}</a></td><td data-label="公司">${esc(row.company)}</td><td data-label="日期">${esc(row.transaction_date)}</td><td data-label="状态">${esc(labels[row.status] || row.status)}</td><td><a class="btn btn-default" href="${requestHref(row.name)}">分配 / 查看进度</a></td></tr>`).join("")}</tbody></table>`);
				$('<button class="btn btn-default">上一页</button>').prop("disabled", !listState.history.length).appendTo(pager).on("click", () => { listState.start = listState.history.pop(); show(); });
				$('<button class="btn btn-default">下一页</button>').prop("disabled", result.next_start === null).appendTo(pager).on("click", () => { listState.history.push(listState.start); listState.start = result.next_start; show(); });
					},
				});
			} catch (error) {
				if (options.background) throw error;
				if (current === generation) page.main.find(".purchase-request-list").html('<div class="purchase-empty">查询失败，请点击“刷新进度”重试。</div>');
			}
		}
		show();
	}

	async function load() {
		const route = frappe.get_route();
		if (route[1]) return frappe.set_route("purchase-supplier-allocation", { material_request: route[1] });
		const request = frappe.route_options?.material_request || new URLSearchParams(window.location.search).get("material_request");
		page.purchase_refresh = request ? load : null;
		page.ps_refresh?.configure({ manual: Boolean(request), protectInputs: Boolean(request) });
		page.ps_refresh?.resetDirty();
		if (frappe.route_options) delete frappe.route_options.material_request;
		// Frappe route options are transient; keep the document in the shareable URL for reload/back.
		if (request) window.history.replaceState(window.history.state, "", requestHref(request));
		const current = ++generation;
		page.clear_primary_action();
		page.main.html('<div class="purchase-empty">正在读取采购进度…</div>');
		try {
			if (!request) {
				renderRequests();
				return;
			}
			const result = await call("get_allocation_context", { material_request: request });
			if (current !== generation) return;
			model = result;
			referenceOptions = model.reference_options || {};
			selections = {}; rows = {}; requestKey = key();
			model.items.forEach((item) => { selections[item.name] = item.available_qty; rows[item.name] = [newRow(item, item.default_supplier, item.available_qty)]; });
			render();
		} catch (error) {
			if (current === generation) page.main.html('<div class="purchase-empty">读取失败，请点击“刷新进度”重试，或切换其他采购申请。</div>');
		}
	}
	page.add_inner_button(__("刷新进度"), load);
	page.main.on("click", "[data-receive-purchase-order]", (event) => {
		frappe.model.open_mapped_doc({ method: "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt", source_name: event.currentTarget.dataset.receivePurchaseOrder });
	});
	page.add_inner_button(__("采购申请列表"), () => frappe.set_route("purchase-supplier-allocation"));
	page.add_inner_button(__("缺料采购"), () => frappe.set_route("shortage-purchase-planning"));
	wrapper.load_purchase_allocation = load;
};
frappe.pages["purchase-supplier-allocation"].on_page_show = (wrapper) => wrapper.load_purchase_allocation();
}
