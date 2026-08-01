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
					<div><span>${__("缺料项")}</span><strong data-summary="shortage">${__("待检查")}</strong></div>
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
		status: "editing",
		idempotencyKey: null,
	};

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
				args: { items },
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
		$root.find(".deep-check").text(__("正在检查…"));
		announce(__("正在服务器检查库存、BOM、缺料和订单规则。"));
		return frappe
			.call({
				method: "process_simplification.api.quick_order.preflight_quick_sales_order",
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
				state.deepResult = result;
				updateSummary(result);
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
				if (state.status === "deep_checking") setStatus("editing");
				throw error;
			});
	}

	function confirmationHtml(result) {
		return `
			<div class="quick-confirm-summary">
				<div><span>${__("客户")}</span><strong>${frappe.utils.escape_html(result.customer)}</strong></div>
				<div><span>${__("交付日期")}</span><strong>${frappe.utils.escape_html(result.delivery_date)}</strong></div>
				<div><span>${__("订单金额")}</span><strong>${format_currency(
			result.grand_total,
			result.currency
		)}</strong></div>
				<div><span>${__("可预留 / 需生产")}</span><strong>${format_number(
			result.available_to_reserve,
			null,
			2
		)} / ${format_number(result.production_required, null, 2)}</strong></div>
				<div><span>${__("原料缺料")}</span><strong>${cint(result.shortage_item_count)} ${__("项")}</strong></div>
			</div>
			${
				result.warnings?.length
					? `<div class="quick-confirm-warning"><strong>${__(
							"可以下单，但请留意"
					  )}</strong>${renderIssues(result.warnings)}</div>`
					: ""
			}
			<p class="text-muted">${__("销售订单提交不会自动预留库存、创建生产任务或采购申请。")}</p>
		`;
	}

	function showConfirmation(result, payload, editSequence = state.editSequence) {
		const dialog = new frappe.ui.Dialog({
			title: __("确认这张销售订单"),
			fields: [{ fieldname: "summary", fieldtype: "HTML", options: confirmationHtml(result) }],
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
					state.idempotencyKey = null;
					updateSummary(submitted);
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
