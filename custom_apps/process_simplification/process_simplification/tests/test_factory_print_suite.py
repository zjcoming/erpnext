from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from jinja2 import FileSystemLoader, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from process_simplification import printing


class TestFactoryPrintSuite(TestCase):
	def render(self, doctype, **fields):
		env = SandboxedEnvironment(loader=FileSystemLoader(str(printing.FACTORY_LETTERHEAD_TEMPLATE.parents[2])), undefined=StrictUndefined)
		return env.get_template("templates/print/" + printing.FACTORY_FORMATS[doctype][1]).render(
			doc=frappe._dict(doctype=doctype, name="TEST-1", **fields),
			frappe=SimpleNamespace(utils=SimpleNamespace(fmt_money=lambda v, **kw: f'{kw.get("currency")} {float(v or 0):.2f}')),
			_=lambda value: {"Nos": "个", "Box": "箱"}.get(value, value),
			get_print_amount_in_words=lambda doc: "金额大写",
			letter_head="<div>本公司</div>", no_letterhead=False, footer="",
			print_settings=frappe._dict(repeat_header_footer=True),
		)

	def test_manufacture_keeps_consumed_and_finished_goods_directions_separate(self):
		html = self.render("Stock Entry", purpose="Manufacture", work_order="WO-1", items=[
			frappe._dict(item_code="RAW", qty=2, uom="Box", stock_uom="Nos", transfer_qty=20, s_warehouse="原料仓"),
			frappe._dict(item_code="FG", qty=8, uom="Nos", t_warehouse="成品仓", is_finished_item=1),
		])
		for text in ("生产完工入库单", "RAW", "FG", "2 箱", "20 个", "8 个", "原料仓", "成品仓", "不合并汇总"):
			self.assertIn(text, html)
		self.assertNotIn("valuation_rate", html)

	def test_purchase_return_preserves_negative_accepted_rejected_and_warehouses(self):
		html = self.render("Purchase Receipt", is_return=1, return_against="PR-ORIGINAL", items=[
			frappe._dict(item_code="A", qty=-5, rejected_qty=-2, uom="Nos", warehouse="接收仓", rejected_warehouse="拒收仓", purchase_order="PO-1"),
		])
		for text in ("采购退货单", "PR-ORIGINAL", "退回接收品", "退回拒收品", "-5 个", "-2 个", "接收仓", "拒收仓", "PO-1"):
			self.assertIn(text, html)

	def test_delivery_return_marks_direction_and_no_valuation_fields(self):
		html = self.render("Delivery Note", is_return=1, customer_name="客户", items=[
			frappe._dict(item_code="A", qty=-3, uom="Nos", warehouse="退货仓", against_sales_order="SO-1", valuation_rate=987654321),
		])
		for text in ("销售退货入库单", "客户退货入库", "-3 个", "SO-1", "退货仓"):
			self.assertIn(text, html)
		self.assertNotIn("987654321", html)

	def test_purchase_order_shows_saved_totals_discount_rounding_and_currency(self):
		html = self.render("Purchase Order", currency="CNY", total=100, discount_amount=5, grand_total=101.23,
			rounding_adjustment=-0.23, rounded_total=101, items=[frappe._dict(item_code="A", qty=10, uom="Nos", rate=10, amount=100)],
			taxes=[frappe._dict(description="增值税", tax_amount_after_discount_amount=6.23)])
		for text in ("CNY 100.00", "CNY 5.00", "CNY 6.23", "CNY 101.23", "CNY -0.23", "CNY 101.00"):
			self.assertIn(text, html)

	def test_long_lists_preserve_rows_and_escape_names_and_remarks(self):
		rows = [frappe._dict(item_code=f"M{i:03d}", item_name="<script>bad</script>", qty=i) for i in range(75)]
		for doctype in ("Purchase Order", "Purchase Receipt", "Stock Entry", "Delivery Note"):
			html = self.render(doctype, items=rows, remarks="<b>检查</b>", terms="<b>检查</b>")
			self.assertIn("M074", html)
			self.assertIn("display: table-header-group", html)
			self.assertNotIn("<script>bad", html)
			self.assertNotIn("<b>检查", html)

	def test_install_registers_five_defaults_once_and_preserves_custom_or_disabled_formats(self):
		with patch.object(frappe, "db", Mock(), create=True) as db, patch.object(frappe, "get_doc") as doc, patch.object(frappe, "get_meta") as meta, patch("frappe.custom.doctype.property_setter.property_setter.make_property_setter") as setter:
			db.exists.return_value = False; meta.return_value.default_print_format = "Standard"
			printing.ensure_factory_print_formats()
			self.assertEqual(doc.return_value.insert.call_count, 5)
			self.assertEqual(setter.call_count, 5)
			for kind, (name, template) in printing.FACTORY_FORMATS.items():
				doc.reset_mock(); setter.reset_mock(); db.exists.return_value = True
				meta.return_value.default_print_format = name
				db.get_value.return_value = frappe._dict(doc_type=kind, html=printing._format_html(template), disabled=0)
				printing._ensure_factory_format(kind)
				doc.assert_not_called(); setter.assert_not_called()
				meta.return_value.default_print_format = "客户自定义"
				printing._ensure_factory_format(kind); setter.assert_not_called()
				meta.return_value.default_print_format = "Standard"
				db.get_value.return_value.disabled = 1
				printing._ensure_factory_format(kind); setter.assert_not_called()
				db.get_value.return_value.disabled = 0; db.get_value.return_value.html = "客户同名格式"
				printing._ensure_factory_format(kind); setter.assert_not_called()

	def test_native_purchase_and_delivery_defaults_upgrade_but_custom_same_name_does_not(self):
		with patch.object(frappe, "db", Mock(), create=True) as db, patch.object(frappe, "get_doc"), patch.object(frappe, "get_meta") as meta, patch("frappe.custom.doctype.property_setter.property_setter.make_property_setter") as setter:
			db.exists.return_value = False
			for kind, native in [("Purchase Order", "Purchase Order Standard"), ("Delivery Note", "Delivery Note with Item Image")]:
				meta.return_value.default_print_format = native
				db.get_value.return_value = "Yes"; setter.reset_mock()
				printing._ensure_factory_format(kind)
				self.assertEqual(setter.call_args.args[3], printing.FACTORY_FORMATS[kind][0])
				db.get_value.return_value = "No"; setter.reset_mock()
				printing._ensure_factory_format(kind); setter.assert_not_called()

	def test_server_defaults_match_preview_and_preserve_explicit_custom_or_no_header(self):
		kind = "Stock Entry"; name, file = printing.FACTORY_FORMATS[kind]
		doc = frappe._dict(doctype=kind, name="SE-1")
		doc.as_dict = lambda: dict(doc)
		args = dict(doc=doc, letter_head="OLD", footer="OLD FOOTER", no_letterhead=False)
		format = frappe._dict(name=name, html=printing._format_html(file))
		with patch.object(frappe, "form_dict", frappe._dict(), create=True), patch.object(frappe, "db", Mock(), create=True) as db, patch.object(frappe, "render_template", return_value="NEW") as render, patch("frappe.utils.pdf.pdf_body_html", side_effect=lambda template, values, **kw: values) as delegate:
			db.get_value.side_effect = ["Company Letterhead - Grey", frappe._dict(content="HEADER", disabled=0)]
			self.assertEqual(printing.factory_pdf_body_html(None, args, format)["letter_head"], "NEW")
			self.assertEqual(args["letter_head"], "OLD")
			self.assertEqual(delegate.call_args.args[1]["footer"], "")
			for selected, disabled, custom in [("客户表头", False, False), ("", True, False), ("", False, True)]:
				render.reset_mock(); db.reset_mock()
				frappe.form_dict.letterhead = selected
				printing.factory_pdf_body_html(None, {**args, "no_letterhead": disabled}, frappe._dict(name=name, html="CUSTOM") if custom else format)
				render.assert_not_called()
