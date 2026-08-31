function psExecutiveEscape(value) {
	if (typeof frappe !== "undefined" && frappe.utils?.escape_html) {
		return frappe.utils.escape_html(String(value ?? ""));
	}
	return String(value ?? "")
		.replaceAll("&", "&amp;")
		.replaceAll("<", "&lt;")
		.replaceAll(">", "&gt;")
		.replaceAll('"', "&quot;")
		.replaceAll("'", "&#039;");
}

function psExecutiveChangeMeta(value) {
	if (value === null || value === undefined) {
		return { label: "暂无同期数据", tone: "muted" };
	}
	const number = Number(value || 0);
	return {
		label: `${number >= 0 ? "↑" : "↓"} ${Math.abs(number).toFixed(1)}% 较上一同期`,
		tone: number >= 0 ? "positive" : "negative",
	};
}

function psExecutiveInventoryChartData(categories) {
	const visible = (categories || []).filter((row) => Number(row.stock_value || 0) !== 0);
	return {
		labels: visible.map((row) => row.label),
		values: visible.map((row) => Number(row.stock_value || 0)),
		colors: visible.map((row) => row.color),
	};
}

function psExecutiveFormatCurrency(value, currency) {
	const amount = Number(value || 0);
	const safeAmount = Number.isFinite(amount) ? amount : 0;
	const safeCurrency = String(currency || "CNY")
		.toUpperCase()
		.replace(/[^A-Z0-9_-]/g, "") || "CNY";
	try {
		return new Intl.NumberFormat("zh-CN", {
			style: "currency",
			currency: safeCurrency,
			currencyDisplay: "code",
			minimumFractionDigits: 2,
			maximumFractionDigits: 2,
		}).format(safeAmount).replace(/\s+/g, " ");
	} catch (_error) {
		return `${safeCurrency} ${safeAmount.toLocaleString("zh-CN", {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2,
		})}`;
	}
}

function psExecutiveShouldReloadCompany(settingCompany, loadedCompany, selectedCompany) {
	return !settingCompany && String(selectedCompany || "") !== String(loadedCompany || "");
}

function psExecutiveDestroyCharts(charts) {
	for (const chart of charts || []) {
		// ResizeObserver can already have a redraw queued while the Desk page is
		// unloading. Neutralize that callback before disconnecting the observer so
		// it cannot touch SVG nodes after the browser has removed them.
		if (chart && typeof chart.draw === "function") chart.draw = () => {};
		chart?.destroy?.();
	}
	return [];
}

function psExecutiveChartOptions(options) {
	// Frappe Charts temporarily swaps the SVG during entry animation. A queued
	// ResizeObserver redraw can then remove the already-swapped node and throw a
	// NotFoundError. These dashboard charts are informational, so render them
	// without animation and keep resize handling stable.
	return { ...options, animate: false, disableEntryAnimation: true };
}

class ProcessSimplificationExecutiveDashboard {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("经营总览"),
			single_column: true,
		});
		this.company = null;
		this.charts = [];
		this.setting_company = false;
		window.addEventListener("beforeunload", () => this.destroy_charts(), { once: true });
		this.make_filters();
		this.make_body();
		this.load();
	}

	make_filters() {
		const today = frappe.datetime.get_today();
		this.company_field = this.page.add_field({
			fieldname: "company",
			label: __("公司"),
			fieldtype: "Select",
			options: [],
			change: () => {
				if (
					psExecutiveShouldReloadCompany(
						this.setting_company,
						this.company,
						this.company_field.get_value()
					)
				) this.load();
			},
		});
		this.from_date_field = this.page.add_field({
			fieldname: "from_date",
			label: __("开始日期"),
			fieldtype: "Date",
			default: `${today.slice(0, 7)}-01`,
			reqd: 1,
		});
		this.to_date_field = this.page.add_field({
			fieldname: "to_date",
			label: __("结束日期"),
			fieldtype: "Date",
			default: today,
			reqd: 1,
		});
		this.page.add_inner_button(__("刷新"), () => this.load(), __("经营数据"));
	}

	make_body() {
		this.page.main.addClass("process-simplification-page ps-executive-page");
		this.root = $(
			`<div class="ps-executive-dashboard">
				<section class="ps-exec-hero">
					<div>
						<div class="ps-exec-eyebrow">BUSINESS OVERVIEW</div>
						<h2>${__("经营驾驶舱")}</h2>
						<p>${__("订单、毛利、库存和交付风险集中在一个页面。金额仅向老板角色开放。")}</p>
					</div>
					<div class="ps-exec-updated" data-updated></div>
				</section>
				<div class="ps-exec-loading" data-loading>
					<div class="ps-exec-spinner"></div><span>${__("正在汇总经营数据…")}</span>
				</div>
				<div class="ps-exec-content hide" data-content>
					<section class="ps-exec-kpi-grid" data-kpis></section>
					<section class="ps-exec-chart-grid">
						<article class="ps-exec-panel ps-exec-panel-wide">
							<div class="ps-exec-panel-head"><div><span>${__("订单趋势")}</span><h3>${__("近 6 个月订单额")}</h3></div><div class="ps-exec-panel-note">${__("已提交销售订单 · 含税")}</div></div>
							<div class="ps-exec-chart" data-order-chart></div>
						</article>
						<article class="ps-exec-panel">
							<div class="ps-exec-panel-head"><div><span>${__("仓库总览")}</span><h3>${__("当前库存价值结构")}</h3></div></div>
							<div class="ps-exec-chart" data-inventory-chart></div>
						</article>
					</section>
					<section class="ps-exec-stock-grid" data-stock-cards></section>
					<section class="ps-exec-lower-grid">
						<article class="ps-exec-panel">
							<div class="ps-exec-panel-head"><div><span>${__("交付健康")}</span><h3>${__("当前未交付订单")}</h3></div></div>
							<div data-order-health></div>
						</article>
						<article class="ps-exec-panel ps-exec-panel-wide">
							<div class="ps-exec-panel-head"><div><span>${__("老板关注")}</span><h3>${__("最早逾期订单")}</h3></div></div>
							<div data-overdue-list></div>
						</article>
					</section>
				</div>
			</div>`
		).appendTo(this.page.main);
	}

	async load() {
		if (this.loading) return;
		this.loading = true;
		this.root.find("[data-loading]").removeClass("hide");
		this.root.find("[data-content]").addClass("hide");
		try {
			const response = await frappe.call({
				method: "process_simplification.api.executive_dashboard.get_dashboard",
				args: {
					company: this.company_field.get_value() || undefined,
					from_date: this.from_date_field.get_value(),
					to_date: this.to_date_field.get_value(),
				},
			});
			this.data = response.message || {};
			await this.set_company_options(this.data.companies || [], this.data.company);
			this.root.find("[data-content]").removeClass("hide");
			this.render();
		} catch (error) {
			frappe.msgprint({
				title: __("经营总览加载失败"),
				message: error?.message || __("无法读取经营数据。"),
				indicator: "red",
			});
		} finally {
			this.root.find("[data-loading]").addClass("hide");
			this.loading = false;
		}
	}

	async set_company_options(companies, selected) {
		const options = companies.map((row) => row.name);
		this.company = selected || null;
		this.setting_company = true;
		try {
			this.company_field.df.options = options;
			this.company_field.refresh();
			await this.company_field.set_value(selected);
		} finally {
			this.setting_company = false;
		}
	}

	format_currency(value) {
		return psExecutiveFormatCurrency(value, this.data.currency);
	}

	kpi_card({ label, value, detail, tone = "blue", change = null }) {
		const changeMeta = change === null ? null : psExecutiveChangeMeta(change);
		return `<article class="ps-exec-kpi ps-exec-tone-${tone}">
			<div class="ps-exec-kpi-label">${psExecutiveEscape(label)}</div>
			<div class="ps-exec-kpi-value">${value}</div>
			<div class="ps-exec-kpi-footer">
				<span>${psExecutiveEscape(detail || "")}</span>
				${changeMeta ? `<span class="ps-exec-change ps-exec-change-${changeMeta.tone}">${psExecutiveEscape(changeMeta.label)}</span>` : ""}
			</div>
		</article>`;
	}

	render() {
		const data = this.data;
		const gross = data.gross_profit || {};
		const ageing = data.stock_ageing || {};
		const orderChange = data.orders.order_amount_change;
		const checkedAt = data.checked_at ? frappe.datetime.str_to_user(data.checked_at) : "";
		this.root.find("[data-updated]").html(
			`<span>${psExecutiveEscape(data.company)}</span><small>${__("更新时间")} ${psExecutiveEscape(checkedAt)}</small>`
		);
		this.root.find("[data-kpis]").html([
			this.kpi_card({
				label: __("本期订单额"),
				value: this.format_currency(data.orders.order_amount),
				detail: `${data.orders.order_count} ${__("张已提交订单 · 含税")}`,
				tone: "blue",
				change: orderChange === null || orderChange === undefined ? null : orderChange,
			}),
			this.kpi_card({
				label: __("本期订单数"),
				value: frappe.format(data.orders.order_count, { fieldtype: "Int" }),
				detail: `${data.period.from_date} — ${data.period.to_date}`,
				tone: "violet",
			}),
			this.kpi_card({
				label: __("毛利率"),
				value: gross.available ? `${Number(gross.gross_margin_percent || 0).toFixed(1)}%` : "—",
				detail: gross.available ? __("按销售发票与库存估值") : gross.message,
				tone: "emerald",
			}),
			this.kpi_card({
				label: __("当前库存总值"),
				value: this.format_currency(data.inventory.total_stock_value),
				detail: __("成品、半成品、在制和原材料"),
				tone: "cyan",
			}),
			this.kpi_card({
				label: __("逾期订单"),
				value: frappe.format(data.order_health.overdue_orders, { fieldtype: "Int" }),
				detail: `${__("待交付金额")} ${this.format_currency(data.order_health.pending_amount)}`,
				tone: data.order_health.overdue_orders ? "red" : "emerald",
			}),
			this.kpi_card({
				label: __("90 天以上库存"),
				value: ageing.available ? this.format_currency(ageing.stock_value) : "—",
				detail: ageing.available ? `${ageing.item_count} ${__("个物料")}` : ageing.message,
				tone: ageing.stock_value ? "amber" : "slate",
			}),
		].join(""));
		this.render_stock_cards();
		this.render_order_health();
		this.render_overdue_orders();
		this.render_charts();
	}

	render_stock_cards() {
		const categories = (this.data.inventory.categories || []).filter((row) => row.key !== "other");
		this.root.find("[data-stock-cards]").html(
			categories.map((row) => `<article class="ps-exec-stock-card" style="--ps-stock-color:${row.color}">
				<div class="ps-exec-stock-mark"></div>
				<div><span>${psExecutiveEscape(row.label)}</span><strong>${this.format_currency(row.stock_value)}</strong></div>
				<small>${row.item_count} ${__("个有库存物料")}</small>
			</article>`).join("")
		);
	}

	render_order_health() {
		const health = this.data.order_health || {};
		const maxValue = Math.max(Number(health.open_orders || 0), 1);
		const rows = [
			{ label: __("已经逾期"), value: health.overdue_orders, tone: "red" },
			{ label: __("7 天内到期"), value: health.due_within_7_days, tone: "amber" },
			{ label: __("其他待交付"), value: health.other_open_orders, tone: "blue" },
		];
		this.root.find("[data-order-health]").html(`<div class="ps-exec-health-total">
			<strong>${health.open_orders || 0}</strong><span>${__("张未交付订单")}</span>
		</div>${rows.map((row) => `<div class="ps-exec-health-row">
			<div><span>${row.label}</span><strong>${Number(row.value || 0)}</strong></div>
			<div class="ps-exec-health-track"><i class="ps-exec-health-${row.tone}" style="width:${Math.max((Number(row.value || 0) / maxValue) * 100, row.value ? 4 : 0)}%"></i></div>
		</div>`).join("")}`);
	}

	render_overdue_orders() {
		const orders = this.data.overdue_orders || [];
		if (!orders.length) {
			this.root.find("[data-overdue-list]").html(`<div class="ps-exec-empty">${__("当前没有逾期未交付订单")}</div>`);
			return;
		}
		this.root.find("[data-overdue-list]").html(`<div class="ps-exec-order-list">${orders.map((row) => `
			<div class="ps-exec-order-row">
				<div><strong>${psExecutiveEscape(row.name)}</strong><span>${psExecutiveEscape(row.customer_name || row.customer)}</span></div>
				<div><small>${__("交付日期")}</small><strong>${psExecutiveEscape(row.delivery_date)}</strong></div>
				<div><small>${__("待交付")}</small><strong>${this.format_currency(row.pending_amount)}</strong></div>
				<div class="ps-exec-progress"><i style="width:${Math.min(Math.max(Number(row.per_delivered || 0), 0), 100)}%"></i><span>${Number(row.per_delivered || 0).toFixed(0)}%</span></div>
			</div>`).join("")}</div>`);
	}

	render_charts() {
		this.destroy_charts();
		const trend = this.data.order_trend || [];
		const orderElement = this.root.find("[data-order-chart]").empty()[0];
		this.charts.push(new frappe.Chart(orderElement, psExecutiveChartOptions({
			data: {
				labels: trend.map((row) => row.month),
				datasets: [{ name: __("订单额"), values: trend.map((row) => Number(row.order_amount || 0)) }],
			},
			type: "bar",
			height: 270,
			colors: ["#2563eb"],
			axisOptions: { xAxisMode: "tick", yAxisMode: "tick", xIsSeries: true },
			barOptions: { spaceRatio: 0.45 },
			tooltipOptions: { formatTooltipY: (value) => this.format_currency(value) },
		})));

		const inventoryData = psExecutiveInventoryChartData(this.data.inventory.categories);
		const inventoryElement = this.root.find("[data-inventory-chart]").empty()[0];
		if (inventoryData.values.length) {
			this.charts.push(new frappe.Chart(inventoryElement, psExecutiveChartOptions({
				data: { labels: inventoryData.labels, datasets: [{ values: inventoryData.values }] },
				type: "donut",
				height: 270,
				colors: inventoryData.colors,
				tooltipOptions: { formatTooltipY: (value) => this.format_currency(value) },
			})));
		} else {
			$(inventoryElement).html(`<div class="ps-exec-empty">${__("当前没有库存价值数据")}</div>`);
		}
	}

	destroy_charts() {
		this.charts = psExecutiveDestroyCharts(this.charts);
	}
}

if (typeof frappe !== "undefined") {
	frappe.pages["executive-dashboard"].on_page_load = (wrapper) => {
		wrapper.executive_dashboard = new ProcessSimplificationExecutiveDashboard(wrapper);
	};
}

if (typeof module !== "undefined" && module.exports) {
	module.exports = {
		psExecutiveChangeMeta,
		psExecutiveInventoryChartData,
		psExecutiveEscape,
		psExecutiveFormatCurrency,
		psExecutiveShouldReloadCompany,
		psExecutiveDestroyCharts,
		psExecutiveChartOptions,
	};
}
