const test = require("node:test");
const assert = require("node:assert/strict");
const { installChinesePrintDefaults } = require("../../public/js/print_defaults.js");

function printPage({ doctype = "Purchase Order", letterhead = "Company Letterhead - Grey", factory = true } = {}) {
	const calls = [];
	class PrintView {
		constructor() {
			this.frm = { doc: { doctype, language: "en" }, meta: { default_print_format: "Purchase Order with Item Image" } };
			this.print_format_selector = this.add_sidebar_item({ fieldname: "print_format" }).$input;
			this.language_selector = this.add_sidebar_item({ fieldname: "language" }).$input;
			this.letterhead_selector = this.add_sidebar_item({ fieldname: "letterhead" }).$input;
		}
		add_sidebar_item() {
			let text = "";
			const input = { val(value) { if (arguments.length) { text = value; return this; } return text; } };
			return {
				$input: input, get_translated: (value) => value,
				get_search_args: (txt) => ({ doctype: "Print Format", txt, filters: { doc_type: doctype } }),
				translate_and_set_input_value(title, value) { const label = this.get_translated(title); this.title_value_map[label] = value; input.val(label); },
				get_input_value() { return this.title_value_map?.[input.val()] || input.val(); },
			};
		}
		async show() { this.set_default_print_format(); this.set_default_print_language(); await this.set_default_letterhead(); }
		set_default_print_format() {
			if (!["Standard", "Purchase Order Standard", "Purchase Order with Item Image"].includes(this.print_format_selector.val())) {
				this.print_format_selector.val(this.frm.meta.default_print_format || "");
			}
		}
		selected_format() { return this.print_format_selector.val() || "Standard"; }
		async set_default_letterhead() { this.letterhead_selector.val(letterhead); }
	}
	const frappe = { ui: { form: { PrintView } }, db: { async get_value(...args) { calls.push(args); return { message: factory ? { name: "工厂简洁表头" } : {} }; } } };
	installChinesePrintDefaults(frappe);
	return { page: new PrintView(), calls, frappe };
}

test("entry uses Chinese even for an English document and keeps the native default format ID", async () => {
	const { page } = printPage();
	await page.show();
	assert.equal(page.lang_code, "zh");
	assert.equal(page.language_selector.val(), "中文");
	assert.equal(page.selected_format(), "Purchase Order with Item Image");
	assert.equal(page.print_format_selector.val(), "采购订单（含物料图片）");
	assert.equal(page.frm.doc.language, "en", "printing must not rewrite the document");
	page.set_default_print_format();
	assert.equal(page.selected_format(), "Purchase Order with Item Image");
});

test("manual language survives format refresh but next entry defaults to Chinese", async () => {
	const { page } = printPage();
	await page.show();
	page.ps_language_control.translate_and_set_input_value("英语", "en");
	page.set_user_lang();
	page.set_default_print_language();
	assert.equal(page.lang_code, "en");
	page.print_format_selector.val("Purchase Order Standard");
	page.set_default_print_format();
	page.set_default_print_language();
	assert.equal(page.lang_code, "en");
	assert.equal(page.selected_format(), "Purchase Order Standard");
	assert.equal(page.print_format_selector.val(), "采购订单标准");
	await page.show();
	assert.equal(page.lang_code, "zh");
	page.set_user_lang();
	assert.equal(page.lang_code, "zh", "Chinese label must not become the language code");
});

test("empty native default falls back to Standard without inventing a format", async () => {
	const { page } = printPage();
	page.frm.meta.default_print_format = null;
	await page.show();
	assert.equal(page.selected_format(), "Standard");
});

test("Chinese format names resolve and search by their real IDs while preserving filters", () => {
	const { page } = printPage();
	page.print_format_selector.val("采购订单标准");
	assert.equal(page.selected_format(), "Purchase Order Standard");
	assert.deepEqual(page.ps_format_control.get_search_args("采购订单标准"), {
		doctype: "Print Format", txt: "Purchase Order Standard", filters: { doc_type: "Purchase Order" },
	});
	assert.equal(page.ps_format_control.get_search_args("我的定制格式").txt, "我的定制格式");
});

test("PO and job prints prefer factory header over native placeholder headers", async () => {
	for (const doctype of ["Purchase Order", "Job Card", "Purchase Receipt", "Stock Entry", "Delivery Note"]) {
		const { page, calls } = printPage({ doctype });
		await page.show();
		assert.equal(page.letterhead_selector.val(), "工厂简洁表头");
		assert.deepEqual(calls, [["Letter Head", { name: "工厂简洁表头", disabled: 0 }, "name"]]);
	}
});

test("custom headers, other doctypes and unavailable factory headers keep native choice", async () => {
	for (const options of [{ letterhead: "客户定制表头" }, { doctype: "Sales Invoice" }, { factory: false }]) {
		const { page, calls } = printPage(options);
		await page.show();
		assert.equal(page.letterhead_selector.val(), options.letterhead || "Company Letterhead - Grey");
		if (options.factory !== false) assert.equal(calls.length, 0);
	}
});

test("page hook is safe to load twice", async () => {
	const { page, frappe, calls } = printPage();
	installChinesePrintDefaults(frappe);
	await page.show();
	assert.equal(calls.length, 1);
	assert.equal(page.selected_format(), "Purchase Order with Item Image");
});
