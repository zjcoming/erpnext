function normalizeItemIdentityValue(value) {
	return String(value ?? "").trim();
}

function itemIdentityMeta(itemCode, itemName, translate = (message) => message) {
	const code = normalizeItemIdentityValue(itemCode);
	const name = normalizeItemIdentityValue(itemName);
	const hasMeaningfulName = Boolean(name && name !== code);
	return {
		code,
		name,
		has_meaningful_name: hasMeaningfulName,
		display_name: hasMeaningfulName ? name : translate("名称待维护"),
	};
}

function itemIdentityHtml(itemCode, itemName, helpers, options = {}) {
	const translate = helpers.translate || ((message) => message);
	const escape = (value) => helpers.escapeHtml(String(value ?? ""));
	const meta = itemIdentityMeta(itemCode, itemName, translate);
	const codeLabel = options.codeLabel || translate("物料编码");
	const className = [
		"item-identity",
		meta.has_meaningful_name ? "" : "is-missing-name",
		options.className || "",
	]
		.filter(Boolean)
		.join(" ");
	const displayName = escape(meta.display_name);
	const nameHtml = options.linkToItem && meta.code
		? `<a class="item-identity-name" href="/app/item/${encodeURIComponent(meta.code)}" title="${escape(
			translate(meta.has_meaningful_name ? "打开物料主数据" : "打开物料主数据维护名称")
		)}"><strong>${displayName}</strong></a>`
		: `<strong class="item-identity-name">${displayName}</strong>`;
	return `<div class="${escape(className)}">${nameHtml}<small class="item-identity-code">${escape(
		codeLabel
	)}：${escape(meta.code || translate("未设置"))}</small></div>`;
}

function itemIdentityText(itemCode, itemName, translate = (message) => message, codeLabel = null) {
	const meta = itemIdentityMeta(itemCode, itemName, translate);
	return `${meta.display_name}（${codeLabel || translate("物料编码")}：${meta.code || translate("未设置")}）`;
}

function itemIdentityIntroHtml(itemCode, itemName, helpers) {
	const translate = helpers.translate || ((message) => message);
	const escape = (value) => helpers.escapeHtml(String(value ?? ""));
	const meta = itemIdentityMeta(itemCode, itemName, translate);
	if (!meta.has_meaningful_name) {
		return `<strong>${escape(translate("物料名称待维护"))}</strong><br>${escape(
			translate("当前物料名称与物料编码相同，业务页面无法仅凭数字识别物料。请在“物料名称”中填写工厂常用名称；物料编码继续用于 BOM、库存和单据关联。")
		)}<br><small>${escape(translate("物料编码"))}：${escape(meta.code || translate("未设置"))}</small>`;
	}
	return `<strong>${escape(translate("物料名称"))}：${escape(meta.display_name)}</strong> · ${escape(
		translate("物料编码")
	)}：${escape(meta.code)}<br><small>${escape(
		translate("业务页面优先显示物料名称；物料编码保留用于 BOM、库存和单据追溯。")
	)}</small>`;
}

const processSimplificationItemIdentity = {
	itemIdentityMeta,
	itemIdentityHtml,
	itemIdentityText,
	itemIdentityIntroHtml,
};

if (typeof module !== "undefined" && module.exports) {
	module.exports = processSimplificationItemIdentity;
}

if (typeof window !== "undefined") {
	window.process_simplification = window.process_simplification || {};
	window.process_simplification.item_identity = processSimplificationItemIdentity;
}

if (typeof frappe !== "undefined" && frappe.ui?.form?.on) {
	frappe.ui.form.on("Item", {
		refresh(frm) {
			if (frm.is_new()) return;
			setTimeout(() => {
				if (!frm.doc || frm.doc.name !== frappe.get_route()[2]) return;
				const meta = itemIdentityMeta(frm.doc.item_code || frm.doc.name, frm.doc.item_name, __);
				frm.set_intro(
					itemIdentityIntroHtml(frm.doc.item_code || frm.doc.name, frm.doc.item_name, {
						translate: __,
						escapeHtml: frappe.utils.escape_html,
					}),
					meta.has_meaningful_name ? "blue" : "orange"
				);
			}, 0);
		},
	});
}
