function warehouseBatchHtml(item, esc, number, translate = (value) => value) {
	if (!item.has_batch_no) return "";
	const groups = [["批次", item.batches], ["拒收批次", item.rejected_batches]];
	const details = groups.flatMap(([label, lots]) => (lots || []).map((lot) =>
		'<small>' + label + '：' + esc(lot.batch_no) + ' · ' + number(lot.qty) + ' ' + esc(translate(lot.stock_uom || "")) + '</small>'
	)).join("");
	return '<div class="warehouse-batches"><small>按批次管理 · 在原单核对实际批次</small>' + details + '</div>';
}

function warehouseBatchNavigationHtml(model, esc) {
	const reports = (model?.reports || []).map((report) =>
		'<a class="btn btn-default" href="/desk/query-report/' + encodeURIComponent(report.name) + '">' + esc(report.label) + '</a>'
	).join(" ");
	const configuration = model?.can_configure ?
		'<details><summary>批次管理（可选）</summary><p>普通物料无需设置。需要使用时，在库存设置启用，再对指定物料勾选“有批次”。可手动建立批次，也可按需配置自动编号；已有库存的物料需先按原生规则处理。</p>' +
		'<a href="/desk/stock-settings">库存设置</a> · <a href="/desk/item">物料配置</a></details>' : "";
	return reports + configuration;
}

function warehouseDocumentHtml(row, queue, esc, number, translate = (value) => value) {
	const slug = row.doctype.toLowerCase().replaceAll(" ", "-");
	const href = "/desk/" + slug + "/" + encodeURIComponent(row.name);
	const items = (row.items || []).map((item) => {
		const warehouses = item.warehouse || [item.source_warehouse, item.target_warehouse].filter(Boolean).join(" → ");
		return '<li><div><strong>' + esc(item.item_name) + '</strong><small>' + esc(item.item_code) +
			(warehouses ? " · " + esc(warehouses) : "") + '</small>' + warehouseBatchHtml(item, esc, number, translate) + '</div><span>' +
			(queue === "purchase" ? "待收 " : "") + number(item.qty) + " " + esc(translate(item.uom || "")) + '</span></li>';
	});
	const detail = items.length > 3 ? '<details><summary>另外 ' + (items.length - 3) +
		' 项物料</summary><ul class="warehouse-items">' + items.slice(3).join("") + '</ul></details>' : "";
	const title = row.party || row.work_order || row.purpose || row.name;
	const actionLabels = { purchase: "核对采购收货", receipt: "核对收货入库", issue: "核对生产发料", manufacture: "核对完工入库", delivery: "核对客户发货" };
	const action = row.can_write ? (row.is_return ? "核对退货" : actionLabels[queue] || "核对单据并提交") : "查看单据";
	const openAction = '<div class="warehouse-document-action"><a class="btn btn-primary" href="' + href + '">' + action + '</a></div>';
	return '<article class="warehouse-document"><div class="warehouse-document-heading"><div><h3>' +
		esc(title) + '</h3><a href="' + href + '">' + esc(row.name) + '</a></div><span class="warehouse-stage">' +
		(row.is_return ? "退货草稿" : queue === "purchase" ? "待收货" : "待核对提交") +
		'</span></div><p class="warehouse-document-meta">' + esc(row.company) + " · " + esc(row.date || "") +
		(row.due_date ? ' · <span class="' + (row.overdue ? "warehouse-overdue" : "") + '">' +
			(row.overdue ? "已过预计到货日 " : "预计到货 ") + esc(row.due_date) + '</span>' : "") +
		'</p>' + openAction + '<ul class="warehouse-items">' + items.slice(0, 3).join("") + '</ul>' + detail + '</article>';
}

function warehouseActionSummaryHtml(model, esc, number) {
	if (model?.groups) {
		const active = model.groups.filter((group) => (group.actions || []).some((action) =>
			action.status !== "unavailable" && (action.status !== "ready" || action.has_pending || action._refresh_stale)));
		if (!active.length) return warehouseActionSummaryHtml({ actions: model.groups.flatMap((group) => group.actions || []) }, esc, number);
		return active.map((group) => '<section class="warehouse-company-actions"><h2>' + esc(group.company) + '</h2>' +
			warehouseActionSummaryHtml(group, esc, number) + '</section>').join("");
	}
	const definitions = {
		shortage: { title: "缺料待处理", route: "shortage-purchase-planning", action: "查看缺料并处理",
			hint: "核对当前缺口并安排采购，已由库存或采购覆盖的数量会自动扣除。" },
		purchase_followup: { title: "采购申请待跟进", route: "purchase-supplier-allocation", action: "继续采购跟进",
			hint: "继续分配供应商，或跟进采购单提交。已下单部分可在“采购待收货”中处理。" },
	};
	const actions = (model?.actions || []).filter((action) => definitions[action.key] && action.status !== "unavailable");
	if (!actions.length) return "";
	const cards = actions.map((action) => {
		const definition = definitions[action.key];
		if (action.status !== "ready" || action._refresh_stale) {
			const message = action.status === "pending" || action._refresh_stale ? "正在核对最新数据…" : "读取失败，请点击刷新重试。";
			return '<article class="warehouse-action-card is-unresolved"><h3>' + definition.title + '</h3><p>' + message + '</p></article>';
		}
		if (!action.has_pending) return "";
		const query = new URLSearchParams();
		if (model.company) query.set("company", model.company);
		if (action.key === "purchase_followup") query.set("view", "to_order");
		const href = "/desk/" + definition.route + (query.size ? "?" + query.toString() : "");
		const count = action.key === "shortage" ? number(action.material_count) + " 项物料" : number(action.request_count) + " 张申请";
		const orders = action.key === "shortage" && action.sales_order_count > 0 ? " · 涉及 " + number(action.sales_order_count) + " 张销售订单" : "";
		return '<article class="warehouse-action-card"><div><h3>' + definition.title + '</h3><p class="warehouse-action-count"><strong>' +
			esc(count) + '</strong>' + esc(orders) + '</p><p class="warehouse-action-hint">' + definition.hint + '</p></div>' +
			'<a class="btn btn-primary" href="' + esc(href) + '">' + definition.action + '</a></article>';
	}).filter(Boolean).join("");
	const clear = new Set(actions.map((action) => action.key)).size === 2 ? "当前没有缺料或待下单事项。" :
		(actions[0].key === "shortage" ? "当前没有缺料待处理。" : "当前没有采购申请待下单 / 确认。");
	return cards ? '<section class="warehouse-action-summary" aria-label="采购待办">' + cards + '</section>' :
		'<p class="warehouse-actions-clear">' + clear + '</p>';
}

function warehouseWorkbenchRequests(state) {
	return {
		workbench: { method: "process_simplification.api.warehouse.get_workbench",
			args: { company: state.company, queue: state.queue, search: state.search, start: state.cursors.at(-1) } },
		actions: { method: "process_simplification.api.warehouse.get_action_summary", args: { company: state.company } },
	};
}

if (typeof module !== "undefined" && module.exports) module.exports = {
	warehouseDocumentHtml, warehouseBatchHtml, warehouseBatchNavigationHtml, warehouseActionSummaryHtml, warehouseWorkbenchRequests,
};

if (typeof frappe !== "undefined") {
	frappe.pages["warehouse-workbench"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({ parent: wrapper, title: __("库房工作台"), single_column: true });
		const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
		const number = (value) => Number(value || 0).toLocaleString("zh-CN", { maximumFractionDigits: 6 });
		const root = $('<div class="warehouse-workbench">' +
			'<p class="text-muted">先处理待办，也可以直接查询库存和收发记录。</p>' +
				'<nav class="warehouse-shortcuts" aria-label="库存查询与记录"></nav>' +
				'<div class="warehouse-batch-navigation"></div>' +
			'<div class="warehouse-filters"><div><label for="warehouse-company">公司</label>' +
				'<select id="warehouse-company" class="form-control"><option value="">全部可访问公司</option></select></div>' +
				'<div><label for="warehouse-search">查找库存单据</label><input id="warehouse-search" class="form-control" ' +
				'placeholder="单号、物料、供应商或客户" type="search"></div></div>' +
			'<div class="warehouse-preparation" aria-live="polite"></div>' +
			'<h2 class="warehouse-queue-heading">库存单据待办</h2>' +
			'<nav class="warehouse-queues" aria-label="库房待办类型"></nav>' +
			'<div class="warehouse-results" aria-live="polite"></div><div class="warehouse-pagination"></div></div>').appendTo(page.main);
		const state = { company: "", companies: [], queue: null, search: "", cursors: [0], next: null, generation: 0 };
		let searchTimer;
		const shortcuts = [
			["库存余额", "query-report", "Stock Balance"],
			["库存流水", "query-report", "Stock Ledger"],
			["收发记录", "List", "Stock Entry"],
			["采购收货记录", "List", "Purchase Receipt"],
			["发货记录", "List", "Delivery Note"],
			["退料与报废处理", "production-exception-review"],
		];
		shortcuts.forEach(([label, type, name]) => {
			if (type === "query-report" && !frappe.boot.allowed_reports?.[name]) return;
			if (type === "List" && !frappe.model.can_read(name)) return;
			if (type === "production-exception-review" && !frappe.boot.page_info?.[type]) return;
			$('<button type="button" class="btn btn-default">').text(__(label)).appendTo(root.find(".warehouse-shortcuts"))
				.on("click", () => {
					const company = state.company || (type === "query-report" ? state.companies[0] : "");
					const filters = company ? { company } : {};
					return type === "List" ? frappe.set_route("List", name, filters) :
						type === "query-report" ? frappe.set_route(type, name, filters) : frappe.set_route(type);
				});
		});
		async function load(reset = false, options = {}) {
			if (reset) state.cursors = [0];
			const generation = ++state.generation;
			root.attr("aria-busy", "true");
			root.find(".warehouse-shortcuts button").prop("disabled", true);
			if (!options.background) {
				root.find(".warehouse-preparation").html('<p class="text-muted">正在核对采购待办…</p>');
				root.find(".warehouse-results").html('<p class="text-muted warehouse-empty">正在读取库房待办…</p>');
				root.find(".warehouse-pagination").empty();
			}
			try {
				return await frappe.ps_read_page(page, {
					requests: warehouseWorkbenchRequests(state),
					background: options.background,
					apply(response) {
					const model = response.message.workbench;
					root.find(".warehouse-preparation").html(warehouseActionSummaryHtml(response.message.actions, esc, number));
					root.find(".warehouse-batch-navigation").html(warehouseBatchNavigationHtml(model.batch_navigation, esc));
				state.companies = model.companies;
				if (model.companies.length === 1) state.company = model.companies[0];
				state.queue = model.queue;
				state.next = model.next_start;
				const company = root.find("#warehouse-company").empty();
				$("<option>").val("").text("全部可访问公司").appendTo(company);
				model.companies.forEach((name) => $("<option>").val(name).text(name).appendTo(company));
				company.val(state.company);
				company.parent().toggle(model.companies.length > 1);
				root.find(".warehouse-filters").toggleClass("single-company", model.companies.length === 1);
				root.find(".warehouse-shortcuts button").prop("disabled", false);
				root.find(".warehouse-queues").html(model.categories.map((category) =>
					'<button type="button" class="warehouse-queue ' + (category.key === model.queue ? "is-active" : "") +
					'" aria-pressed="' + (category.key === model.queue) + '" data-queue="' + esc(category.key) +
					'"><strong>' + esc(category.label) + '</strong><span class="' + (category.has_pending ? "has-pending" : "") +
					'">' + (category.has_pending ? "有待办" : "暂无待办") + '</span></button>'
				).join(""));
				root.find(".warehouse-results").html(model.rows.length ?
					model.rows.map((row) => warehouseDocumentHtml(row, model.queue, esc, number, __)).join("") :
					'<div class="warehouse-empty">' + (state.search ? "没有匹配的待办，请调整关键词或待办类型。" :
						"此类暂无待处理单据。已提交记录可从上方查询入口查看。") + '</div>');
				root.find(".warehouse-pagination").html(
					'<button type="button" class="btn btn-default" data-page="previous"' +
					(state.cursors.length === 1 ? " disabled" : "") + '>上一页</button><span>第 ' + state.cursors.length +
					' 页 · 本页 ' + model.rows.length + ' 张</span><button type="button" class="btn btn-default" data-page="next"' +
					(!model.has_more ? " disabled" : "") + '>下一页</button>');
					},
				});
			} catch (error) {
				if (generation !== state.generation) return;
				root.find(".warehouse-preparation").html('<p class="warehouse-actions-error">采购待办未能更新，请点击刷新重试。</p>');
				if (options.background) throw error;
				root.find(".warehouse-results").html('<div class="warehouse-empty text-danger">待办读取失败，请检查权限或点击刷新重试。</div>');
			} finally {
				if (generation === state.generation) root.attr("aria-busy", "false");
			}
		}
		root.on("click", "[data-queue]", (event) => { state.queue = $(event.currentTarget).attr("data-queue"); load(true); });
		root.on("change", "#warehouse-company", (event) => { state.company = event.target.value; state.queue = null; load(true); });
		root.on("input", "#warehouse-search", (event) => {
			state.search = event.target.value.trim();
			clearTimeout(searchTimer);
			searchTimer = setTimeout(() => load(true), 300);
		});
		root.on("click", "[data-page]", (event) => {
			if ($(event.currentTarget).attr("data-page") === "next") {
				if (state.next === null) return;
				state.cursors.push(state.next);
			} else if (state.cursors.length > 1) state.cursors.pop();
			load();
		});
		page.add_inner_button(__("刷新"), () => load(true));
		page.warehouseWorkbench = { refresh: () => load(true), backgroundRefresh: (options) => load(false, options) };
	};
	frappe.pages["warehouse-workbench"].refresh = (wrapper) => wrapper.page.warehouseWorkbench.refresh();
}
