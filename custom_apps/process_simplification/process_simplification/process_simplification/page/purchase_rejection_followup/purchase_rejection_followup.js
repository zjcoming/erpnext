function rejectionScope(options, search) {
	const query = new URLSearchParams(search || "");
	return {
		company: options?.company ?? query.get("company") ?? "",
		receipt: options?.receipt ?? query.get("receipt") ?? "",
		view: (options?.view ?? query.get("view")) === "all" ? "all" : "pending",
	};
}

function rejectionRoute({ company, receipt, view } = {}) {
	const query = new URLSearchParams();
	if (company) query.set("company", company);
	if (receipt) query.set("receipt", receipt);
	if (view === "all") query.set("view", view);
	return "/desk/purchase-rejection-followup" + (query.size ? "?" + query : "");
}

function rejectionDocumentLink(doctype, name, label, esc) {
	if (!name) return "";
	return '<a href="/desk/' + doctype + '/' + encodeURIComponent(name) + '">' + esc(label || name) + '</a>';
}

function rejectionReturnAction(row) {
	const drafts = (row.return_drafts || []).filter(Boolean);
	if (drafts.length) return { type: "draft", name: drafts[0] };
	if (row.can_return && (row.items || []).some((item) => Number(item.pending_return_qty) > 0)) {
		return { type: "create", name: row.name };
	}
	return { type: "none" };
}

function rejectionItemHtml(item, { esc, number, translate = (value) => value }, detail = false) {
	const quantity = (value) => esc(number(value)) + ' ' + esc(translate(item.stock_uom || ""));
	const quantities = [["合格入库", item.accepted_qty], ["原拒收", item.rejected_qty],
		["已退拒收", item.returned_rejected_qty], ["拒收待退", item.pending_return_qty]];
	const links = detail ? [
		rejectionDocumentLink("purchase-order", item.purchase_order, "原采购单 " + item.purchase_order, esc),
		rejectionDocumentLink("material-request", item.material_request, "原采购申请 " + item.material_request, esc),
	].filter(Boolean).join(" · ") : "";
	return '<li class="rejection-item"><div><strong>' + esc(item.item_name || item.item_code) + '</strong><small>' +
		esc(item.item_code) + '</small></div><dl class="rejection-quantities">' + quantities.map(([label, value]) =>
		'<div' + (label === "拒收待退" && Number(value) > 0 ? ' class="rejection-pending"' : "") + '><dt>' + label + '</dt><dd>' + quantity(value) + '</dd></div>'
	).join("") + '</dl>' + (detail ? '<div class="rejection-item-meta"><p>合格仓：' + esc(item.warehouse || "未设置") +
		' · 拒收仓：' + esc(item.rejected_warehouse || "未设置") + '</p>' + (links ? '<p>' + links + '</p>' : "") +
		(item.purchase_order ? '<p>原采购单状态：' + esc(translate(item.order_status || "—")) + ' · 订单待收：' + quantity(item.order_pending_qty) + '</p>' : "") + '</div>' : "") + '</li>';
}

function rejectionReceiptHtml(row, helpers, detail = false, scope = {}) {
	const { esc } = helpers;
	const pending = (row.items || []).some((item) => Number(item.pending_return_qty) > 0);
	const drafts = row.return_drafts || [];
	const stage = drafts.length ? "有退货草稿，待核对提交" : pending ? "拒收品待退回" : "拒收品已退回";
	const title = rejectionDocumentLink("purchase-receipt", row.name, row.name, esc);
	return '<article class="rejection-receipt"><header><div><h2>' + esc(row.supplier || row.name) + '</h2>' + title +
		'<p class="text-muted">' + esc(row.company) + ' · ' + esc(row.date || "") + '</p></div><span class="rejection-stage">' +
		stage + '</span></header><ul class="rejection-items">' + (row.items || []).map((item) => rejectionItemHtml(item, helpers, detail)).join("") +
		'</ul>' + (detail ? "" : '<a class="btn btn-primary" href="' + esc(rejectionRoute({ ...scope, company: row.company, receipt: row.name })) + '">查看并处理</a>') + '</article>';
}

function rejectionShortageHtml(row, { esc, number, translate = (value) => value }) {
	if (row.shortage_error) return '<div class="rejection-error" role="alert">当前缺料未能核对，请刷新重试。不能据此判断物料已齐。</div>';
	if (!row.can_check_shortage) return '<p class="text-muted">当前账号无法核对生产缺口，请由有权限的负责人在缺料采购页面核对。</p>';
	const shortages = row.shortage_rows || [];
	const content = shortages.length ? '<ul class="rejection-shortages">' + shortages.map((item) => '<li><strong>' +
		esc(item.item_name || item.item_code) + '</strong><span>' + esc(item.warehouse || "") + '</span><span>当前缺口 ' +
		esc(number(item.shortage_qty)) + ' ' + esc(translate(item.stock_uom || "")) + '</span></li>').join("") + '</ul>' :
		'<p>本次拒收涉及的物料，当前没有待补采缺口。其他到货或在途采购可能已覆盖需求，请同时核对实际齐套情况。</p>';
	const query = new URLSearchParams({ company: row.company || "" });
	return content + '<a class="btn btn-default" href="/desk/shortage-purchase-planning?' + esc(query.toString()) + '">' +
		(row.can_purchase ? "核对缺料并补采" : "查看当前缺料") + '</a>';
}

function rejectionContextHtml(row, helpers) {
	const { esc } = helpers;
	const action = rejectionReturnAction(row);
	const drafts = row.return_drafts || [];
	let button = "";
	if (action.type === "draft") button = '<a class="btn btn-primary" href="/desk/purchase-receipt/' +
		encodeURIComponent(action.name) + '">继续退货草稿</a>';
	if (action.type === "create") button = '<button type="button" class="btn btn-primary" data-rejection-return>登记拒收退货</button>';
	const hint = drafts.length ? '已有退货草稿。请先核对并提交；草稿尚未扣减拒收仓库存。' :
		action.type === "create" ? '将打开未保存的退货单，请核对数量、拒收仓和批次后保存、提交。' :
		(row.items || []).some((item) => Number(item.pending_return_qty) > 0) ? '当前账号不能登记退货，请由有权限的负责人处理。' : '拒收品已退回；继续核对供应商补送和实际生产缺口。';
	return '<section class="rejection-next"><h2>1. 处理拒收品退货</h2><p>' + hint + '</p>' + button +
		(drafts.length > 1 ? '<p>其他退货草稿：' + drafts.slice(1).map((name) => rejectionDocumentLink("purchase-receipt", name, name, esc)).join(" · ") + '</p>' : "") +
		'</section>' + rejectionReceiptHtml(row, helpers, true) +
		'<section class="rejection-next"><h2>2. 跟进补送或补采</h2><p>先退回拒收品；原供应商补送则从原采购单登记收货。不再补送，则由采购负责人关闭原采购单剩余量，再按实际缺口补采。</p>' +
		'<p>其他申请到货不回填本申请。退货、供应商补送和补采需要分别办理，系统不会自动创建补货采购单。</p>' +
		'<p class="text-muted">' + esc(row.current_demand_gap || "下方显示当前生产需求的缺口，可能与本次拒收数量不同。") + '</p>' +
		rejectionShortageHtml(row, helpers) + '</section>';
}

if (typeof module !== "undefined" && module.exports) module.exports = {
	rejectionScope, rejectionRoute, rejectionReturnAction, rejectionItemHtml, rejectionReceiptHtml, rejectionContextHtml, rejectionShortageHtml,
};

if (typeof frappe !== "undefined") {
	frappe.pages["purchase-rejection-followup"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({ parent: wrapper, title: __("拒收与补货跟进"), single_column: true });
		const api = "process_simplification.purchasing.rejections.";
		const helpers = { esc: (value) => frappe.utils.escape_html(String(value ?? "")), translate: __,
			number: (value) => Number(value || 0).toLocaleString("zh-CN", { maximumFractionDigits: 6 }) };
		const root = $('<div class="process-simplification-page rejection-followup"><style>' +
			'.rejection-followup{max-width:1100px;margin:0 auto;padding:16px 0}.rejection-followup h2{font-size:17px;margin:0 0 10px}.rejection-followup p{margin:8px 0}.rejection-followup a{overflow-wrap:anywhere}.rejection-filters{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}.rejection-search-field{flex:1;min-width:180px}.rejection-filters label{display:block;margin-bottom:6px}.rejection-receipt,.rejection-next{border:1px solid var(--border-color);border-radius:10px;padding:18px;margin:16px 0;background:var(--card-bg)}.rejection-next{background:var(--subtle-fg)}.rejection-receipt header{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}.rejection-stage{font-size:13px;white-space:nowrap;color:var(--text-muted)}.rejection-items{list-style:none;padding:0;margin:0 0 16px}.rejection-item{border-top:1px solid var(--border-color);padding:14px 0;overflow-wrap:anywhere}.rejection-item small{display:block;color:var(--text-muted);margin-top:3px}.rejection-quantities{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:12px 0 0}.rejection-quantities dt{font-weight:400;color:var(--text-muted);font-size:12px}.rejection-quantities dd{margin:4px 0 0;font-size:16px;font-weight:600}.rejection-pending dd{color:var(--orange-600,#a65b00)}.rejection-item-meta{font-size:13px;margin-top:12px}.rejection-shortages{list-style:none;padding:0}.rejection-shortages li{display:flex;gap:12px;flex-wrap:wrap;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border-color)}.rejection-pagination{display:flex;gap:14px;align-items:center;justify-content:center;margin:20px 0}.rejection-empty,.rejection-error{padding:20px 0}.rejection-error{color:var(--red-600,#b42318)}.rejection-followup .btn{white-space:normal;min-height:38px}.rejection-return-error:empty{display:none}@media(max-width:600px){.rejection-followup{padding:10px 0}.rejection-receipt,.rejection-next{padding:14px}.rejection-receipt header{display:block}.rejection-stage{display:block;margin-bottom:12px}.rejection-quantities{grid-template-columns:repeat(2,minmax(0,1fr))}.rejection-pagination{gap:10px;font-size:12px}.rejection-filters>div{width:100%}}' +
			'</style><p>拒收品先退回，再核对供应商补送或实际缺料补采。</p><div class="rejection-back"></div>' +
			'<div class="rejection-filters"><div class="rejection-company-field"></div><div><label for="rejection-view">查看范围</label>' +
			'<select id="rejection-view" class="form-control"><option value="pending">拒收待退回</option><option value="all">全部拒收记录</option></select></div>' +
			'<div class="rejection-search-field"><label for="rejection-search">查找收货单</label><input id="rejection-search" class="form-control" type="search" placeholder="单号、供应商或物料"></div></div>' +
			'<div class="rejection-results" aria-live="polite"></div><p class="rejection-return-error rejection-error" role="alert"></p><div class="rejection-pagination"></div></div>').appendTo(page.main);
		const state = { company: "", receipt: "", view: "pending", search: "", start: 0, history: [], next: null, model: null, generation: 0, loading: false, mapping: false, html: null };
		let changingCompany = false, searchTimer;
		const companyControl = frappe.ui.form.make_control({ parent: root.find(".rejection-company-field"), render_input: true,
			df: { label: __("公司"), fieldname: "rejection_company", fieldtype: "Link", options: "Company",
				placeholder: __("全部可访问公司"), change() {
					if (changingCompany) return;
					state.company = this.get_value() || "";
					load(true);
				} } });

		function renderResults(html) {
			if (state.html !== html) root.find(".rejection-results").html(html);
			state.html = html;
		}
		async function load(reset = false, options = {}) {
			if (options.background && (state.loading || state.mapping)) return false;
			if (reset) { state.start = 0; state.history = []; }
			window.history.replaceState(window.history.state, "", rejectionRoute(state));
			const generation = ++state.generation;
			state.loading = true;
			root.attr("aria-busy", "true");
			if (!options.background) {
				state.model = null;
				state.next = null;
				root.find(".rejection-return-error").empty();
				renderResults('<p class="rejection-empty text-muted">正在核对拒收和退货记录…</p>');
				root.find(".rejection-pagination").empty();
			}
			root.find(".rejection-filters").toggle(!state.receipt);
			root.find(".rejection-back").html(state.receipt ? '<a href="' + helpers.esc(rejectionRoute({ company: state.company, view: state.view })) + '">← 返回拒收记录</a>' : "");
			try {
				return await frappe.ps_read_page(page, { method: api + (state.receipt ? "get_context" : "get_page"),
					args: state.receipt ? { receipt: state.receipt } : { company: state.company, start: state.start, page_length: 20, search: state.search, view: state.view }, type: "GET", background: options.background,
					apply(response) {
				if (generation !== state.generation) return false;
				const model = response.message;
				if (!model || (state.receipt ? !model.name : !Array.isArray(model.rows))) throw new Error("Invalid rejection response");
				state.model = model;
				if (state.receipt) {
					renderResults(rejectionContextHtml(model, helpers));
				} else {
					state.next = model.next_start ?? null;
					renderResults(model.rows.length ? model.rows.map((row) => rejectionReceiptHtml(row, helpers, false, state)).join("") :
						'<p class="rejection-empty">' + (state.search ? "没有匹配的拒收记录，请调整关键词。" : state.view === "pending" ? "当前没有拒收品待退回。补送或补采请继续在采购跟进、缺料采购中核对。" : "当前没有拒收记录。") + '</p>');
					root.find(".rejection-pagination").html('<button type="button" class="btn btn-default" data-rejection-page="previous"' + (!state.history.length ? " disabled" : "") +
						'>上一页</button><span>第 ' + (state.history.length + 1) + ' 页 · 本页 ' + model.rows.length + ' 张</span><button type="button" class="btn btn-default" data-rejection-page="next"' +
						(state.next === null ? " disabled" : "") + '>下一页</button>');
				}
				return true;
					},
				});
			} catch (error) {
				if (generation !== state.generation) return false;
				state.model = null;
				state.next = null;
				root.find(".rejection-pagination").empty();
				renderResults('<div class="rejection-error" role="alert">拒收记录读取失败，请检查权限或点击刷新重试。暂时无法确认待办数量。</div>');
				if (options.background) throw error;
				return false;
			} finally {
				if (generation === state.generation) { state.loading = false; root.attr("aria-busy", "false"); }
			}
		}
		async function show() {
			clearTimeout(searchTimer);
			const scope = rejectionScope(frappe.route_options, window.location.search);
			frappe.route_options = null;
			Object.assign(state, scope, { search: "" });
			changingCompany = true;
			companyControl.set_input(state.company);
			changingCompany = false;
			root.find("#rejection-view").val(state.view);
			root.find("#rejection-search").val("");
			return load(true);
		}
		root.on("change", "#rejection-view", (event) => { state.view = event.target.value === "all" ? "all" : "pending"; load(true); });
		root.on("input", "#rejection-search", (event) => {
			state.search = event.target.value.trim();
			clearTimeout(searchTimer);
			searchTimer = setTimeout(() => load(true), 300);
		});
		root.on("click", "[data-rejection-page]", (event) => {
			if (state.loading) return;
			if ($(event.currentTarget).attr("data-rejection-page") === "next") {
				if (state.next === null) return;
				state.history.push(state.start); state.start = state.next;
			} else { if (!state.history.length) return; state.start = state.history.pop(); }
			load();
		});
		root.on("click", "[data-rejection-return]", async (event) => {
			if (state.loading || state.mapping || !state.model || rejectionReturnAction(state.model).type !== "create") return;
			state.mapping = true;
			const button = $(event.currentTarget).prop("disabled", true);
			try {
				await frappe.model.open_mapped_doc({ method: api + "make_rejected_return", source_name: state.model.name });
			} catch (error) {
				root.find(".rejection-return-error").text("退货单未能打开，请刷新后重试；已有草稿请先继续处理。");
			} finally { state.mapping = false; button.prop("disabled", false); }
		});
		page.add_inner_button(__("刷新"), () => load(true));
		page.rejection_refresh = (options) => load(false, options);
		wrapper.purchase_rejection_followup = { state, show, refresh: () => load(true) };
	};
	frappe.pages["purchase-rejection-followup"].on_page_show = (wrapper) => wrapper.purchase_rejection_followup.show();
}
