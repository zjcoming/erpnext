const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const filename = path.join(__dirname, "../../process_simplification/doctype/process_simplification_settings/process_simplification_settings.js");
const escape = (value) => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
function setup(call) {
	const handlers = {};
	const context = vm.createContext({ frappe: { ui: { form: { on: (_, events) => Object.assign(handlers, events) } }, call, utils: { escape_html: escape } } });
	vm.runInContext(readFileSync(filename, "utf8"), context);
	const wrapper = { value: "", html(value) { this.value = value; return this; }, off() { return this; }, on() { return this; } };
	const frm = { fields_dict: { initialization_status: { $wrapper: wrapper } } };
	return { context, frm, wrapper, handlers };
}
const good = { company: "Factory", checks: [{ label: "自动预留库存", value: "已关闭", detail: "按业务动作预留", status: "info", doctype: "Stock Settings", name: "Stock Settings" }], next_steps: [] };

test("manual reservation is not shown as a setup error; data is escaped", () => {
	const { context } = setup();
	const html = context.psInitializationHtml({ ...good, company: '<img src=x onerror="bad()">' }, escape);
	assert.match(html, /基础配置检查通过/);
	assert.match(html, /不代表这些业务已验收/);
	assert.match(html, /&lt;img/);
	assert.doesNotMatch(html, /<img/);
});

test("loading failure cannot leave a success result", async () => {
	const { context, frm, wrapper } = setup(async () => { throw Error("offline"); });
	wrapper.value = "基础配置检查通过";
	await context.psRefreshInitialization(frm);
	assert.match(wrapper.value, /尚不能确认/);
	assert.doesNotMatch(wrapper.value, /基础配置检查通过/);
});

test("company selection does not allow a stale response to overwrite current results", async () => {
	const requests = [];
	const { context, frm, wrapper } = setup((request) => new Promise((resolve) => requests.push({ request, resolve })));
	frm.__psInitializationCompany = "A";
	const first = context.psRefreshInitialization(frm);
	frm.__psInitializationCompany = "B";
	const second = context.psRefreshInitialization(frm);
	requests[1].resolve({ message: { ...good, company: "B" } });
	await second;
	requests[0].resolve({ message: { ...good, company: "A" } });
	await first;
	assert.equal(frm.__psInitializationCompany, "B");
	assert.match(wrapper.value, /当前公司：B/);
	assert.equal(requests[1].request.type, "GET");
	assert.equal(requests[1].request.args.company, "B");
});

test("missing reservation appears as a blocking configuration item", () => {
	const { context } = setup();
	const html = context.psInitializationHtml({ ...good, checks: [{ ...good.checks[0], label: "启用库存预留", status: "error" }] }, escape);
	assert.match(html, /还有 1 项需要处理/);
	assert.match(html, /启用库存预留 · 需要处理/);
});

test("inaccessible settings have no dead link and read-only settings identify the administrator", () => {
	const { context } = setup();
	const hidden = context.psInitializationHtml({ ...good, checks: [{ ...good.checks[0], can_open: false, can_configure: false }] }, escape);
	assert.doesNotMatch(hidden, /data-setup-doctype/);
	assert.match(hidden, /请系统管理员处理/);
	const readonly = context.psInitializationHtml({ ...good, checks: [{ ...good.checks[0], can_open: true, can_configure: false }] }, escape);
	assert.match(readonly, /查看自动预留库存/);
});

test("a valid company-specific fallback still asks for review instead of showing all clear", () => {
	const { context } = setup();
	const html = context.psInitializationHtml({ ...good, checks: [{ ...good.checks[0], status: "warning" }] }, escape);
	assert.match(html, /还有 1 项需要核对/);
	assert.doesNotMatch(html, /<strong>基础配置检查通过/);
});
