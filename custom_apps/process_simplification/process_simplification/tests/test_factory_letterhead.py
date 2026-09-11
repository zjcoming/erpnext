from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from process_simplification import printing


class TestFactoryLetterhead(TestCase):
	def setUp(self):
		self.company = frappe._dict(
			company_name="甲方机械有限公司", company_logo=None, phone_no=None, email=None, website=None
		)
		self.addresses = []
		self.get_value = Mock(spec=lambda: None, side_effect=lambda *args, **kwargs: self.company)
		self.get_all = Mock(spec=lambda: None, side_effect=self._get_all)
		self.template = SandboxedEnvironment(undefined=StrictUndefined).from_string(
			printing.FACTORY_LETTERHEAD_TEMPLATE.read_text(encoding="utf-8")
		)
		self.template.environment.globals["get_document_scan_qr"] = lambda doc: None

	def _get_all(self, doctype, **kwargs):
		if doctype == "Dynamic Link":
			return [row.name for row in self.addresses]
		return self.addresses

	def render(self, **fields):
		return self.template.render(
			doc=frappe._dict(doctype="Purchase Order", company="Factory A", **fields),
			frappe=SimpleNamespace(
				db=SimpleNamespace(get_value=self.get_value),
				get_all=self.get_all,
				utils=SimpleNamespace(get_url=lambda value: "https://erp.example.com" + value),
			),
			_=lambda value: value,
		)

	def test_minimal_company_has_no_empty_contact_labels_or_logo_space(self):
		html = self.render()
		self.assertIn(self.company.company_name, html)
		self.assertNotIn("地址：", html)
		self.assertNotIn("电话：", html)
		self.assertNotIn("邮箱：", html)
		self.assertNotIn("网址：", html)
		self.assertNotIn("<img", html)
		self.assertNotIn('<td class="ps-factory-logo-cell">', html)

	def test_company_and_address_lookup_follow_the_document_company(self):
		self.render()
		self.assertEqual(self.get_value.call_args.args[:2], ("Company", "Factory A"))
		self.assertEqual(
			self.get_all.call_args.kwargs["filters"],
			{"parenttype": "Address", "link_doctype": "Company", "link_name": "Factory A"},
		)
		self.company.company_name = "乙方工厂"
		html = self.template.render(
			doc=frappe._dict(company="Factory B"),
			frappe=SimpleNamespace(db=SimpleNamespace(get_value=self.get_value), get_all=self.get_all),
		)
		self.assertIn("乙方工厂", html)
		self.assertNotIn("甲方机械", html)
		self.assertEqual(self.get_value.call_args.args[1], "Factory B")
		self.assertEqual(self.get_all.call_args.kwargs["filters"]["link_name"], "Factory B")

	def test_uses_company_address_instead_of_customer_or_supplier_billing_address(self):
		self.addresses = [
			frappe._dict(name="Factory address", address_line1="工厂路1号", phone="023-12345678")
		]
		html = self.render(company_address="Unrelated supplier", billing_address="Customer address")
		self.assertIn("工厂路1号", html)
		self.assertIn("电话：023-12345678", html)
		self.assertNotIn("Unrelated supplier", html)
		address_query = self.get_all.call_args.kwargs
		self.assertEqual(address_query["filters"], {"name": ["in", ["Factory address"]], "disabled": 0})

	def test_document_branch_address_wins_if_linked_to_the_company(self):
		self.addresses = [
			frappe._dict(name="Primary", address_line1="总部路1号"),
			frappe._dict(name="Branch", address_line1="分厂路2号"),
		]
		html = self.render(company_address="Branch")
		self.assertIn("分厂路2号", html)
		self.assertNotIn("总部路1号", html)

	def test_escapes_company_contact_and_logo_values(self):
		self.company.update(
			company_name='<script>alert("name")</script>',
			phone_no="<b>123</b>",
			company_logo='/files/logo.png" onerror="alert(1)',
		)
		html = self.render()
		self.assertNotIn("<script>", html)
		self.assertNotIn("<b>123</b>", html)
		self.assertNotIn('src="https://erp.example.com/files/logo.png" onerror=', html)
		self.assertIn("&lt;script&gt;", html)
		self.assertIn("<img", html)

	def test_missing_company_does_not_print_another_company_identity(self):
		self.assertEqual(self.template.render(doc={}).strip(), "")
		self.get_value.return_value = None
		self.get_value.side_effect = None
		self.assertEqual(self.render().strip(), "")

	def test_city_is_not_duplicated_when_it_is_already_in_address(self):
		self.addresses = [
			frappe._dict(name="Primary", state="重庆市", city="重庆市", address_line1="重庆市工业路1号")
		]
		self.assertEqual(self.render().count("重庆市"), 1)


class TestFactoryLetterheadInstallation(TestCase):
	def test_migration_preserves_existing_user_edits_and_disabled_choice(self):
		with patch.object(frappe, "db", Mock(), create=True), patch.object(frappe, "get_doc") as get_doc:
			frappe.db.exists.return_value = printing.FACTORY_LETTERHEAD_NAME
			printing.ensure_factory_letterhead()
			get_doc.assert_not_called()

	def test_existing_default_is_preserved_and_new_record_uses_html(self):
		with patch.object(frappe, "db", Mock(), create=True), patch.object(frappe, "get_doc") as get_doc:
			frappe.db.exists.side_effect = [None, "Existing custom letterhead"]
			printing.ensure_factory_letterhead()
			values = get_doc.call_args.args[0]
			self.assertEqual(values["is_default"], 0)
			self.assertEqual(values["source"], "HTML")
			self.assertEqual(values["letter_head_name"], printing.FACTORY_LETTERHEAD_NAME)
			get_doc.return_value.insert.assert_called_once_with(ignore_permissions=True)

	def test_becomes_default_only_when_no_default_exists(self):
		with patch.object(frappe, "db", Mock(), create=True), patch.object(frappe, "get_doc") as get_doc:
			frappe.db.exists.return_value = None
			printing.ensure_factory_letterhead()
			self.assertEqual(get_doc.call_args.args[0]["is_default"], 1)
