const shortageItemIdentity = typeof module !== "undefined" && module.exports
	? require("../../../public/js/item_identity.js")
	: window.process_simplification.item_identity;

function shortageDocumentLink(doctype, name, helpers) {
	if (!name) return "";
	const esc = helpers.escapeHtml;
	const route = encodeURIComponent(String(name));
	const slug = String(doctype || "").toLowerCase().replaceAll(" ", "-");
	return `<a href="/app/${slug}/${route}">${esc(name)}</a>`;
}

function shortageSourceHtml(source, helpers) {
	const esc = helpers.escapeHtml;
	const fmt = helpers.formatQty;
	const translate = helpers.translate || ((message) => message);
	const order = shortageDocumentLink("Sales Order", source.sales_order, helpers);
	const productionPlanLink = shortageDocumentLink("Production Plan", source.production_plan, helpers);
	const workOrderLink = shortageDocumentLink("Work Order", source.work_order, helpers);
	const productionPlan = productionPlanLink
		? `<span>${esc(translate("生产计划"))} ${productionPlanLink}</span>`
		: "";
	const workOrder = workOrderLink
		? `<span>${esc(translate("工单"))} ${workOrderLink}</span>`
		: "";
	const finishedItem = source.finished_item
		? `<span>${esc(translate("成品"))} ${esc(source.finished_item)}</span>`
		: "";
	const production = [productionPlan, workOrder].filter(Boolean).join(" · ");

	return `
		<div class="shortage-source-item">
			<div class="shortage-source-primary">${order || productionPlanLink || workOrderLink || finishedItem || esc(translate("生产需求"))}</div>
			<div class="shortage-source-meta">
				${[finishedItem, production].filter(Boolean).join(" · ")}
				<span>${esc(translate("需用"))} ${fmt(source.required_qty)}</span>
			</div>
		</div>
	`;
}

function shortageSourcesHtml(sources, helpers) {
	const rows = sources || [];
	const visible = rows.slice(0, 3).map((source) => shortageSourceHtml(source, helpers)).join("");
	if (rows.length <= 3) return visible;
	const rest = rows.slice(3).map((source) => shortageSourceHtml(source, helpers)).join("");
	return `${visible}<details class="shortage-more-sources"><summary>${helpers.escapeHtml(
		helpers.translate("另")
	)} ${rows.length - 3} ${helpers.escapeHtml(helpers.translate("个来源"))}</summary>${rest}</details>`;
}

function purchaseQuantityPrecision(row) {
	const precision = Number(row.quantity_precision ?? 2);
	return Number.isInteger(precision) && precision >= 0 && precision <= 9 ? precision : 2;
}

function normalizedPurchaseQuantity(value, row) {
	const number = Number(value ?? 0);
	return Number.isFinite(number) ? Number(number.toFixed(purchaseQuantityPrecision(row))) : 0;
}

function preparePurchaseRows(rows) {
	return (rows || []).filter((row) => normalizedPurchaseQuantity(row.shortage_qty, row) > 0)
		.map((row) => ({ ...row, purchase_qty: normalizedPurchaseQuantity(row.purchase_qty ?? row.shortage_qty, row) }));
}

function shortageRowsHtml(rows, helpers) {
	const esc = helpers.escapeHtml;
	const translate = helpers.translate || ((message) => message);
	return (rows || []).map((row, index) => {
		const rowIndex = row._row_index === undefined ? index : row._row_index;
		const precision = purchaseQuantityPrecision(row);
		const fmt = (value) => helpers.formatQty(value, precision);
		const purchaseQty = normalizedPurchaseQuantity(row.purchase_qty ?? row.shortage_qty, row).toFixed(precision);
		const selected = row._selected !== false;
		return `
			<tr data-index="${rowIndex}" class="${selected ? "is-selected" : ""}">
				<td class="shortage-check-cell" data-label="${esc(translate("选择"))}">
					<label class="shortage-check" title="${esc(translate("选择此物料"))}">
						<input type="checkbox" class="shortage-select" ${selected ? "checked" : ""}>
						<span></span>
					</label>
				</td>
				<td class="shortage-material-cell" data-label="${esc(translate("物料"))}">
					${shortageItemIdentity.itemIdentityHtml(
						row.item_code,
						row.item_name,
						{ translate, escapeHtml: esc },
						{ linkToItem: true }
					)}
					${row.stock_uom ? `<span class="shortage-uom">${esc(row.stock_uom)}</span>` : ""}
				</td>
				<td class="shortage-warehouse-cell" data-label="${esc(translate("来源仓"))}">
					<span class="shortage-warehouse">${esc(row.warehouse || translate("未配置"))}</span>
				</td>
				<td class="shortage-coverage-cell" data-label="${esc(translate("缺料测算"))}">
					<div class="shortage-coverage-grid">
						<span><small>${esc(translate("总需求"))}</small><strong>${fmt(row.required_qty)}</strong></span>
						<span><small>${esc(translate("可用库存"))}</small><strong>${fmt(row.available_qty)}</strong></span>
						<span><small>${esc(translate("采购申请"))}</small><strong>${fmt(row.open_material_request_qty)}</strong></span>
						<span><small>${esc(translate("采购订单"))}</small><strong>${fmt(row.open_purchase_order_qty)}</strong></span>
						<span class="shortage-gap"><small>${esc(translate("仍需采购"))}</small><strong>${fmt(row.shortage_qty)}</strong></span>
					</div>
				</td>
				<td class="shortage-purchase-cell" data-label="${esc(translate("本次采购"))}">
					<div class="shortage-qty-editor">
						<input class="form-control text-right purchase-qty" type="number" min="0" step="${(10 ** -precision).toFixed(precision)}" value="${purchaseQty}">
						${row.stock_uom ? `<span>${esc(row.stock_uom)}</span>` : ""}
					</div>
					<small>${esc(translate("建议按剩余缺口采购"))}</small>
				</td>
				<td class="shortage-sources-cell" data-label="${esc(translate("需求来源"))}">
					${shortageSourcesHtml(row.sources, { ...helpers, formatQty: fmt, translate })}
				</td>
			</tr>
		`;
	}).join("");
}

function shortageSummary(rows) {
	const salesOrders = new Set();
	const warehouses = new Set();
	for (const row of rows || []) {
		if (row.warehouse) warehouses.add(row.warehouse);
		for (const source of row.sources || []) {
			if (source.sales_order) salesOrders.add(source.sales_order);
		}
	}
	return {
		itemCount: (rows || []).length,
		salesOrderCount: salesOrders.size,
		warehouseCount: warehouses.size,
	};
}

function filterShortageRows(rows, search) {
	const needle = String(search || "").trim().toLocaleLowerCase();
	if (!needle) return rows || [];
	return (rows || []).filter((row) => {
		const sourceText = (row.sources || []).map((source) => [
			source.sales_order,
			source.sales_order_item,
			source.production_plan,
			source.work_order,
			source.finished_item,
		].filter(Boolean).join(" ")).join(" ");
		return [row.item_code, row.item_name, row.warehouse, sourceText]
			.filter(Boolean)
			.join(" ")
			.toLocaleLowerCase()
			.includes(needle);
	});
}

function canCreateMaterialRequest(model) {
	return Boolean(model && typeof model.can_create === "function" && model.can_create("Material Request"));
}

function shortagePageHtml(helpers) {
	const esc = helpers.escapeHtml;
	const translate = helpers.translate;
	return `
		<div class="process-simplification-page shortage-purchase-planning">
			<section class="shortage-hero">
				<div>
					<span class="shortage-eyebrow">${esc(translate("采购执行台"))}</span>
					<h2>${esc(translate("当前待采购缺料"))}</h2>
					<p>${esc(translate("打开即汇总全部未完成生产需求（含未排产订单），并扣除可用库存、在途采购申请和采购订单。"))}</p>
				</div>
				<button type="button" class="btn btn-default shortage-refresh" data-action="refresh">
					<span aria-hidden="true">↻</span> ${esc(translate("刷新缺料"))}
				</button>
			</section>

			<section class="shortage-summary" aria-live="polite">
				<div><span>${esc(translate("待采购物料"))}</span><strong data-summary="items">—</strong></div>
				<div><span>${esc(translate("本次已选"))}</span><strong data-summary="selected">—</strong></div>
				<div><span>${esc(translate("关联销售订单"))}</span><strong data-summary="orders">—</strong></div>
				<div><span>${esc(translate("涉及来源仓"))}</span><strong data-summary="warehouses">—</strong></div>
			</section>

			<section class="shortage-panel">
				<div class="shortage-toolbar">
					<div class="shortage-search-wrap">
						<span aria-hidden="true">⌕</span>
						<input class="form-control shortage-search" type="search" placeholder="${esc(translate("搜索物料、订单、工单或仓库"))}">
					</div>
					<div class="shortage-date-field" data-field="schedule_date"></div>
					<label class="shortage-select-all-wrap">
						<input type="checkbox" class="select-all" checked>
						<span>${esc(translate("选择当前显示"))}</span>
					</label>
				</div>

				<div class="shortage-status"></div>
				<div class="shortage-table-wrap">
					<table class="table shortage-table">
						<thead><tr>
							<th class="shortage-check-cell"></th>
							<th>${esc(translate("物料"))}</th>
							<th>${esc(translate("来源仓"))}</th>
							<th>${esc(translate("缺料测算"))}</th>
							<th>${esc(translate("本次采购"))}</th>
							<th>${esc(translate("需求来源"))}</th>
						</tr></thead>
						<tbody></tbody>
					</table>
				</div>
			</section>

			<div class="shortage-safety-note">
				<span aria-hidden="true">✓</span>
				<div><strong>${esc(translate("提交前会再次核验"))}</strong><small>${esc(translate("系统会按最新库存与在途数量复核，防止重复采购；采购申请生成后自动提交。"))}</small></div>
			</div>
		</div>
	`;
}

const shortagePurchasePlanningApi = {
	normalizedPurchaseQuantity,
	preparePurchaseRows,
	shortageDocumentLink,
	shortageSourceHtml,
	shortageRowsHtml,
	shortageSummary,
	filterShortageRows,
	canCreateMaterialRequest,
	shortagePageHtml,
};

if (typeof module !== "undefined" && module.exports) {
	module.exports = shortagePurchasePlanningApi;
}

if (typeof frappe !== "undefined") {
frappe.pages["shortage-purchase-planning"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("缺料采购"),
		single_column: true,
	});

	page.main.html(shortagePageHtml({ translate: __, escapeHtml: frappe.utils.escape_html }));
	page.add_inner_button(__("已建采购申请／供应商分配"), () => frappe.set_route("purchase-supplier-allocation"));
	page.add_inner_button(__("到货通知记录"), () => frappe.set_route("purchase-receipt-notice"));
	const $root = page.main.find(".shortage-purchase-planning");
	const canCreate = canCreateMaterialRequest(frappe.model);
	const state = {
		rows: [],
		selectedIndexes: new Set(),
		search: "",
		loading: false,
		loaded: false,
	};

	const scheduleDateField = frappe.ui.form.make_control({
		parent: $root.find('[data-field="schedule_date"]'),
		df: {
			fieldname: "schedule_date",
			fieldtype: "Date",
			label: __("需要日期"),
			default: frappe.datetime.add_days(frappe.datetime.nowdate(), 1),
			reqd: 1,
		},
		render_input: true,
	});
	scheduleDateField.set_value(frappe.datetime.add_days(frappe.datetime.nowdate(), 1));

	function fmt(value, precision = 2) {
		return format_number(flt(value), null, precision);
	}

	function visibleEntries() {
		const visibleRows = new Set(filterShortageRows(state.rows, state.search));
		return state.rows
			.map((row, index) => ({ row, index }))
			.filter(({ row }) => visibleRows.has(row));
	}

	function selectedRows() {
		return state.rows.filter((row, index) => state.selectedIndexes.has(index)).map((row) => ({
			...row,
			purchase_qty: normalizedPurchaseQuantity(row.purchase_qty ?? row.shortage_qty, row),
			schedule_date: scheduleDateField.get_value(),
		}));
	}

	function updatePrimaryAction() {
		if (!canCreate) {
			page.clear_primary_action();
			return;
		}
		const count = state.selectedIndexes.size;
		page.set_primary_action(__("生成采购申请（{0} 项）", [count]), createMaterialRequest);
		page.btn_primary && page.btn_primary.prop("disabled", !count || state.loading);
	}

	function renderSummary() {
		const summary = shortageSummary(state.rows);
		$root.find('[data-summary="items"]').text(summary.itemCount);
		$root.find('[data-summary="selected"]').text(state.selectedIndexes.size);
		$root.find('[data-summary="orders"]').text(summary.salesOrderCount);
		$root.find('[data-summary="warehouses"]').text(summary.warehouseCount);
		updatePrimaryAction();
	}

	function renderStatus(kind, message) {
		const content = {
			loading: `<div class="shortage-state shortage-loading"><span class="shortage-spinner"></span><div><strong>${__("正在汇总全部缺料")}</strong><small>${__("正在核对已排产需求、库存和在途采购…")}</small></div></div>`,
			empty: `<div class="shortage-state shortage-empty"><span>✓</span><div><strong>${__("当前没有待采购缺料")}</strong><small>${__("当前需求已有库存、采购申请或采购订单覆盖。已有申请可从“已建采购申请／供应商分配”继续下单。")}</small></div></div>`,
			filtered: `<div class="shortage-state shortage-empty"><span>⌕</span><div><strong>${__("没有匹配结果")}</strong><small>${__("换一个物料、订单、工单或仓库关键词试试。")}</small></div></div>`,
			error: `<div class="shortage-state shortage-error"><span>!</span><div><strong>${__("缺料读取失败")}</strong><small>${frappe.utils.escape_html(message || __("请刷新重试。"))}</small></div><button type="button" class="btn btn-default" data-action="refresh">${__("重新读取")}</button></div>`,
		}[kind] || "";
		$root.find(".shortage-status").html(content);
	}

	function renderRows() {
		const entries = visibleEntries();
		$root.find("tbody").html(shortageRowsHtml(entries.map(({ row, index }) => ({
			...row,
			_row_index: index,
			_selected: state.selectedIndexes.has(index),
		})), {
			escapeHtml: frappe.utils.escape_html,
			formatQty: fmt,
			translate: __,
		}));
		$root.find(".shortage-table-wrap").toggle(Boolean(entries.length));
		if (state.loaded && !entries.length) renderStatus(state.rows.length ? "filtered" : "empty");
		else if (!state.loading) $root.find(".shortage-status").empty();

		const visibleIndexes = entries.map(({ index }) => index);
		const allVisibleSelected = Boolean(visibleIndexes.length)
			&& visibleIndexes.every((index) => state.selectedIndexes.has(index));
		$root.find(".select-all").prop("checked", allVisibleSelected);
		$root.find(".select-all").prop("indeterminate",
			!allVisibleSelected && visibleIndexes.some((index) => state.selectedIndexes.has(index))
		);
		renderSummary();
	}

	function loadAllShortages() {
		if (state.loading) return;
		state.loading = true;
		state.loaded = false;
		$root.find(".shortage-table-wrap").hide();
		$root.find('[data-action="refresh"]').prop("disabled", true);
		renderStatus("loading");
		renderSummary();
		frappe.call({
			method: "process_simplification.api.shortage.check_all_shortages",
		}).then((response) => {
			state.rows = preparePurchaseRows((response && response.message && response.message.shortages) || []);
			state.selectedIndexes = new Set(state.rows.map((_, index) => index));
			state.loaded = true;
			state.loading = false;
			$root.find('[data-action="refresh"]').prop("disabled", false);
			renderRows();
		}, (error) => {
			state.loading = false;
			state.loaded = false;
			$root.find('[data-action="refresh"]').prop("disabled", false);
			$root.find(".shortage-table-wrap").hide();
			renderStatus("error", error && (error.message || error.exc));
			renderSummary();
		});
	}

	function createMaterialRequest() {
		const rows = selectedRows();
		if (!rows.length) {
			frappe.msgprint(__("请至少选择一条缺料记录。"));
			return;
		}
		if (!scheduleDateField.get_value()) {
			frappe.msgprint(__("请选择需要日期。"));
			return;
		}
		const invalid = rows.find((row) => flt(row.purchase_qty) <= 0);
		if (invalid) {
			frappe.msgprint(__("物料 {0} 的本次采购数量必须大于 0。", [invalid.item_code]));
			return;
		}
		frappe.confirm(
			__("确认按当前 {0} 项物料生成并提交采购申请？提交前会再次核验最新缺口。", [rows.length]),
			() => {
				frappe.call({
					method: "process_simplification.api.shortage.create_material_request",
					type: "POST",
					args: { shortage_rows: rows, schedule_date: scheduleDateField.get_value() },
					freeze: true,
					freeze_message: __("正在复核并生成采购申请…"),
				}).then((response) => {
					const materialRequest = response && response.message && response.message.material_request;
					if (materialRequest) frappe.set_route("purchase-supplier-allocation", { material_request: materialRequest });
				});
			}
		);
	}

	$root.on("input", ".shortage-search", (event) => {
		state.search = event.currentTarget.value || "";
		renderRows();
	});
	$root.on("change", ".shortage-select", (event) => {
		const $row = $(event.currentTarget).closest("tr");
		const index = cint($row.data("index"));
		if ($(event.currentTarget).prop("checked")) state.selectedIndexes.add(index);
		else state.selectedIndexes.delete(index);
		$row.toggleClass("is-selected", state.selectedIndexes.has(index));
		renderRows();
	});
	$root.on("change", ".select-all", (event) => {
		const checked = $(event.currentTarget).prop("checked");
		for (const { index } of visibleEntries()) {
			if (checked) state.selectedIndexes.add(index);
			else state.selectedIndexes.delete(index);
		}
		renderRows();
	});
	$root.on("input change", ".purchase-qty", (event) => {
		const index = cint($(event.currentTarget).closest("tr").data("index"));
		if (state.rows[index]) state.rows[index].purchase_qty = flt(event.currentTarget.value);
	});
	$root.on("click", '[data-action="refresh"]', loadAllShortages);

	wrapper.shortage_purchase_planning = { state, loadAllShortages };
	renderSummary();
	loadAllShortages();
};

}
