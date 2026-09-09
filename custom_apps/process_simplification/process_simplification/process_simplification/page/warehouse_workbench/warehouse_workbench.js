function warehouseDocumentHtml(row, queue, esc, number) {
	const slug = row.doctype.toLowerCase().replaceAll(" ", "-");
	const href = "/desk/" + slug + "/" + encodeURIComponent(row.name);
	const items = (row.items || []).map((item) => {
		const warehouses = item.warehouse || [item.source_warehouse, item.target_warehouse].filter(Boolean).join(" → ");
		return '<li><div><strong>' + esc(item.item_name) + '</strong><small>' + esc(item.item_code) +
			(warehouses ? " · " + esc(warehouses) : "") + '</small></div><span>' +
			(queue === "purchase" ? "待收 " : "") + number(item.qty) + " " + esc(item.uom || "") + '</span></li>';
	});
	const detail = items.length > 3 ? '<details><summary>另外 ' + (items.length - 3) +
		' 项物料</summary><ul class="warehouse-items">' + items.slice(3).join("") + '</ul></details>' : "";
	const title = row.party || row.work_order || row.purpose || row.name;
	const action = row.can_write ? (queue === "purchase" ? "打开采购单收货" : "打开处理") : "查看单据";
	const openAction = '<div class="warehouse-document-action"><a class="btn btn-primary" href="' + href + '">' + action + '</a></div>';
	return '<article class="warehouse-document"><div class="warehouse-document-heading"><div><h3>' +
		esc(title) + '</h3><a href="' + href + '">' + esc(row.name) + '</a></div><span class="warehouse-stage">' +
		(row.is_return ? "退货草稿" : queue === "purchase" ? "待收货" : "待核对提交") +
		'</span></div><p class="warehouse-document-meta">' + esc(row.company) + " · " + esc(row.date || "") +
		(row.due_date ? ' · <span class="' + (row.overdue ? "warehouse-overdue" : "") + '">' +
			(row.overdue ? "已过预计到货日 " : "预计到货 ") + esc(row.due_date) + '</span>' : "") +
		'</p>' + openAction + '<ul class="warehouse-items">' + items.slice(0, 3).join("") + '</ul>' + detail + '</article>';
}

if (typeof module !== "undefined" && module.exports) module.exports = { warehouseDocumentHtml };

if (typeof frappe !== "undefined") {
	frappe.pages["warehouse-workbench"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({ parent: wrapper, title: __("库房工作台"), single_column: true });
		const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
		const number = (value) => Number(value || 0).toLocaleString("zh-CN", { maximumFractionDigits: 6 });
		const root = $('<div class="warehouse-workbench">' +
			'<p class="text-muted">先处理待办，也可以直接查询库存和收发记录。</p>' +
			'<nav class="warehouse-shortcuts" aria-label="库存查询与记录"></nav>' +
			'<div class="warehouse-filters"><div><label for="warehouse-company">公司</label>' +
			'<select id="warehouse-company" class="form-control"><option value="">全部可访问公司</option></select></div>' +
			'<div><label for="warehouse-search">查找待办</label><input id="warehouse-search" class="form-control" ' +
			'placeholder="单号、物料、供应商或客户" type="search"></div></div>' +
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
				root.find(".warehouse-results").html('<p class="text-muted warehouse-empty">正在读取库房待办…</p>');
				root.find(".warehouse-pagination").empty();
			}
			try {
				return await frappe.ps_read_page(page, {
					method: "process_simplification.api.warehouse.get_workbench",
					background: options.background,
					args: { company: state.company, queue: state.queue, search: state.search, start: state.cursors.at(-1) },
					apply(response) {
				const model = response.message;
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
					model.rows.map((row) => warehouseDocumentHtml(row, model.queue, esc, number)).join("") :
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
				if (options.background) throw error;
				if (generation !== state.generation) return;
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
