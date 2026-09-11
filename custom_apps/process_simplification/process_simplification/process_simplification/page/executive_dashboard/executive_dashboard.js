function psExecutiveEscape(value) {
	return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
		.replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function psExecutiveChangeMeta(value) {
	if (value === null || value === undefined) return { label: "暂无同期数据", tone: "muted" };
	const number = Number(value || 0);
	return {
		label: `${number >= 0 ? "↑" : "↓"} ${Math.abs(number).toFixed(1)}% 较上期`,
		tone: number >= 0 ? "positive" : "negative",
	};
}

function psExecutiveFormatCurrency(value, currency) {
	const safeCurrency = String(currency || "CNY").toUpperCase().replace(/[^A-Z0-9_-]/g, "") || "CNY";
	return `${safeCurrency} ${psExecutiveFormatAmount(value)}`;
}

function psExecutiveFormatAmount(value) {
	const amount = Number(value || 0);
	return new Intl.NumberFormat("zh-CN", {
		minimumFractionDigits: 2, maximumFractionDigits: 2,
	}).format(Number.isFinite(amount) ? amount : 0);
}

function psExecutiveFormatInteger(value) {
	const number = Number(value || 0);
	return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 })
		.format(Number.isFinite(number) ? Math.trunc(number) : 0);
}

function psExecutiveDestroyCharts(charts) {
	for (const chart of charts || []) {
		// Neutralize any queued ResizeObserver redraw before removing its SVG.
		if (chart && typeof chart.draw === "function") chart.draw = () => {};
		chart?.destroy?.();
	}
	return [];
}

function psExecutiveChartOptions(options) {
	// Entry animation swaps the SVG and can race a ResizeObserver redraw.
	return { ...options, animate: false, disableEntryAnimation: true };
}

function psExecutiveCompactAmount(value) {
	const number = Number(value);
	const safe = Number.isFinite(number) ? number : 0;
	const absolute = Math.abs(safe);
	const divisor = absolute >= 100000000 ? 100000000 : absolute >= 10000 ? 10000 : 1;
	return {
		value: new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(safe / divisor),
		unit: divisor === 100000000 ? "亿" : divisor === 10000 ? "万" : "",
	};
}

function psExecutivePeriod(preset, today) {
	const [year, month] = today.split("-").map(Number);
	if (preset === "year") return { from_date: year + "-01-01", to_date: today };
	if (preset === "last_month") {
		const to_date = new Date(Date.UTC(year, month - 1, 0)).toISOString().slice(0, 10);
		return { from_date: to_date.slice(0, 7) + "-01", to_date };
	}
	return { from_date: today.slice(0, 7) + "-01", to_date: today };
}

function psExecutivePeriodPreset(period, today) {
	return ["month", "last_month", "year"].find((key) => {
		const range = psExecutivePeriod(key, today);
		return range.from_date === period.from_date && range.to_date === period.to_date;
	}) || "custom";
}

function psExecutiveProgress(value) {
	const number = Number(value);
	return Number.isFinite(number) ? Math.min(Math.max(number, 0), 100) : 0;
}

function psExecutiveOverdueDays(deliveryDate, checkedAt) {
	// Compare calendar dates independently of browser timezone / DST.
	const due = Date.parse(String(deliveryDate || "").slice(0, 10));
	const reference = Date.parse(String(checkedAt || "").slice(0, 10));
	return Number.isFinite(due) && Number.isFinite(reference)
		? Math.max(0, Math.floor((reference - due) / 86400000)) : 0;
}

class ProcessSimplificationExecutiveDashboard {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.page = frappe.ui.make_app_page({
			parent: wrapper, title: __("经营总览"), single_column: true,
		});
		this.charts = [];
		this.filters = psExecutivePeriod("month", frappe.datetime.get_today());
		window.addEventListener("beforeunload", () => this.destroy_charts(), { once: true });
		this.make_body();
		this.load();
	}

	make_body() {
		this.page.main.addClass("process-simplification-page ps-executive-page");
		this.root = $(`<div class="ps-executive-dashboard">
			<header class="ps-exec-toolbar">
				<div class="ps-exec-context"><strong data-company>${__("经营总览")}</strong><span data-updated></span></div>
				<div class="ps-exec-controls" role="group" aria-label="${__("订单统计期间")}">
					<div class="ps-exec-periods">
						<button type="button" data-period="month">${__("本月")}</button>
						<button type="button" data-period="last_month">${__("上月")}</button>
						<button type="button" data-period="year">${__("今年")}</button>
					</div>
					<button type="button" data-filter>${__("筛选")}</button>
					<button type="button" data-refresh>${__("刷新")}</button>
				</div>
			</header>
			<p class="ps-exec-scope" data-scope></p>
			<div class="ps-exec-loading" data-loading role="status">
				<div class="ps-exec-spinner"></div><span>${__("正在汇总经营数据…")}</span>
			</div>
			<div class="ps-exec-error hide" data-error role="alert"></div>
			<div class="ps-exec-content hide" data-content>
				<section class="ps-exec-metrics" data-kpis aria-label="${__("经营关键指标")}"></section>
				<div class="ps-exec-attention" data-attention></div>
				<section class="ps-exec-delivery-grid">
					<article class="ps-exec-section ps-exec-overdue-section">
						<div class="ps-exec-section-head"><div><h3>${__("先盯这些逾期订单")}</h3><p data-overdue-note></p></div></div>
						<div data-overdue-list></div>
					</article>
					<article class="ps-exec-section">
						<div class="ps-exec-section-head"><h3>${__("待交付概况")}</h3></div>
						<div data-order-health></div>
					</article>
				</section>
				<section class="ps-exec-analysis-grid">
					<article class="ps-exec-section">
						<div class="ps-exec-section-head"><div><h3>${__("库存资金分布")}</h3><p>${__("当前库存价值，按成本估值")}</p></div></div>
						<div data-inventory-list></div>
					</article>
					<article class="ps-exec-section">
						<div class="ps-exec-section-head"><div><h3>${__("近 6 个月订单趋势")}</h3><p data-trend-note></p></div></div>
						<div class="ps-exec-trend" data-order-chart></div>
						<details class="ps-exec-detail"><summary>${__("查看每月金额")}</summary><div data-trend-table></div></details>
					</article>
				</section>
				<details class="ps-exec-detail ps-exec-definitions">
					<summary>${__("数据口径")}</summary>
					<p>${__("订单额为所选期间已提交销售订单的含税金额，不代表已收款。库存、库龄与待交付数据为当前状态，不随订单统计期间变化。")}</p>
					<p>${__("待交付金额按订单行剩余净额计算，税费和取整差额按净额比例分摊；交付进度为订单数量交付比例。同期对比为紧邻所选期间之前、天数相同的一段时间。")}</p>
				</details>
			</div>
		</div>`).appendTo(this.page.main);
		this.root.on("click", "[data-period]", (event) => {
			if (this.loading) return;
			this.load({ filters: {
				company: this.filters.company,
				...psExecutivePeriod(event.currentTarget.dataset.period, frappe.datetime.get_today()),
			} });
		});
		this.root.on("click", "[data-filter]", () => this.show_filters());
		this.root.on("click", "[data-refresh]", () => this.load());
	}

	show_filters() {
		if (this.loading) return;
		const dialog = new frappe.ui.Dialog({
			title: __("筛选经营数据"),
			fields: [
				{ fieldname: "company", label: __("公司"), fieldtype: "Select",
					options: (this.data?.companies || []).map((row) => row.name),
					default: this.filters.company, reqd: 1 },
				{ fieldname: "from_date", label: __("订单开始日期"), fieldtype: "Date", default: this.filters.from_date, reqd: 1 },
				{ fieldname: "to_date", label: __("订单结束日期"), fieldtype: "Date", default: this.filters.to_date, reqd: 1 },
				{ fieldtype: "HTML", options: `<p class="text-muted">${__("日期只筛选订单额和订单数，库存与交付风险始终显示当前状态。")}</p>` },
			],
			primary_action_label: __("应用"),
			primary_action: (values) => {
				if (this.loading) return;
				if (values.from_date > values.to_date) {
					frappe.msgprint(__("开始日期不能晚于结束日期。"));
					return;
				}
				dialog.hide();
				this.load({ filters: values });
			},
		});
		dialog.show();
	}

	async load(options = {}) {
		if (this.loading) return false;
		this.loading = true;
		const requested = { ...this.filters, ...options.filters };
		this.root.attr("aria-busy", "true");
		this.root.find(".ps-exec-controls button").prop("disabled", true);
		this.root.find("[data-error]").addClass("hide");
		if (!this.data) this.root.find("[data-loading]").removeClass("hide");
		try {
			return await frappe.ps_read_page(this.page, {
				method: "process_simplification.page_refresh.executive_dashboard",
				background: options.background,
				args: requested,
				apply: (response) => {
					this.data = response.message || {};
					this.filters = { company: this.data.company, ...this.data.period };
					this.root.find("[data-content]").removeClass("hide");
					this.render();
				},
			});
		} catch (error) {
			this.root.find("[data-error]").text(this.data
				? __("刷新失败，仍显示上次成功的数据，请重试。")
				: __("经营数据加载失败，请点击刷新重试。")).removeClass("hide");
			if (options.background) throw error;
			return false;
		} finally {
			this.root.find("[data-loading]").addClass("hide");
			this.root.find(".ps-exec-controls button").prop("disabled", false);
			this.root.attr("aria-busy", "false");
			this.loading = false;
		}
	}

	format_currency(value) {
		return psExecutiveFormatCurrency(value, this.data.currency);
	}

	currency_unit() {
		return this.data.currency === "CNY" ? __("元") : String(this.data.currency || "CNY").replace(/[^A-Z]/g, "");
	}

	money(value) {
		return `${psExecutiveFormatAmount(value)} ${this.currency_unit()}`;
	}

	metric({ label, amount, value, unit, detail, foot = "", tone = "" }) {
		const compact = amount === undefined ? { value, unit } : psExecutiveCompactAmount(amount);
		const exact = amount === undefined ? "" : this.money(amount);
		return `<article class="ps-exec-metric ${tone ? `ps-exec-metric-${tone}` : ""}">
			<div class="ps-exec-metric-label">${psExecutiveEscape(label)}</div>
			<div class="ps-exec-metric-value" ${exact ? `aria-label="${psExecutiveEscape(label + " " + exact)}" data-exact-value="${psExecutiveEscape(exact)}"` : ""}>
				<strong>${psExecutiveEscape(compact.value)}</strong><span>${psExecutiveEscape(compact.unit || (amount === undefined ? "" : this.currency_unit()))}</span>
			</div>
			${exact ? `<div class="ps-exec-exact">${exact}</div>` : ""}
			<div class="ps-exec-metric-detail">${psExecutiveEscape(detail || "")}</div>${foot}
		</article>`;
	}

	render() {
		const data = this.data;
		const health = data.order_health || {};
		const ageing = data.stock_ageing || {};
		const checkedAt = String(data.checked_at || "");
		const preset = psExecutivePeriodPreset(data.period, frappe.datetime.get_today());
		this.root.find("[data-company]").text(data.company);
		this.root.find("[data-updated]").text(checkedAt ? `${checkedAt.slice(5, 10)} ${checkedAt.slice(11, 16)} ${__("更新")}` : "");
		this.root.find("[data-scope]").text(`${__("订单期间")} ${data.period.from_date} — ${data.period.to_date} · ${__("库存与交付为当前状态")}`);
		this.root.find("[data-period]").each((_index, node) => {
			node.setAttribute("aria-pressed", String(node.dataset.period === preset));
		});
		this.root.find("[data-filter]").attr("aria-pressed", String(preset === "custom"));
		const change = psExecutiveChangeMeta(data.orders.order_amount_change);
		this.root.find("[data-kpis]").html([
			this.metric({
				label: __("本期订单额"), amount: data.orders.order_amount,
				detail: `${psExecutiveFormatInteger(data.orders.order_count)} ${__("张订单 · 含税")}`,
				foot: `<span class="ps-exec-change ps-exec-change-${change.tone}">${psExecutiveEscape(change.label)}</span>`,
				tone: "primary",
			}),
			this.metric({
				label: __("逾期未交付"), value: psExecutiveFormatInteger(health.overdue_orders), unit: __("单"),
				detail: `${__("待交付")} ${this.money(health.overdue_amount)}`,
				foot: `<span class="ps-exec-metric-hint">${__("7 天内到期")} ${psExecutiveFormatInteger(health.due_within_7_days)} ${__("单")}</span>`,
				tone: health.overdue_orders ? "danger" : "",
			}),
			this.metric({
				label: __("当前库存总值"), amount: data.inventory.total_stock_value,
				detail: __("各类库存成本合计"),
			}),
			this.metric({
				label: __("90 天以上库存"),
				...(ageing.available ? { amount: ageing.stock_value } : { value: "—" }),
				detail: ageing.available ? `${psExecutiveFormatInteger(ageing.item_count)} ${__("个物料")}` : ageing.message || __("暂时无法计算"),
				tone: ageing.available && ageing.stock_value > 0 ? "warning" : "",
			}),
		].join(""));
		const overdueCount = Number(health.overdue_orders || 0);
		this.root.find("[data-attention]").html(overdueCount
			? `<strong>${overdueCount} ${__("单已逾期，需要跟进交付")}</strong><span>${__("逾期待交付")} ${this.money(health.overdue_amount)}</span>`
			: `<strong>${__("当前没有逾期订单")}</strong><span>${__("7 天内到期")} ${psExecutiveFormatInteger(health.due_within_7_days)} ${__("单")}</span>`
		).toggleClass("is-clear", !overdueCount);
		this.render_order_health();
		this.render_overdue_orders();
		this.render_inventory();
		this.render_charts();
	}

	render_order_health() {
		const health = this.data.order_health || {};
		const maxValue = Math.max(Number(health.open_orders || 0), 1);
		const rows = [
			{ label: __("已经逾期"), value: health.overdue_orders, tone: "red" },
			{ label: __("7 天内到期"), value: health.due_within_7_days, tone: "amber" },
			{ label: __("其他待交付"), value: health.other_open_orders, tone: "blue" },
		];
		this.root.find("[data-order-health]").html(`<div class="ps-exec-pending-total">
			<strong>${psExecutiveFormatInteger(health.open_orders)} <small>${__("单待交付")}</small></strong>
			<span>${this.money(health.pending_amount)}</span>
		</div>${rows.map((row) => `<div class="ps-exec-health-row">
			<div><span>${row.label}</span><strong>${psExecutiveFormatInteger(row.value)} ${__("单")}</strong></div>
			<div class="ps-exec-health-track"><i class="ps-exec-health-${row.tone}" style="width:${psExecutiveProgress(Number(row.value || 0) / maxValue * 100)}%"></i></div>
		</div>`).join("")}`);
	}

	render_overdue_orders() {
		const orders = this.data.overdue_orders || [];
		const count = Number(this.data.order_health.overdue_orders || 0);
		this.root.find("[data-overdue-note]").text(count
			? `${__("按交期从早到晚")} · ${__("显示")} ${orders.length} / ${count} ${__("单")} · ${__("点订单查看交付进展")}`
			: __("继续关注即将到期的交付"));
		if (!orders.length) {
			this.root.find("[data-overdue-list]").html(`<div class="ps-exec-empty">${__("当前没有逾期未交付订单")}</div>`);
			return;
		}
		this.root.find("[data-overdue-list]").html(orders.map((row) => {
			const progress = psExecutiveProgress(row.per_delivered);
			const days = psExecutiveOverdueDays(row.delivery_date, this.data.checked_at);
			const href = `/desk/order-workbench?sales_order=${encodeURIComponent(row.name)}`;
			return `<a class="ps-exec-order" href="${psExecutiveEscape(href)}">
				<div class="ps-exec-order-identity"><strong>${psExecutiveEscape(row.customer_name || row.customer)}</strong><span>${psExecutiveEscape(row.name)}</span></div>
				<div class="ps-exec-order-due"><strong>${__("逾期")} ${days} ${__("天")}</strong><span>${psExecutiveEscape(row.delivery_date)} ${__("交付")}</span></div>
				<div class="ps-exec-order-amount"><small>${__("待交付金额")}</small><strong>${this.money(row.pending_amount)}</strong></div>
				<div class="ps-exec-delivered">
					<span>${progress === 0 ? __("尚未交付") : `${__("已交付")} ${progress.toFixed(1).replace(/\.0$/, "")}%`}</span>
					<div class="ps-exec-delivery-track" role="progressbar" aria-label="${__("已交付比例")}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}"><i style="width:${progress}%"></i></div>
					<small class="ps-exec-open-order">${__("查看订单")} →</small>
				</div>
			</a>`;
		}).join(""));
	}

	render_inventory() {
		const categories = this.data.inventory.categories || [];
		const total = Number(this.data.inventory.total_stock_value || 0);
		const hasNegative = categories.some((row) => Number(row.stock_value) < 0);
		this.root.find("[data-inventory-list]").html(categories.map((row) => {
			const percentage = !hasNegative && total > 0 ? psExecutiveProgress(Number(row.stock_value || 0) / total * 100) : 0;
			const color = /^#[0-9a-f]{6}$/i.test(row.color) ? row.color : "#64748b";
			return `<div class="ps-exec-inventory-row">
				<div class="ps-exec-inventory-heading"><span><i style="background:${color}"></i>${psExecutiveEscape(row.label)}</span><strong>${this.money(row.stock_value)}</strong></div>
				<div class="ps-exec-inventory-meta"><span>${psExecutiveFormatInteger(row.item_count)} ${__("个物料")}</span><span>${hasNegative ? "" : `${percentage.toFixed(1)}%`}</span></div>
				${hasNegative ? "" : `<div class="ps-exec-inventory-track"><i style="background:${color};width:${percentage}%"></i></div>`}
			</div>`;
		}).join("") + (hasNegative ? `<p class="ps-exec-scope">${__("存在负库存价值，仅展示金额，不计算占比。")}</p>` : ""));
	}

	render_charts() {
		this.destroy_charts();
		const trend = this.data.order_trend || [];
		this.root.find("[data-trend-note]").text(`${__("已提交订单 · 含税")} · ${__("截至")} ${this.data.period.to_date}`);
		this.root.find("[data-trend-table]").html(`<table class="ps-exec-months"><thead><tr><th>${__("月份")}</th><th>${__("订单数")}</th><th>${__("订单额")}</th></tr></thead><tbody>${trend.map((row) => `<tr><td>${psExecutiveEscape(row.month)}</td><td>${psExecutiveFormatInteger(row.order_count)}</td><td>${this.money(row.order_amount)}</td></tr>`).join("")}</tbody></table>`);
		const orderElement = this.root.find("[data-order-chart]").empty()[0];
		if (!trend.some((row) => Number(row.order_amount))) {
			$(orderElement).html(`<div class="ps-exec-empty">${__("这段时间暂无订单金额")}</div>`);
			return;
		}
		const largest = Math.max(...trend.map((row) => Math.abs(Number(row.order_amount || 0))));
		const scale = largest >= 100000000 ? 100000000 : largest >= 10000 ? 10000 : 1;
		const unit = (scale === 100000000 ? __("亿") : scale === 10000 ? __("万") : "") + this.currency_unit();
		this.root.find("[data-trend-note]").append(document.createTextNode(` · ${__("单位")}：${unit}`));
		this.charts.push(new frappe.Chart(orderElement, psExecutiveChartOptions({
			data: {
				labels: trend.map((row) => row.month.slice(2).replace("-", "/")),
				datasets: [{ name: __("订单额"), values: trend.map((row) => Number(row.order_amount || 0) / scale) }],
			},
			type: "bar", height: 230, colors: ["#287d76"],
			axisOptions: { xAxisMode: "tick", yAxisMode: "tick", xIsSeries: true },
			barOptions: { spaceRatio: 0.45 },
			tooltipOptions: { formatTooltipY: (value) => this.format_currency(value * scale) },
		})));
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
		psExecutiveChangeMeta, psExecutiveEscape, psExecutiveFormatAmount,
		psExecutiveFormatCurrency, psExecutiveFormatInteger, psExecutiveDestroyCharts,
		psExecutiveChartOptions, psExecutiveCompactAmount, psExecutivePeriod,
		psExecutivePeriodPreset, psExecutiveProgress, psExecutiveOverdueDays,
		ProcessSimplificationExecutiveDashboard,
	};
}
