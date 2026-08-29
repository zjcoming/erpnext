function materialStatusMeta(status, translate = (message) => message) {
	const materialStatusCopy = {
		ready_now: { label: translate("当前可生产"), indicator: "green" },
		production_required: { label: translate("需生产"), indicator: "orange" },
		awaiting_purchase_receipt: { label: translate("待采购到货"), indicator: "blue" },
		purchase_request_pending: { label: translate("已提采购申请"), indicator: "orange" },
		new_purchase_required: { label: translate("需新增采购"), indicator: "red" },
		cannot_calculate: { label: translate("无法判断"), indicator: "gray" },
	};
	return materialStatusCopy[status] || materialStatusCopy.cannot_calculate;
}

function buildMaterialRiskView(result = {}) {
	const summary = Array.isArray(result.material_coverage) ? result.material_coverage : [];
	const summaryByMaterial = new Map(
		summary.map((material) => [`${material.item_code || ""}\u0000${material.warehouse || ""}`, material])
	);
	const groups = (Array.isArray(result.material_groups) ? result.material_groups : []).map((group) => {
		const materials = (Array.isArray(group.materials) ? group.materials : []).map((material) => {
			const aggregate = summaryByMaterial.get(
				`${material.item_code || ""}\u0000${material.warehouse || ""}`
			);
			return {
				...material,
				shared_inventory: Boolean(aggregate && (aggregate.sources || []).length > 1),
			};
		});
		return {
			...group,
			materials,
			has_production: materials.some((material) => material.status === "production_required"),
			has_shortage: materials.some((material) =>
				["new_purchase_required", "cannot_calculate"].includes(material.status)
			),
		};
	});
	return {
		groups,
		summary,
		shortages: Array.isArray(result.shortages) ? result.shortages : [],
		blockers: Array.isArray(result.blockers) ? result.blockers : [],
		checked_at: result.checked_at || "",
		zero_production: Number(result.production_required || 0) <= 0,
	};
}

function clearRowStaleLabels(root) {
	for (const element of root.find(".quick-stale-label").get()) element.remove();
}

function quickOrderPreviewArgs(items, defaults, deliveryDate) {
	return {
		items,
		company: defaults?.company,
		delivery_date: deliveryDate,
	};
}

function materialRiskHtml(view, helpers) {
	const translate = helpers.translate;
	const escape = (value) => helpers.escapeHtml(String(value ?? ""));
	const number = (value) => escape(helpers.formatNumber(Number(value || 0)));
	const label = (code, name) => {
		const safeCode = escape(code);
		const safeName = escape(name);
		return safeName && safeName !== safeCode
			? `<strong>${safeCode}</strong><small>${safeName}</small>`
			: `<strong>${safeCode}</strong>`;
	};
	const unit = (material) => (material.stock_uom ? ` <small>${escape(material.stock_uom)}</small>` : "");
	const statusPill = (status) => {
		const meta = materialStatusMeta(status, translate);
		return `<span class="indicator-pill ${meta.indicator}">${escape(meta.label)}</span>`;
	};
	const banner = view.stale
		? `<div class="quick-material-risk-banner is-stale" role="status">${escape(
				translate("订单已修改，以下结果仅供参考，请重新检查")
		  )}</div>`
		: view.checking
		? `<div class="quick-material-risk-banner is-checking" role="status">${escape(
				translate("正在重新检查")
		  )}</div>`
		: "";
	const blockers = view.blockers.length
		? `<div class="quick-material-risk-blockers" role="alert"><strong>${escape(
				translate("当前存在阻止下单的问题")
		  )}</strong><ul>${view.blockers
				.map((issue) => `<li>${escape(issue.message)}</li>`)
				.join("")}</ul></div>`
		: "";
	if (view.zero_production) {
		return `${banner}${blockers}<div class="quick-material-risk-zero">${escape(
			translate("当前成品库存可覆盖，本单无需展开生产物料")
		)}</div>`;
	}

	const groupCards = view.groups
		.filter((group) => Number(group.production_required || 0) > 0)
		.map((group) => {
			const rows = group.materials
				.map((material) => {
					const level = Math.max(Number(material.level || 1), 1);
					const isManufactured = material.supply_type === "manufactured";
					const handling = isManufactured
						? `<div class="quick-material-handling">${statusPill(material.status)}${Number(material.production_required_qty || 0) > 0 ? `<span>${escape(translate("需生产"))} ${number(material.production_required_qty)}${unit(material)}</span>` : ""}${material.bom_no ? `<small>${escape(translate("BOM"))}: ${escape(material.bom_no)}</small>` : ""}</div>`
						: `<div class="quick-material-handling">${statusPill(material.status)}${Number(material.shortage_qty || 0) > 0 ? `<span>${escape(translate("建议采购"))} ${number(material.shortage_qty)}${unit(material)}</span>` : ""}</div>`;
					return `
						<tr>
							<td class="quick-material-name"><div class="quick-material-tree-item" style="--quick-material-level: ${level - 1}"><small>${escape(translate("第 {0} 层", [level]))}</small>${label(material.item_code, material.item_name)}</div></td>
							<td>${escape(translate(isManufactured ? "生产件" : "采购件"))}</td>
							<td class="quick-material-number">${number(material.required_qty)}${unit(material)}</td>
							<td>${escape(material.warehouse || translate("未设置"))}</td>
							<td class="quick-material-number">${number(material.actual_qty)}</td>
							<td class="quick-material-number">${number(material.committed_qty)}</td>
							<td class="quick-material-number">${number(material.available_qty)}</td>
							<td class="quick-material-number">${number(material.current_gap_qty)}</td>
							<td>${handling}</td>
						</tr>`;
				})
				.join("");
			return `
				<details class="quick-material-group${group.has_production ? " has-production" : ""}${
					group.has_shortage ? " has-shortage" : ""
				}" data-material-group="${escape(group.row)}"${group.has_shortage || group.has_production ? " open" : ""}>
					<summary>
						<div class="quick-material-product">${label(group.item_code, group.item_name)}</div>
						<div class="quick-material-product-facts">
							<span>${escape(translate("订单数量"))} <strong>${number(group.qty)}</strong></span>
							<span>${escape(translate("可预留成品"))} <strong>${number(group.available_to_reserve)}</strong></span>
							<span>${escape(translate("需生产"))} <strong>${number(group.production_required)}</strong></span>
							<span>${escape(translate("成品仓"))} <strong>${escape(group.warehouse || translate("未设置"))}</strong></span>
							<span>${escape(translate("BOM"))} <strong>${escape(group.bom_no || translate("未设置"))}</strong></span>
						</div>
						<span class="quick-material-toggle">${escape(
							group.has_shortage || group.has_production
								? translate("收起层级")
								: translate("查看层级用料")
						)}</span>
					</summary>
					<p class="quick-material-scope-note">${escape(
						translate(
							"先检查直接依赖；半成品先使用现货，只对不足数量继续检查下一级 BOM。采购判断以下方底层采购物料汇总为准。"
						)
					)}</p>
					<div class="quick-material-table-wrap">
						<table class="table quick-material-table">
							<thead><tr>
				<th>${escape(translate("层级需求"))}</th><th>${escape(translate("供应方式"))}</th><th>${escape(
				translate("本层需求")
			)}</th><th>${escape(translate("来源仓库"))}</th><th>${escape(translate("账面库存"))}</th><th>${escape(
				translate("已占用")
			)}</th><th>${escape(translate("本单分配"))}</th><th>${escape(translate("本层缺口"))}</th><th>${escape(
				translate("处理方式")
			)}</th>
							</tr></thead>
							<tbody>${rows}</tbody>
						</table>
					</div>
				</details>`;
		})
		.join("");

	const summaryRows = view.summary
		.map(
			(material) => `
				<tr>
					<td class="quick-material-name">${label(material.item_code, material.item_name)}</td>
					<td>${escape(material.warehouse || translate("未设置"))}</td>
					<td class="quick-material-number">${number(material.required_qty)}${unit(material)}</td>
					<td class="quick-material-number">${number(material.actual_qty)}</td>
					<td class="quick-material-number">${number(material.committed_qty)}</td>
					<td class="quick-material-number">${number(material.available_qty)}</td>
					<td class="quick-material-number">${number(material.open_material_request_qty)}</td>
					<td class="quick-material-number">${number(material.open_purchase_order_qty)}</td>
					<td class="quick-material-number">${number(material.current_gap_qty)}</td>
					<td class="quick-material-number">${number(material.shortage_qty)}</td>
					<td>${statusPill(material.status)}</td>
				</tr>`
		)
		.join("");
	const summary = view.summary.length
		? `<section class="quick-material-procurement" aria-labelledby="quick-material-procurement-title">
			<div class="quick-material-summary-heading">
				<h4 id="quick-material-procurement-title">${escape(translate("底层采购物料汇总"))}</h4>
				<strong>${escape(translate("预计需新增采购 {0} 项", [view.shortages.length]))}</strong>
			</div>
			<p class="text-muted">${escape(translate("这里只汇总采购件；半成品缺口在上方标记为需生产。共享采购物料按物料和仓库合并计算。"))}</p>
			<div class="quick-material-table-wrap"><table class="table quick-material-table quick-material-summary-table">
				<thead><tr><th>${escape(translate("物料"))}</th><th>${escape(translate("来源仓库"))}</th><th>${escape(
				translate("本单需求")
		  )}</th><th>${escape(translate("账面"))}</th><th>${escape(translate("已占用"))}</th><th>${escape(
				translate("本单可用")
		  )}</th><th>${escape(translate("采购申请"))}</th><th>${escape(
				translate("按时在途")
			  )}</th><th>${escape(translate("即时采购缺口"))}</th><th>${escape(
				translate("建议新增申请")
		  )}</th><th>${escape(translate("结论"))}</th></tr></thead>
				<tbody>${summaryRows}</tbody>
			</table></div>
		</section>`
		: `<div class="quick-material-risk-empty">${escape(translate("当前没有需要采购的底层物料"))}</div>`;

	return `${banner}${blockers}<div class="quick-material-groups">${groupCards}</div>${summary}`;
}

function confirmationHtml(result, helpers) {
	const translate = helpers.translate;
	const escape = (value) => helpers.escapeHtml(String(value ?? ""));
	const number = (value) => escape(helpers.formatNumber(Number(value || 0)));
	const shortages = Array.isArray(result.shortages) ? result.shortages : [];
	const shortageRows = shortages
		.slice(0, 5)
		.map((material) => {
			const procurementCoverage =
				Number(material.open_material_request_qty || 0) +
				Number(material.open_purchase_order_qty || 0);
			return `<tr><td>${escape(material.item_code)}${
				material.item_name ? `<small>${escape(material.item_name)}</small>` : ""
			}</td><td>${escape(material.warehouse || translate("未设置"))}</td><td>${number(
				material.current_gap_qty
			)}</td><td>${number(procurementCoverage)}</td><td>${number(material.shortage_qty)}</td></tr>`;
		})
		.join("");
	const extraShortages = Math.max(shortages.length - 5, 0);
	const shortageDetail = shortageRows
		? `<div class="quick-confirm-shortages"><strong>${escape(translate("采购缺口明细"))}</strong>
			<div class="quick-material-table-wrap"><table class="table"><thead><tr><th>${escape(
				translate("物料")
			)}</th><th>${escape(translate("来源仓库"))}</th><th>${escape(
				translate("即时采购缺口")
		  )}</th><th>${escape(translate("现有采购覆盖"))}</th><th>${escape(
				translate("建议新增申请")
		  )}</th></tr></thead><tbody>${shortageRows}</tbody></table></div>
			${
				extraShortages
					? `<p>${escape(translate("另有 {0} 项，请查看页面下方明细", [extraShortages]))}</p>`
					: ""
			}</div>`
		: "";
	const issues = (result.warnings || []).map((issue) => `<li>${escape(issue.message)}</li>`).join("");
	return `
		<div class="quick-confirm-summary">
			<div><span>${escape(translate("客户"))}</span><strong>${escape(result.customer)}</strong></div>
			<div><span>${escape(translate("交付日期"))}</span><strong>${escape(result.delivery_date)}</strong></div>
			<div><span>${escape(translate("订单金额"))}</span><strong>${escape(
		helpers.formatCurrency(result.grand_total, result.currency)
	)}</strong></div>
			<div><span>${escape(translate("可预留 / 需生产"))}</span><strong>${number(
		result.available_to_reserve
	)} / ${number(result.production_required)}</strong></div>
			<div><span>${escape(translate("原料缺料"))}</span><strong>${number(result.shortage_item_count)} ${escape(
		translate("项")
	)}</strong></div>
		</div>
		${shortageDetail}
		${
			issues
				? `<div class="quick-confirm-warning"><strong>${escape(
						translate("可以下单，但请留意")
				  )}</strong><ul class="quick-confirm-issues">${issues}</ul></div>`
				: ""
		}
		<p class="text-muted">${escape(translate("销售订单提交不会自动预留库存、创建生产任务或采购申请。"))}</p>`;
}

frappe.pages["quick-sales-order"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("快速开单"),
		single_column: true,
	});

	page.main.html(`
		<div class="process-simplification-page quick-sales-order quick-order-v2">
			<section class="quick-order-card quick-order-header" aria-labelledby="quick-order-info-title">
				<div class="quick-section-heading">
					<div>
						<h4 id="quick-order-info-title">${__("订单信息")}</h4>
						<p>${__("只填写客户和统一交期，其余信息由系统按 ERPNext 设置带出。")}</p>
					</div>
					<button class="btn btn-link btn-sm standard-sales-order">${__("使用标准销售订单")}</button>
				</div>
				<div class="quick-order-fields">
					<div data-field="customer"></div>
					<div data-field="delivery_date"></div>
					<div data-field="po_no"></div>
				</div>
				<details class="quick-order-remarks">
					<summary>${__("添加订单备注（可选）")}</summary>
					<div data-field="remarks"></div>
				</details>
			</section>

			<section class="quick-order-card quick-order-items" aria-labelledby="quick-order-items-title">
				<div class="quick-section-heading">
					<div>
						<h4 id="quick-order-items-title">${__("产品明细")}</h4>
						<p>${__("库存显示当前可预留数量，不等于仓库账面数量。")}</p>
					</div>
					<button class="btn btn-default btn-sm add-row">${__("添加产品")}</button>
				</div>
				<div class="table-responsive">
					<table class="table quick-items" aria-label="${__("快速开单产品明细")}">
						<thead>
							<tr>
								<th class="product-column">${__("产品")}</th>
								<th class="qty-column">${__("数量")}</th>
								<th class="rate-column">${__("成交单价")}</th>
								<th class="amount-column text-right">${__("金额")}</th>
								<th class="fulfillment-column">${__("履约情况")}</th>
								<th class="remove-column"><span class="sr-only">${__("操作")}</span></th>
							</tr>
						</thead>
						<tbody></tbody>
					</table>
				</div>
				<button class="btn btn-link btn-sm add-row add-row-inline">＋ ${__("添加一行")}</button>
			</section>

			<section class="quick-material-risk" aria-labelledby="quick-material-risk-title" tabindex="-1">
				<div class="quick-material-risk-heading">
					<div>
						<h3 id="quick-material-risk-title">${__("生产与物料风险")}</h3>
						<p>${__("按多级 BOM 逐层检查：半成品先用现货，不足部分转为生产需求，再检查下一层用料。")}</p>
					</div>
					<span class="quick-material-risk-time">${__("尚未检查")}</span>
				</div>
				<div class="quick-material-risk-body quick-material-risk-empty">${__("尚无可用的物料检查结果")}</div>
			</section>

			<section class="quick-order-guidance" aria-label="${__("快速开单适用范围")}">
				<strong>${__("什么时候使用标准销售订单？")}</strong>
				<span>${__("多个交期、同产品分行定价、外币或特殊税费、产品组合、委外、客户供料、序列号或批次等情况。")}</span>
			</section>

			<div class="quick-order-announcer sr-only" aria-live="polite"></div>
			<footer class="quick-order-summary" aria-label="${__("订单汇总")}">
				<div class="quick-summary-metrics">
					<div><span>${__("订单金额")}</span><strong data-summary="total">0.00</strong></div>
					<div><span>${__("可预留成品")}</span><strong data-summary="available">0</strong></div>
					<div><span>${__("需生产")}</span><strong data-summary="production">0</strong></div>
					<div><span>${__(
						"缺料项"
					)}</span><button type="button" class="quick-summary-shortage-link" data-summary="shortage">${__(
		"待检查"
	)}</button></div>
					<div><span>${__("最近检查")}</span><strong data-summary="checked_at">—</strong></div>
				</div>
				<div class="quick-summary-actions">
					<button class="btn btn-default deep-check">${__("检查库存与缺料")}</button>
					<button class="btn btn-primary confirm-order">${__("确认下单")}</button>
				</div>
			</footer>
		</div>
	`);

	const $root = page.main.find(".quick-sales-order");
	const state = {
		defaults: {},
		rows: new Map(),
		rowIndex: 0,
		previewSequence: 0,
		previewTimer: null,
		editSequence: 0,
		deepResult: null,
		lastMaterialResult: null,
		status: "editing",
		idempotencyKey: null,
	};
	const pageHelpers = {
		translate: __,
		escapeHtml: frappe.utils.escape_html,
		formatNumber: (value) => format_number(flt(value), null, 2),
		formatDate: (value) => frappe.datetime.str_to_user(value),
		formatCurrency: (value, currency) => format_currency(value, currency),
	};

	function renderMaterialRisk(result, { stale = false, checking = false } = {}) {
		const view = buildMaterialRiskView(result);
		view.stale = stale;
		view.checking = checking;
		$root.find(".quick-material-risk").toggleClass("is-stale", stale);
		$root
			.find(".quick-material-risk-body")
			.removeClass("quick-material-risk-empty")
			.html(materialRiskHtml(view, pageHelpers));
		$root
			.find(".quick-material-risk-time")
			.text(
				view.checked_at
					? `${__("检查于")} ${pageHelpers.formatDate(view.checked_at)}`
					: __("尚未检查")
			);
	}

	function rememberMaterialResult(result) {
		state.lastMaterialResult = { ...result, review_token: null };
	}

	function setMaterialRiskStale() {
		const $section = $root.find(".quick-material-risk");
		if (!$section.find(".quick-material-table, .quick-material-risk-zero").length) return;
		$section.addClass("is-stale");
		$section.find(".quick-material-risk-banner").remove();
		$section
			.find(".quick-material-risk-body")
			.prepend(
				`<div class="quick-material-risk-banner is-stale" role="status">${frappe.utils.escape_html(
					__("订单已修改，以下结果仅供参考，请重新检查")
				)}</div>`
			);
	}

	function announce(message) {
		$root.find(".quick-order-announcer").text(message || "");
	}

	function newIdempotencyKey() {
		return window.crypto?.randomUUID
			? window.crypto.randomUUID()
			: `${Date.now()}-${Math.random().toString(36).slice(2)}`;
	}

	function setStatus(status) {
		state.status = status;
		const busy = ["preview_loading", "deep_checking", "submitting"].includes(status);
		$root.find(".deep-check, .confirm-order").prop("disabled", busy);
	}

	function makeHeaderControl(fieldname, df) {
		const control = frappe.ui.form.make_control({
			parent: $root.find(`[data-field="${fieldname}"]`),
			df: Object.assign({ fieldname }, df),
			render_input: true,
		});
		control.$wrapper.find(".tooltip-content").remove();
		return control;
	}

	const fields = {
		customer: makeHeaderControl("customer", {
			fieldtype: "Link",
			options: "Customer",
			label: __("客户"),
			reqd: 1,
			change: () => markStale(),
		}),
		delivery_date: makeHeaderControl("delivery_date", {
			fieldtype: "Date",
			label: __("交付日期"),
			reqd: 1,
			change: () => markStale(),
		}),
		po_no: makeHeaderControl("po_no", {
			fieldtype: "Data",
			label: __("客户订单号（可选）"),
			change: () => markStale(),
		}),
		remarks: makeHeaderControl("remarks", {
			fieldtype: "Data",
			label: __("订单备注"),
			placeholder: __("例如：客户要求同批送达"),
			change: () => markStale(),
		}),
	};

	function productQuery() {
		return { query: "process_simplification.api.quick_order.search_quick_order_products" };
	}

	function makeCellControl($row, fieldname, df) {
		const control = frappe.ui.form.make_control({
			parent: $row.find(`[data-field="${fieldname}"]`),
			df: Object.assign({ fieldname, label: "" }, df),
			render_input: true,
		});
		control.toggle_label(false);
		control.$wrapper.find(".tooltip-content").remove();
		return control;
	}

	function rowTemplate(id) {
		return $(`
			<tr data-row-id="${id}">
				<td class="product-cell">
					<div data-field="item_code"></div>
					<div class="quick-row-help text-muted"></div>
				</td>
				<td data-field="qty"></td>
				<td data-field="rate"></td>
				<td class="quick-line-amount text-right">0.00</td>
				<td><div class="quick-fulfillment" role="status">${__("选择产品后显示库存情况")}</div></td>
				<td><button class="btn btn-xs btn-default remove-row" title="${__("删除产品")}" aria-label="${__(
			"删除产品"
		)}">×</button></td>
			</tr>
		`);
	}

	function updateLineAmount(row) {
		const amount = flt(row.qty.get_value()) * flt(row.rate.get_value());
		row.$element.find(".quick-line-amount").text(format_currency(amount, state.defaults.currency));
	}

	function renderRowStatus(row, preview) {
		row.preview = preview || null;
		if (!preview) {
			row.$element.find(".quick-fulfillment").html(`<span class="text-muted">${__("待检查")}</span>`);
			return;
		}
		const blocker = (preview.issues || []).find((issue) => issue.severity === "blocker");
		const warning = (preview.issues || []).find((issue) => issue.severity === "warning");
		const title = blocker ? __("无法下单") : warning ? __("需要生产") : __("库存可覆盖");
		const indicator = blocker ? "red" : warning ? "orange" : "green";
		const issueText = blocker?.message || warning?.message || __("当前可预留成品足够。 ");
		row.$element
			.find(".quick-row-help")
			.text(
				[preview.item_name, preview.stock_uom ? `${__("单位")}: ${preview.stock_uom}` : ""]
					.filter(Boolean)
					.join(" · ")
			);
		row.$element.find(".quick-fulfillment").html(`
			<div><span class="indicator-pill ${indicator}">${frappe.utils.escape_html(title)}</span></div>
			<div class="quick-fulfillment-numbers">
				${__("可预留")} <strong>${format_number(flt(preview.available_to_reserve), null, 2)}</strong>
				· ${__("需生产")} <strong>${format_number(flt(preview.production_required), null, 2)}</strong>
				· ${preview.bom_no ? __("BOM 已就绪") : __("无 BOM")}
			</div>
			<small>${frappe.utils.escape_html(issueText)}</small>
		`);
	}

	function addRow(values = {}) {
		const id = ++state.rowIndex;
		const $element = rowTemplate(id);
		$root.find(".quick-items tbody").append($element);
		const row = { id, $element, preview: null };
		row.item_code = makeCellControl($element, "item_code", {
			fieldtype: "Link",
			options: "Item",
			placeholder: __("输入产品编码或名称搜索"),
			reqd: 1,
			only_select: true,
			get_query: productQuery,
			change: () => applyItemDefaults(row),
		});
		row.qty = makeCellControl($element, "qty", {
			fieldtype: "Float",
			placeholder: __("数量"),
			reqd: 1,
			change: () => {
				updateLineAmount(row);
				markStale({ refreshPreview: true });
			},
		});
		row.rate = makeCellControl($element, "rate", {
			fieldtype: "Currency",
			placeholder: __("成交单价"),
			reqd: 1,
			change: () => {
				updateLineAmount(row);
				markStale();
			},
		});
		// Keep totals and safety state current while the user types; Frappe's
		// control-level change callback may wait until the field loses focus.
		row.qty.$input.on("input.quick-order", () => {
			updateLineAmount(row);
			markStale({ refreshPreview: true });
		});
		row.rate.$input.on("input.quick-order", () => {
			updateLineAmount(row);
			markStale();
		});
		state.rows.set(id, row);
		row.qty.set_value(values.qty || "");
		row.rate.set_value(values.rate || "");
		row.item_code.set_value(values.item_code || "");
		return row;
	}

	function applyItemDefaults(row) {
		markStale({ refreshPreview: true });
		const itemCode = row.item_code.get_value();
		if (!itemCode) return;
		frappe
			.call({
				method: "process_simplification.api.quick_order.get_quick_order_item_defaults",
				args: { item_code: itemCode },
			})
			.then((response) => {
				if (!state.rows.has(row.id) || row.item_code.get_value() !== itemCode) return;
				const detail = response.message || {};
				if (!flt(row.rate.get_value()) && detail.rate) row.rate.set_value(detail.rate);
				row.$element
					.find(".quick-row-help")
					.text(
						[detail.item_name, detail.stock_uom ? `${__("单位")}: ${detail.stock_uom}` : ""]
							.filter(Boolean)
							.join(" · ")
					);
				schedulePreview();
			});
	}

	function collectItems({ includeBlank = false } = {}) {
		return Array.from(state.rows.values())
			.map((row) => ({
				item_code: row.item_code.get_value(),
				qty: flt(row.qty.get_value()),
				rate: flt(row.rate.get_value()),
			}))
			.filter((row) => includeBlank || row.item_code || row.qty || row.rate);
	}

	function collectPayload() {
		return {
			customer: fields.customer.get_value(),
			delivery_date: fields.delivery_date.get_value(),
			po_no: fields.po_no.get_value(),
			remarks: fields.remarks.get_value(),
			items: collectItems(),
		};
	}

	function updateSummary(result = null) {
		const total = collectItems().reduce((sum, row) => sum + flt(row.qty) * flt(row.rate), 0);
		$root.find('[data-summary="total"]').text(format_currency(total, state.defaults.currency));
		const previews = Array.from(state.rows.values())
			.map((row) => row.preview)
			.filter(Boolean);
		$root
			.find('[data-summary="available"]')
			.text(
				format_number(
					result?.available_to_reserve ??
						previews.reduce((sum, row) => sum + flt(row.available_to_reserve), 0),
					null,
					2
				)
			);
		$root
			.find('[data-summary="production"]')
			.text(
				format_number(
					result?.production_required ??
						previews.reduce((sum, row) => sum + flt(row.production_required), 0),
					null,
					2
				)
			);
		$root
			.find('[data-summary="shortage"]')
			.text(result ? `${cint(result.shortage_item_count)} ${__("项")}` : __("待检查"));
		$root
			.find('[data-summary="checked_at"]')
			.text(result?.checked_at ? frappe.datetime.str_to_user(result.checked_at) : "—");
	}

	function markStale({ refreshPreview = false } = {}) {
		const hadDeepResult = Boolean(state.deepResult);
		if (hadDeepResult) setMaterialRiskStale();
		state.editSequence += 1;
		state.previewSequence += 1;
		state.deepResult = null;
		state.idempotencyKey = null;
		setStatus("preview_stale");
		for (const row of state.rows.values()) {
			if (row.preview) {
				row.$element.find(".quick-stale-label").remove();
				row.$element
					.find(".quick-fulfillment")
					.prepend(`<div class="quick-stale-label">${__("待重新检查")}</div>`);
			}
			updateLineAmount(row);
		}
		updateSummary();
		if (hadDeepResult) announce(__("订单已修改，确认下单前请重新检查。"));
		if (refreshPreview) schedulePreview();
	}

	function schedulePreview() {
		window.clearTimeout(state.previewTimer);
		state.previewTimer = window.setTimeout(runPreview, 650);
	}

	function runPreview() {
		const items = collectItems().filter((row) => row.item_code && row.qty > 0);
		if (!items.length) return;
		const sequence = ++state.previewSequence;
		setStatus("preview_loading");
		announce(__("正在读取可预留成品库存。"));
		frappe
			.call({
				method: "process_simplification.api.quick_order.preview_quick_order_items",
				args: quickOrderPreviewArgs(
					items,
					state.defaults,
					fields.delivery_date.get_value()
				),
			})
			.then((response) => {
				if (sequence !== state.previewSequence) return;
				const result = response.message || {};
				const byItem = new Map((result.rows || []).map((row) => [row.item_code, row]));
				for (const row of state.rows.values())
					renderRowStatus(row, byItem.get(row.item_code.get_value()));
				setStatus("preview_current");
				updateSummary();
				announce(__("库存预览已更新。"));
			})
			.catch(() => {
				if (sequence !== state.previewSequence) return;
				setStatus("preview_error");
				for (const row of state.rows.values()) {
					if (!row.item_code.get_value()) continue;
					row.preview = null;
					row.$element
						.find(".quick-fulfillment")
						.html(
							`<span class="indicator-pill red">${__("库存预览失败")}</span><small>${__(
								"请重新检查；最终下单前仍会由服务器再次校验。"
							)}</small>`
						);
				}
				announce(__("库存预览失败，请重试。"));
			});
	}

	function validateClientPayload(payload) {
		if (!payload.customer) return __("请选择客户。");
		if (!payload.delivery_date) return __("请选择交付日期。");
		if (!payload.items.length) return __("请至少添加一个产品。");
		for (let index = 0; index < payload.items.length; index += 1) {
			const row = payload.items[index];
			if (!row.item_code) return __("第 {0} 行请选择产品。", [index + 1]);
			if (row.qty <= 0) return __("第 {0} 行数量必须大于 0。", [index + 1]);
			if (row.rate <= 0) return __("第 {0} 行成交单价必须大于 0。", [index + 1]);
		}
		return null;
	}

	function renderIssues(issues) {
		if (!issues?.length) return "";
		return `<ul class="quick-confirm-issues">${issues
			.map((issue) => `<li>${frappe.utils.escape_html(issue.message)}</li>`)
			.join("")}</ul>`;
	}

	function showBlocked(result) {
		setStatus("blocked");
		frappe.msgprint({
			title: __("暂时不能下单"),
			message: renderIssues(result.blockers),
			indicator: "red",
		});
	}

	function runPreflight({ openConfirmation = false } = {}) {
		const payload = collectPayload();
		const editSequence = state.editSequence;
		const validation = validateClientPayload(payload);
		if (validation) {
			frappe.msgprint({ title: __("快速开单"), message: validation, indicator: "red" });
			return Promise.resolve(null);
		}
		setStatus("deep_checking");
		if (state.lastMaterialResult) renderMaterialRisk(state.lastMaterialResult, { checking: true });
		$root.find(".deep-check").text(__("正在检查…"));
		announce(__("正在服务器检查库存、BOM、缺料和订单规则。"));
		return frappe
			.call({
				method: "process_simplification.api.quick_order.preflight_quick_sales_order",
				type: "POST",
				args: { payload },
				freeze: openConfirmation,
				freeze_message: __("正在进行下单前安全检查…"),
			})
			.then((response) => {
				if (editSequence !== state.editSequence) {
					setStatus("preview_stale");
					frappe.show_alert({ message: __("订单内容已变化，请重新检查。"), indicator: "orange" });
					return null;
				}
				const result = response.message || {};
				clearRowStaleLabels($root);
				state.deepResult = result;
				rememberMaterialResult(result);
				updateSummary(result);
				renderMaterialRisk(result, { stale: false });
				$root.find(".deep-check").text(__("重新检查库存与缺料"));
				if (!result.can_submit) {
					showBlocked(result);
					return result;
				}
				setStatus("ready_to_confirm");
				announce(__("安全检查完成，可以确认下单。"));
				if (openConfirmation) showConfirmation(result, payload, editSequence);
				return result;
			})
			.catch((error) => {
				$root.find(".deep-check").text(__("检查库存与缺料"));
				if (state.lastMaterialResult) renderMaterialRisk(state.lastMaterialResult, { stale: true });
				if (state.status === "deep_checking") setStatus("editing");
				throw error;
			});
	}

	function showConfirmation(result, payload, editSequence = state.editSequence) {
		const dialog = new frappe.ui.Dialog({
			title: __("确认这张销售订单"),
			fields: [
				{ fieldname: "summary", fieldtype: "HTML", options: confirmationHtml(result, pageHelpers) },
			],
			primary_action_label: __("确认创建销售订单"),
			primary_action: () => submitOrder(dialog, result, payload, editSequence),
		});
		dialog.set_secondary_action(() => dialog.hide());
		dialog.set_secondary_action_label(__("返回修改"));
		dialog.show();
	}

	function submitOrder(dialog, result, payload, editSequence) {
		if (editSequence !== state.editSequence) {
			dialog.hide();
			frappe.msgprint({
				title: __("订单已修改"),
				message: __("确认窗口打开后订单内容发生了变化，请重新检查后再确认。"),
				indicator: "orange",
			});
			return;
		}
		if (!state.idempotencyKey) state.idempotencyKey = newIdempotencyKey();
		setStatus("submitting");
		dialog.get_primary_btn().prop("disabled", true).text(__("正在安全提交…"));
		frappe
			.call({
				method: "process_simplification.api.quick_order.submit_quick_sales_order",
				type: "POST",
				args: {
					payload,
					review_token: result.review_token,
					idempotency_key: state.idempotencyKey,
				},
				freeze: true,
				freeze_message: __("正在创建销售订单…"),
			})
			.then((response) => {
				const submitted = response.message || {};
				if (submitted.status === "reconfirmation_required") {
					dialog.hide();
					state.deepResult = submitted;
					rememberMaterialResult(submitted);
					state.idempotencyKey = null;
					updateSummary(submitted);
					renderMaterialRisk(submitted, { stale: false });
					setStatus("reconfirmation_required");
					frappe.msgprint({
						title: __("情况已变化，请重新确认"),
						message: __("库存、BOM、缺料或商业校验结果在提交前发生变化，系统没有创建订单。"),
						indicator: "orange",
					});
					showConfirmation(submitted, payload, state.editSequence);
					return;
				}
				if (submitted.status === "blocked") {
					dialog.hide();
					state.deepResult = submitted;
					rememberMaterialResult(submitted);
					renderMaterialRisk(submitted, { stale: false });
					showBlocked(submitted);
					return;
				}
				if (submitted.sales_order && submitted.route) {
					dialog.hide();
					frappe.show_alert({ message: __("销售订单已安全创建"), indicator: "green" });
					frappe.set_route(submitted.route);
				}
			})
			.catch(() => setStatus("ready_to_confirm"))
			.finally(() => dialog.get_primary_btn().prop("disabled", false).text(__("确认创建销售订单")));
	}

	function loadContext() {
		frappe
			.call({
				method: "process_simplification.api.quick_order.get_quick_order_context",
			})
			.then((response) => {
				state.defaults = response.message || {};
				fields.delivery_date.set_value(
					state.defaults.default_delivery_date ||
						frappe.datetime.add_days(frappe.datetime.nowdate(), 7)
				);
				addRow();
				updateSummary();
			});
	}

	$root.on("click", ".add-row", () => addRow());
	$root.on("click", ".remove-row", (event) => {
		const $row = $(event.currentTarget).closest("tr");
		state.rows.delete(cint($row.data("row-id")));
		$row.remove();
		if (!state.rows.size) addRow();
		markStale({ refreshPreview: true });
	});
	$root.on("click", ".deep-check", () => runPreflight().catch(() => {}));
	$root.on("click", ".confirm-order", () => runPreflight({ openConfirmation: true }).catch(() => {}));
	$root.on("click", ".quick-summary-shortage-link", () => {
		const section = $root.find(".quick-material-risk").get(0);
		section?.scrollIntoView({ behavior: "smooth", block: "start" });
		section?.focus({ preventScroll: true });
	});
	$root.on("click", ".standard-sales-order", () => {
		frappe.new_doc("Sales Order", {
			customer: fields.customer.get_value(),
			delivery_date: fields.delivery_date.get_value(),
			po_no: fields.po_no.get_value(),
		});
	});

	page.clear_actions();
	loadContext();
};

if (typeof module !== "undefined" && module.exports) {
	module.exports = {
		materialStatusMeta,
		buildMaterialRiskView,
		materialRiskHtml,
		confirmationHtml,
		clearRowStaleLabels,
		quickOrderPreviewArgs,
	};
}
