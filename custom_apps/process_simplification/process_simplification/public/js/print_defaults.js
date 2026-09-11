"use strict";

const PS_PRINT_FORMAT_LABELS = {
	"Standard": "标准",
	"Purchase Order Standard": "采购订单标准",
	"Purchase Order with Item Image": "采购订单（含物料图片）",
};

function installChinesePrintDefaults(frappeRef) {
	const prototype = frappeRef.ui?.form?.PrintView?.prototype;
	if (!prototype || prototype.ps_chinese_print_defaults) return;
	prototype.ps_chinese_print_defaults = true;
	const original = {
		show: prototype.show,
		add_sidebar_item: prototype.add_sidebar_item,
		set_default_print_format: prototype.set_default_print_format,
		selected_format: prototype.selected_format,
		set_default_letterhead: prototype.set_default_letterhead,
	};
	prototype.add_sidebar_item = function (df, ...args) {
		const control = original.add_sidebar_item.call(this, df, ...args);
		// Native PrintView assigns $input.val directly, bypassing Link's normal map setup.
		if (["language", "print_format"].includes(df.fieldname)) control.title_value_map ||= {};
		if (df.fieldname === "language") this.ps_language_control = control;
		if (df.fieldname === "print_format") {
			this.ps_format_control = control;
			for (const [name, label] of Object.entries(PS_PRINT_FORMAT_LABELS)) control.title_value_map[label] = name;
			const translate = control.get_translated.bind(control);
			control.get_translated = (value) => PS_PRINT_FORMAT_LABELS[value] || translate(value);
			const search = control.get_search_args.bind(control);
			control.get_search_args = (term) => search(control.title_value_map[term] || term);
		}
		return control;
	};
	prototype.show = function (...args) {
		this.ps_selected_language = null;
		return original.show.apply(this, args);
	};
	prototype.set_default_print_language = function () {
		this.lang_code = this.ps_selected_language || "zh";
		this.language_selector.val(this.lang_code);
		// Keep the actual Language ID while showing a readable label.
		this.ps_language_control?.translate_and_set_input_value(
			this.lang_code === "zh" ? "中文" : this.lang_code, this.lang_code
		);
	};
	prototype.set_user_lang = function () {
		this.lang_code = this.ps_language_control?.get_input_value() || this.language_selector.val() || "zh";
		this.ps_selected_language = this.lang_code;
	};
	prototype.selected_format = function () {
		return this.ps_format_control?.get_input_value() || original.selected_format.call(this);
	};
	prototype.set_default_print_format = function () {
		// Native PrintView reads raw input text; resolve its Link title before doing so.
		this.print_format_selector.val(this.ps_format_control?.get_input_value() || this.print_format_selector.val() || "");
		original.set_default_print_format.call(this);
		const name = this.print_format_selector.val();
		if (name) this.ps_format_control?.translate_and_set_input_value(name, name);
	};
	prototype.set_default_letterhead = async function () {
		await original.set_default_letterhead.call(this);
		if (!["Purchase Order", "Job Card", "Purchase Receipt", "Stock Entry", "Delivery Note"].includes(this.frm.doc.doctype)) return;
		if (!["", "Company Letterhead", "Company Letterhead - Grey"].includes(this.letterhead_selector.val() || "")) return;
		const { message } = await frappeRef.db.get_value("Letter Head", { name: "工厂简洁表头", disabled: 0 }, "name");
		if (message?.name) this.letterhead_selector.val(message.name);
	};
}

if (typeof module !== "undefined" && module.exports) module.exports = { installChinesePrintDefaults };
if (typeof frappe !== "undefined") installChinesePrintDefaults(frappe);
