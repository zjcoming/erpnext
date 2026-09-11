from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from jinja2.sandbox import SandboxedEnvironment

from process_simplification import print_localization as printing


class TestChinesePrintAmount(TestCase):
	def test_yuan_groups_zeroes_cents_sign_and_rounding(self):
		for value, words in {
			"0": "零元整",
			"0.05": "零元伍分",
			"0.80": "零元捌角",
			"10": "壹拾元整",
			"1001.01": "壹仟零壹元零壹分",
			"10001": "壹万零壹元整",
			"100000001": "壹亿零壹元整",
			"100010001": "壹亿零壹万零壹元整",
			"97671.80": "玖万柒仟陆佰柒拾壹元捌角",
			"-12.34": "负壹拾贰元叁角肆分",
			"9.995": "壹拾元整",
			"-0.001": "零元整",
		}.items():
			with self.subTest(value=value):
				self.assertEqual(printing.chinese_yuan(value), "人民币" + words)

	def test_rejects_invalid_and_out_of_range_amounts(self):
		for value in [None, "bad", "NaN", "Infinity", "-Infinity", "10000000000000000"]:
			with self.subTest(value=value), self.assertRaises(ValueError):
				printing.chinese_yuan(value)

	def test_matches_displayed_grand_total_and_never_mutates_document(self):
		doc = frappe._dict(currency="CNY", grand_total=97671.8, rounded_total=97672, in_words="old words")
		before = dict(doc)
		self.assertEqual(printing.get_print_amount_in_words(doc), "人民币玖万柒仟陆佰柒拾壹元捌角")
		self.assertEqual(dict(doc), before)

	def test_foreign_currency_keeps_existing_currency_words(self):
		doc = frappe._dict(currency="USD", grand_total=10, in_words="USD Ten only")
		self.assertEqual(printing.get_print_amount_in_words(doc), doc.in_words)


class TestLocalizedPurchaseFormat(TestCase):
	def setUp(self):
		self.format = frappe._dict(name="Purchase Order Standard", standard="Yes", doc_type="Purchase Order")
		self.environment = SandboxedEnvironment()
		self.environment.globals.update(
			_=lambda value: {"Sr No": "序号", "Nos": "个"}.get(value, value),
			get_print_amount_in_words=printing.get_print_amount_in_words,
		)

	def test_localized_row_number_units_and_amount_words_are_escaped(self):
		source = '{{ _("No") }} {{ item.uom }} {{ doc.in_words }}'
		with (
			patch.object(frappe.local, "lang", "zh", create=True),
			patch("frappe.www.printview.get_print_format", return_value=source),
		):
			template = printing.get_print_format_template(self.environment, self.format)
			doc = frappe._dict(currency="CNY", grand_total=100, in_words="old")
			self.assertEqual(template.render(doc=doc, item={"uom": "Nos"}), "序号 个 人民币壹佰元整")
			doc.currency, doc.in_words = "USD", "<script>bad</script>"
			html = template.render(doc=doc, item={"uom": "<b>unit</b>"})
			self.assertNotIn("<script>", html)
			self.assertNotIn("<b>", html)

	def test_leaves_english_user_custom_formats_and_other_doctypes_untouched(self):
		for lang, changes in [
			("en", {}),
			("zh", {"standard": "No"}),
			("zh", {"name": "My Purchase Format"}),
			("zh", {"doc_type": "Sales Order"}),
		]:
			with (
				self.subTest(lang=lang, changes=changes),
				patch.object(frappe.local, "lang", lang, create=True),
			):
				self.assertIsNone(
					printing.get_print_format_template(self.environment, {**self.format, **changes})
				)

	def test_optional_company_fields_do_not_open_a_modal_or_write_data(self):
		with (
			patch.object(frappe, "db", Mock(), create=True) as db,
			patch.object(frappe, "get_doc") as get_doc,
		):
			self.assertIsNone(printing.skip_automatic_company_details("Purchase Order", "PO1"))
			self.assertEqual(db.mock_calls, [])
			get_doc.assert_not_called()
