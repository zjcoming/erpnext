const test = require("node:test");
const assert = require("node:assert/strict");

const {
	itemIdentityMeta,
	itemIdentityHtml,
	itemIdentityText,
	itemIdentityIntroHtml,
} = require("../../public/js/item_identity.js");

const escapeHtml = (value) =>
	String(value ?? "")
		.replaceAll("&", "&amp;")
		.replaceAll("<", "&lt;")
		.replaceAll(">", "&gt;")
		.replaceAll('"', "&quot;")
		.replaceAll("'", "&#39;");

const helpers = { translate: (message) => message, escapeHtml };

test("meaningful item names are primary and codes remain labeled trace keys", () => {
	const html = itemIdentityHtml("204001004", "PA6德尔隆", helpers, { linkToItem: true });

	assert.ok(html.indexOf("PA6德尔隆") < html.indexOf("物料编码：204001004"));
	assert.match(html, /href="\/app\/item\/204001004"/);
	assert.doesNotMatch(html, /名称待维护/);
	assert.equal(itemIdentityText("204001004", "PA6德尔隆"), "PA6德尔隆（物料编码：204001004）");
});

test("code-only master data is marked for maintenance instead of showing a fake name", () => {
	const meta = itemIdentityMeta("301008201014", "301008201014");
	const html = itemIdentityHtml("301008201014", "301008201014", helpers);

	assert.equal(meta.has_meaningful_name, false);
	assert.match(html, /名称待维护/);
	assert.match(html, /物料编码：301008201014/);
	assert.match(html, /is-missing-name/);
});

test("item form guidance explains the business name and trace code boundary", () => {
	const missing = itemIdentityIntroHtml("301008201014", "301008201014", helpers);
	const named = itemIdentityIntroHtml("204001004", "PA6德尔隆", helpers);

	assert.match(missing, /物料名称待维护/);
	assert.match(missing, /BOM、库存和单据关联/);
	assert.match(named, /物料名称：PA6德尔隆/);
	assert.match(named, /物料编码：204001004/);
});
