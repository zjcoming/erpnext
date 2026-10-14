"""Native Work Order submission with the restricted factory supervisor role."""

from unittest.mock import patch

import frappe
from frappe.client import insert, submit
from frappe.handler import run_doc_method
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from erpnext.manufacturing.doctype.work_order import work_order as native_work_order
from process_simplification.api.production_plan_adapter import (
	_AUTO_JOB_CARD_WORK_ORDER_FLAG,
	_allow_generated_job_cards_for,
	_submit_work_orders,
)
from process_simplification.management_access import (
	PRODUCTION_MANAGER_ROLE,
	ensure_management_access,
)
from process_simplification.tests import test_worker_reporting as reporting_fixtures


class TestNativeWorkOrderSubmit(IntegrationTestCase):
	TEST_COMPANY = reporting_fixtures.TestWorkerReporting.TEST_COMPANY
	TEST_COMPANY_ABBR = reporting_fixtures.TestWorkerReporting.TEST_COMPANY_ABBR
	OTHER_COMPANY = reporting_fixtures.TestWorkerReporting.OTHER_COMPANY
	OTHER_COMPANY_ABBR = reporting_fixtures.TestWorkerReporting.OTHER_COMPANY_ABBR
	_ensure_company = classmethod(reporting_fixtures.TestWorkerReporting._ensure_company.__func__)
	_make_user = reporting_fixtures.TestWorkerReporting._make_user
	_email = reporting_fixtures.TestWorkerReporting._email
	_grant_company = reporting_fixtures.TestWorkerReporting._grant_company

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._ensure_company(cls.TEST_COMPANY, cls.TEST_COMPANY_ABBR)
		cls._ensure_company(cls.OTHER_COMPANY, cls.OTHER_COMPANY_ABBR)
		ensure_management_access()
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.addCleanup(self._rollback)
		self.enterContext(self.change_settings("Manufacturing Settings", {"disable_capacity_planning": 1}))
		self.supervisor = self._make_user(PRODUCTION_MANAGER_ROLE)
		self._grant_company(self.supervisor, self.TEST_COMPANY)

	def _rollback(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _draft(self, company=None):
		company = company or self.TEST_COMPANY
		with self.set_user("Administrator"):
			prefix = "NATIVE-WO-" + frappe.generate_hash(length=8)
			warehouse = frappe.get_doc(dict(doctype="Warehouse", warehouse_name=prefix,
				company=company)).insert().name
			items = []
			for suffix in ("RM", "FG"):
				items.append(frappe.get_doc(dict(doctype="Item", item_code=prefix + "-" + suffix,
					item_name=prefix + "-" + suffix, item_group="Products", stock_uom="Nos",
					is_stock_item=1, is_purchase_item=1, include_item_in_manufacturing=1,
					valuation_rate=1)).insert().name)
			workstation = frappe.get_doc(dict(doctype="Workstation", workstation_name=prefix)).insert().name
			operation = frappe.get_doc(dict(doctype="Operation", name=prefix, workstation=workstation)).insert().name
			bom = frappe.get_doc(dict(doctype="BOM", item=items[1], company=company,
				currency="INR", quantity=1, is_active=1, is_default=1, with_operations=1,
				operations=[dict(operation=operation, workstation=workstation, time_in_mins=1)],
				items=[dict(item_code=items[0], qty=1, uom="Nos", stock_uom="Nos", rate=1,
					operation=operation, source_warehouse=warehouse)]))
			bom.insert().submit()
			wo = frappe.get_doc(dict(doctype="Work Order", company=company,
				production_item=items[1], bom_no=bom.name, qty=2, stock_uom="Nos",
				source_warehouse=warehouse, wip_warehouse=warehouse, fg_warehouse=warehouse,
				reserve_stock=0, transfer_material_against="Work Order", planned_start_date=now_datetime()))
			wo.get_items_and_operations_from_bom()
			wo.insert()
			return wo

	def _cards(self, work_order):
		return frappe.get_all("Job Card", filters={"work_order": work_order},
			fields=["name", "company", "operation_id", "for_quantity", "docstatus", "owner", "modified_by"])

	def test_native_submit_creates_only_bom_job_cards_as_real_supervisor(self):
		wo = self._draft()
		with self.set_user(self.supervisor):
			self.assertTrue(frappe.has_permission("Work Order", "submit", doc=wo))
			for ptype in ("create", "write", "submit"):
				self.assertFalse(frappe.has_permission("Job Card", ptype))
			result = submit(wo.as_dict())
			self.assertEqual(result["docstatus"], 1)
			self.assertEqual(frappe.session.user, self.supervisor)
			self.assertNotIn(_AUTO_JOB_CARD_WORK_ORDER_FLAG, frappe.flags)
			cards = self._cards(wo.name)
			self.assertEqual(len(cards), 1)
			card = cards[0]
			self.assertEqual((card.company, card.operation_id, card.for_quantity, card.docstatus),
				(self.TEST_COMPANY, wo.operations[0].name, 2, 0))
			self.assertEqual((card.owner, card.modified_by), (self.supervisor, self.supervisor))
			manual = frappe.copy_doc(frappe.get_doc("Job Card", card.name))
			with self.assertRaises(frappe.PermissionError):
				insert(manual.as_dict())
			self.assertEqual(self._cards(wo.name), cards)

	def test_non_submit_and_remote_hook_do_not_gain_job_card_permission(self):
		wo = self._draft()
		with self.set_user(self.supervisor):
			with self.assertRaises(frappe.PermissionError):
				run_doc_method("create_job_card", dt="Work Order", dn=wo.name)
			with self.assertRaises(frappe.PermissionError):
				wo.create_job_card()
		self.assertEqual(self._cards(wo.name), [])
		self.assertEqual(frappe.db.get_value("Work Order", wo.name, "docstatus"), 0)
		self.assertNotIn(_AUTO_JOB_CARD_WORK_ORDER_FLAG, frappe.flags)

	def test_foreign_company_submit_is_denied_before_automatic_card_creation(self):
		wo = self._draft(self.OTHER_COMPANY)
		with self.set_user(self.supervisor), patch.object(native_work_order.WorkOrder, "create_job_card") as create:
			with self.assertRaises(frappe.PermissionError):
				submit(wo.as_dict())
			create.assert_not_called()
		self.assertEqual(frappe.db.get_value("Work Order", wo.name, "docstatus"), 0)
		self.assertEqual(self._cards(wo.name), [])
		self.assertNotIn(_AUTO_JOB_CARD_WORK_ORDER_FLAG, frappe.flags)

	def test_employee_company_scope_is_preserved_without_native_company_permission(self):
		user = self._make_user(PRODUCTION_MANAGER_ROLE)
		frappe.get_doc(dict(doctype="Employee", first_name=user, company=self.TEST_COMPANY,
			user_id=user, date_of_birth="1990-05-08", date_of_joining="2013-01-01",
			gender="Female", status="Active", create_user_permission=0)).insert(ignore_permissions=True)
		self.assertFalse(frappe.db.exists("User Permission", dict(user=user, allow="Company")))
		own, foreign = self._draft(), self._draft(self.OTHER_COMPANY)
		with self.set_user(user):
			self.assertTrue(frappe.has_permission("Work Order", "submit", doc=foreign))
			self.assertFalse(frappe.has_permission("Work Order", "read", doc=foreign))
			frappe.db.savepoint("foreign_native_submit")
			with self.assertRaises(frappe.PermissionError):
				submit(foreign.as_dict())
			frappe.db.rollback(save_point="foreign_native_submit")
			self.assertNotIn(_AUTO_JOB_CARD_WORK_ORDER_FLAG, frappe.flags)
			self.assertEqual(submit(own.as_dict())["docstatus"], 1)
		self.assertEqual(frappe.db.get_value("Work Order", foreign.name, "docstatus"), 0)
		self.assertEqual(self._cards(foreign.name), [])
		self.assertEqual(len(self._cards(own.name)), 1)

	def test_exact_work_order_scope_does_not_allow_another_job_card(self):
		own, other = self._draft(), self._draft()
		with self.set_user("Administrator"):
			other.submit()
			cards = self._cards(other.name)
			manual = frappe.copy_doc(frappe.get_doc("Job Card", cards[0].name))
		with self.set_user(self.supervisor), _allow_generated_job_cards_for(own.name):
			with self.assertRaises(frappe.PermissionError):
				insert(manual.as_dict())
		self.assertEqual(self._cards(other.name), cards)
		self.assertEqual(self._cards(own.name), [])
		self.assertNotIn(_AUTO_JOB_CARD_WORK_ORDER_FLAG, frappe.flags)

	def test_native_failure_restores_outer_scope_and_request_rollback_removes_cards(self):
		wo = self._draft()
		original = native_work_order.create_job_card
		def fail_after_insert(*args, **kwargs):
			original(*args, **kwargs)
			raise frappe.ValidationError("Injected failure after automatic Job Card insert")
		frappe.db.savepoint("native_submit_failure")
		with self.set_user(self.supervisor), _allow_generated_job_cards_for("WO-OUTER"):
			with patch.object(native_work_order, "create_job_card", fail_after_insert):
				with self.assertRaisesRegex(frappe.ValidationError, "Injected failure"):
					submit(wo.as_dict())
			self.assertEqual(getattr(frappe.flags, _AUTO_JOB_CARD_WORK_ORDER_FLAG), "WO-OUTER")
			self.assertEqual(frappe.session.user, self.supervisor)
		# The ordinary HTTP request owns rollback; the submit path must never commit.
		frappe.db.rollback(save_point="native_submit_failure")
		self.assertEqual(frappe.db.get_value("Work Order", wo.name, "docstatus"), 0)
		self.assertEqual(self._cards(wo.name), [])
		self.assertNotIn(_AUTO_JOB_CARD_WORK_ORDER_FLAG, frappe.flags)

	def test_existing_guided_submit_keeps_exact_scope_and_real_session(self):
		orders = [self._draft(), self._draft()]
		with self.set_user(self.supervisor):
			_submit_work_orders([wo.name for wo in orders])
			self.assertEqual(frappe.session.user, self.supervisor)
			self.assertFalse(frappe.has_permission("Job Card", "create"))
			self.assertNotIn(_AUTO_JOB_CARD_WORK_ORDER_FLAG, frappe.flags)
		for wo in orders:
			self.assertEqual(frappe.db.get_value("Work Order", wo.name, "docstatus"), 1)
			cards = self._cards(wo.name)
			self.assertEqual(len(cards), 1)
			self.assertEqual((cards[0].operation_id, cards[0].owner), (wo.operations[0].name, self.supervisor))
