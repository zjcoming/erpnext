from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from process_simplification import printing


class TestFactoryJobPrint(TestCase):
	def render(self, **fields):
		template = SandboxedEnvironment(undefined=StrictUndefined).from_string(
			(printing.FACTORY_LETTERHEAD_TEMPLATE.parent / "factory_job_card.html").read_text()
		)
		return template.render(
			doc=frappe._dict(name="PO-JOB00208", **fields),
			frappe=SimpleNamespace(utils=frappe.utils), _=lambda value: {"Nos": "个"}.get(value, value),
			letter_head="<div>用户自己的公司</div>", no_letterhead=False, footer="",
			print_settings=SimpleNamespace(repeat_header_footer=True),
		)

	def test_task_information_is_compact_and_internal_configuration_is_not_printed(self):
		html = self.render(item_name="焊线线圈", production_item="301001790", operation="焊接", for_quantity=1,
			stock_uom="Nos", naming_series="PO-JOB.#####", operation_id="INTERNAL-ID", worker_reporting_enabled=1)
		for expected in ("用户自己的公司", "PO-JOB00208", "焊线线圈", "301001790", "焊接", "草稿", "任务数量", "个"):
			self.assertIn(expected, html)
		for excluded in ("INTERNAL-ID", "PO-JOB.#####", "Worker Reporting", "工工资率", "工序用料"):
			self.assertNotIn(excluded, html)

	def test_long_material_lists_keep_every_row_and_repeat_column_headers(self):
		items = [frappe._dict(item_code=f"M-{i}", item_name=f"用料{i}", required_qty=i + 0.25, uom="Nos") for i in range(70)]
		html = self.render(items=items)
		self.assertEqual(html.count('class="ps-job-right">'), 71)
		self.assertIn("用料69", html)
		self.assertIn("display: table-header-group", html)
		self.assertIn("69.25", html)

	def test_data_is_escaped_and_rich_text_remarks_do_not_inject_markup(self):
		html = self.render(item_name='<img src=x onerror="evil()">', remarks="<b>先检查尺寸</b>", items=[frappe._dict(item_code="<script>bad</script>")])
		self.assertNotIn("<img src=x", html)
		self.assertNotIn("<script>bad", html)
		self.assertIn("先检查尺寸", html)
		self.assertNotIn("<b>先检查", html)

	def test_install_sets_default_only_for_native_standard_and_preserves_user_format(self):
		with patch.object(frappe, "db", Mock(), create=True) as db, patch.object(frappe, "get_doc") as get_doc, patch.object(frappe, "get_meta") as meta, patch("frappe.custom.doctype.property_setter.property_setter.make_property_setter") as setter:
			db.exists.return_value = False
			meta.return_value.default_print_format = "Standard"
			printing.ensure_factory_job_print_format()
			get_doc.return_value.insert.assert_called_once_with(ignore_permissions=True)
			setter.assert_called_once_with("Job Card", None, "default_print_format", printing.FACTORY_JOB_FORMAT_NAME, "Data", for_doctype=True)
			db.exists.return_value = True
			meta.return_value.default_print_format = "用户自己的格式"
			get_doc.reset_mock(); setter.reset_mock()
			printing.ensure_factory_job_print_format()
			get_doc.assert_not_called(); setter.assert_not_called()
