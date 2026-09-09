frappe.pages["purchase-receipt-notice"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("采购到货通知"), single_column: true });
	const api = "process_simplification.purchasing.receipts.";
	const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
	const statusLabels = { Pending: "等待发送", Partial: "部分送达", Delivered: "已送达", Failed: "发送失败", Suppressed: "通知关闭，未发送", "No Recipients": "没有有效接收人", Skipped: "已跳过", Retry: "等待重试" };
	let generation = 0;
	const listState = { start: 0, history: [], search: "", from_date: "", to_date: "" };
	async function load() {
		const route = frappe.get_route();
		if (route[1]) return frappe.set_route("purchase-receipt-notice", { name: route[1] });
		const name = frappe.route_options?.name || new URLSearchParams(window.location.search).get("name");
		if (frappe.route_options) delete frappe.route_options.name;
		if (name) window.history.replaceState(window.history.state, "", `/desk/purchase-receipt-notice?name=${encodeURIComponent(name)}`);
		const current = ++generation;
		page.clear_primary_action();
		page.main.html('<div class="purchase-empty">正在读取到货通知…</div>');
		const message = name ? (await frappe.call({ method: api + "get_notice", args: { name } })).message : null;
		if (current !== generation) return;
		const root = $('<div class="process-simplification-page receipt-notice">');
		page.main.empty().append(root);
		root.append('<header class="purchase-hero"><div><span class="purchase-eyebrow">采购执行台</span><h1>到货通知</h1><p class="purchase-meta">每批收货提交后通知一次，按收货单查看记录。</p></div><a class="btn btn-default" href="/desk/purchase-supplier-allocation">供应商分配</a></header>');
		if (!name) {
			const section = $('<section class="purchase-section"><div class="purchase-section-heading"><h2>到货记录</h2><span class="purchase-notice-page"></span></div><div class="purchase-list-filters"></div><div class="purchase-notice-rows"></div><div class="purchase-list-pager"></div></section>').appendTo(root);
			let timer;
			for (const [key, label, type] of [["search", "搜索供应商、收货单或物料", "search"], ["from_date", "开始日期", "date"], ["to_date", "结束日期", "date"]]) {
				const holder = $('<label class="form-group">').text(label).appendTo(section.find(".purchase-list-filters"));
				$('<input class="form-control">').attr("type", type).val(listState[key]).appendTo(holder).on("input change", (event) => {
					listState[key] = event.currentTarget.value; listState.start = 0; listState.history = [];
					clearTimeout(timer); timer = setTimeout(show, 250);
				});
			}
			async function show() {
				if (!section.get(0).isConnected) return;
				const token = ++generation;
				const host = section.find(".purchase-notice-rows").html('<div class="purchase-empty">正在查询…</div>');
				const pager = section.find(".purchase-list-pager").empty();
				try {
					const { message: result } = await frappe.call({ method: api + "get_notice_page", args: { start: listState.start, search: listState.search, from_date: listState.from_date, to_date: listState.to_date } });
					if (token !== generation) return;
					section.find(".purchase-notice-page").text(`第 ${listState.history.length + 1} 页 · 本页 ${result.rows.length} 条`);
					host.html(!result.rows.length ? '<div class="purchase-empty">当前范围没有到货记录，可修改搜索条件或日期。</div>' : `<table class="purchase-table purchase-request-table"><thead><tr><th>收货单 / 供应商</th><th>发生时间</th><th>通知状态</th><th></th></tr></thead><tbody>${result.rows.map((event) => `<tr><td><strong>${esc(event.supplier)}</strong><br>${esc(event.receipt)}</td><td data-label="发生时间">${esc(event.occurred_at)}</td><td data-label="通知状态">${esc(statusLabels[event.status] || event.status)}</td><td><a class="btn btn-default" href="/desk/purchase-receipt-notice?name=${encodeURIComponent(event.name)}">查看通知</a></td></tr>`).join("")}</tbody></table>`);
					$('<button class="btn btn-default">上一页</button>').prop("disabled", !listState.history.length).appendTo(pager).on("click", () => { listState.start = listState.history.pop(); show(); });
					$('<button class="btn btn-default">下一页</button>').prop("disabled", result.next_start === null).appendTo(pager).on("click", () => { listState.history.push(listState.start); listState.start = result.next_start; show(); });
				} catch (error) {
					if (token === generation) host.html('<div class="purchase-empty">查询失败，请点击“刷新”重试。</div>');
				}
			}
			show();
			section.append('<p class="purchase-footnote">管理人员可查看本公司记录。通知关闭期间的事件保留记录，不会补发。</p>');
			return;
		}
		if (message.current_docstatus === 2) root.append('<div class="purchase-notice-warning">该收货单当前已撤销。以下保留当时记录，请以当前库存和生产工作台为准。</div>');
		root.append(`<section class="purchase-card"><h4>${esc(message.subject)}</h4><p class="purchase-meta">${esc(message.company)} · ${esc(statusLabels[message.status] || message.status)}</p><div class="receipt-body">${message.description}</div></section>`);
		if (message.can_open_receipt) $('<button class="btn btn-default">查看收货单</button>').appendTo(root).on("click", () => frappe.set_route("Form", "Purchase Receipt", message.receipt));
		$('<button class="btn btn-default ml-2">检查生产齐套</button>').appendTo(root).on("click", () => frappe.set_route("production-workbench"));
		if (message.can_retry) {
			const deliveries = $('<section class="purchase-card mt-3"><h4>通知投递</h4></section>').appendTo(root);
			(message.deliveries || []).forEach((row) => deliveries.append(`<p>${esc(row.recipient)} · ${esc(statusLabels[row.status] || row.status)} · 已尝试 ${row.attempts} 次${row.last_error ? `<details><summary>查看原因</summary><pre style="white-space:pre-wrap">${esc(row.last_error)}</pre></details>` : ""}</p>`));
			if (message.last_error) deliveries.append(`<p>${esc(message.last_error)}</p>`);
			if (["Failed", "Pending", "Partial", "No Recipients"].includes(message.status)) page.set_primary_action("重试未送达通知", async () => {
				await frappe.call({ method: api + "retry_notice", args: { name }, type: "POST", freeze: true });
				frappe.show_alert("已安排重试，成功送达的通知不会重复发送。"); await load();
			});
		}
	}
	page.add_inner_button(__("刷新"), load);
	page.add_inner_button(__("到货通知列表"), () => frappe.set_route("purchase-receipt-notice"));
	wrapper.load_receipt_notice = load;
};
frappe.pages["purchase-receipt-notice"].on_page_show = (wrapper) => wrapper.load_receipt_notice();
