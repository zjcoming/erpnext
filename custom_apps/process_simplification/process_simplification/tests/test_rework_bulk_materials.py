"""Guard the normal-role path from an approved rework transfer to guided issue."""

from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from process_simplification.production_workflow import bulk_materials as bulk


class Record(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)


class TestReworkBulkMaterials(TestCase):
	def setUp(self):
		self.stack = ExitStack()
		self.addCleanup(self.stack.close)
		self.stack.enter_context(patch.object(frappe, "throw", side_effect=lambda msg, exc=frappe.ValidationError: (_ for _ in ()).throw(exc(msg))))
		self.permissions = self.stack.enter_context(patch.object(frappe, "has_permission", return_value=True))
		self.wo = Record(doctype="Work Order", name="WO", company="C", docstatus=1, status="Not Started",
			production_plan=None, bom_no="BOM", production_item="FG", qty=1, stock_uom="Nos", item_name="FG",
			sales_order=None, source_warehouse="Rework", wip_warehouse="WIP", skip_transfer=0,
			transfer_material_against="Work Order", track_semi_finished_goods=0,
			required_items=[Record(item_code="RM", source_warehouse="Rework", required_qty=1)])
		self.request = Record(doctype="Material Handling Request", name="MHR", company="C", action="Rework",
			status="Completed", rework_order="WO", rework_bom="BOM", stock_entry="TRANSFER",
			items=[Record(item_code="RM", qty=1, source_warehouse="Quarantine", target_warehouse="Rework",
				original_source_warehouse="Stores")])
		self.transfer = Record(doctype="Stock Entry", name="TRANSFER", company="C", docstatus=1,
			purpose="Material Transfer", is_return=0, custom_material_handling_request="MHR",
			items=[Record(item_code="RM", s_warehouse="Quarantine", t_warehouse="Rework", transfer_qty=1)])
		self.bom = Record(doctype="BOM", name="BOM", docstatus=1, is_active=1, company="C", item="FG", quantity=1,
			items=[Record(item_code="RM", stock_qty=1, is_stock_item=1, bom_no=None)])
		self.plan = Record(doctype="Production Plan", name="PLAN", company="C", docstatus=1, status="In Process")
		self.settings = frappe._dict(custom_material_rework_warehouse="Rework", default_scrap_warehouse="Scrap")
		self.warehouse = frappe._dict(company="C", disabled=0, is_group=0)
		self.documents = {("Work Order", "WO"): self.wo, ("Material Handling Request", "MHR"): self.request,
			("Stock Entry", "TRANSFER"): self.transfer, ("BOM", "BOM"): self.bom,
			("Production Plan", "PLAN"): self.plan}
		self.get_doc = self.stack.enter_context(patch.object(frappe, "get_doc", side_effect=lambda dt, name, **kwargs: self.documents[(dt, name)]))
		self.get_all = self.stack.enter_context(patch.object(frappe, "get_all", return_value=["MHR"]))
		def get_value(dt, name, field, **kwargs):
			if dt == "Company":
				return self.settings
			if dt == "Warehouse":
				return self.warehouse
			if dt == "Stock Entry":
				return name
			if dt == "Work Order":
				return self.documents[(dt, name)].get(field)
			raise AssertionError((dt, name, field))
		self.get_value = self.stack.enter_context(patch.object(frappe.db, "get_value", side_effect=get_value))

	def test_posted_single_material_rework_is_a_supported_independent_order(self):
		self.assertIs(bulk._work_order("WO", {"C"}, lock=True), self.wo)
		self.assertIn((("Material Handling Request", "MHR"), {"for_update": True}),
			[(call.args, call.kwargs) for call in self.get_doc.call_args_list])
		self.assertIn((("Stock Entry", "TRANSFER"), {"for_update": True}),
			[(call.args, call.kwargs) for call in self.get_doc.call_args_list])

	def test_transfer_lock_precedes_request_lock_and_incomplete_posting_takes_neither(self):
		calls = []
		get_doc, get_value = self.get_doc.side_effect, self.get_value.side_effect
		self.get_doc.side_effect = lambda dt, name, **kw: (calls.append((dt, bool(kw.get('for_update')))), get_doc(dt, name, **kw))[1]
		self.get_value.side_effect = lambda dt, name, field, **kw: (calls.append((dt, bool(kw.get('for_update')))), get_value(dt, name, field, **kw))[1]
		bulk._work_order('WO', {'C'}, lock=True)
		self.assertLess(calls.index(('Stock Entry', True)), calls.index(('Material Handling Request', True)))
		calls.clear()
		self.request.status = 'Awaiting Stock Entry'
		with self.assertRaises(frappe.ValidationError):
			bulk._work_order('WO', {'C'}, lock=True)
		self.assertNotIn(('Stock Entry', True), calls)
		self.assertNotIn(('Material Handling Request', True), calls)

	def test_existing_plan_branch_does_not_need_a_rework_link(self):
		self.wo.production_plan = "PLAN"
		self.assertIs(bulk._work_order("WO", {"C"}), self.wo)
		self.get_all.assert_not_called()
		self.plan.status = "Closed"
		with self.assertRaisesRegex(frappe.ValidationError, "生产计划"):
			bulk._work_order("WO", {"C"})

	def test_unbound_or_ambiguous_independent_orders_are_rejected(self):
		for names in ([], ["MHR", "MHR-OTHER"]):
			with self.subTest(names=names):
				self.get_all.return_value = names
				with self.assertRaisesRegex(frappe.ValidationError, "唯一"):
					bulk._work_order("WO", {"C"})

	def test_request_must_be_completed_and_bound_to_this_order_company_and_bom(self):
		for field, value in [('action','Scrap'), ('status','Pending Approval'), ('status','Withdrawn'),
			('rework_order','ANOTHER-WO'), ('company','Other'), ('rework_bom','OTHER-BOM'), ('stock_entry',None)]:
			with self.subTest(field=field, value=value):
				old = getattr(self.request, field)
				setattr(self.request, field, value)
				with self.assertRaises(frappe.ValidationError):
					bulk._work_order("WO", {"C", "Other"})
				setattr(self.request, field, old)

	def test_transfer_must_be_posted_bound_and_match_the_approved_route(self):
		for field, value in [('docstatus',0), ('docstatus',2), ('company','Other'),
			('purpose','Material Receipt'), ('is_return',1), ('custom_material_handling_request','OTHER-MHR')]:
			with self.subTest(field=field, value=value):
				old = getattr(self.transfer, field)
				setattr(self.transfer, field, value)
				with self.assertRaisesRegex(frappe.ValidationError, "转仓"):
					bulk._work_order("WO", {"C", "Other"})
				setattr(self.transfer, field, old)
		for field, value in [('item_code','OTHER'), ('s_warehouse','Stores'), ('t_warehouse','Stores'), ('transfer_qty',2)]:
			with self.subTest(field=field):
				row = self.transfer.items[0]
				old = getattr(row, field)
				setattr(row, field, value)
				with self.assertRaisesRegex(frappe.ValidationError, "转仓"):
					bulk._work_order("WO", {"C"})
				setattr(row, field, old)

	def test_split_native_transfer_rows_preserve_exact_approved_total(self):
		self.transfer.items[0].transfer_qty = .4
		self.transfer.items.append(Record(item_code="RM", s_warehouse="Quarantine", t_warehouse="Rework", transfer_qty=.6))
		self.assertIs(bulk._work_order("WO", {"C"}), self.wo)
		self.transfer.items[1].transfer_qty = .7
		with self.assertRaisesRegex(frappe.ValidationError, "转仓"):
			bulk._work_order("WO", {"C"})

	def test_approved_warehouse_must_remain_independent_and_enabled(self):
		self.settings.custom_material_rework_warehouse = "Other-Rework"
		with self.assertRaisesRegex(frappe.ValidationError, "独立返工仓"):
			bulk._work_order("WO", {"C"})
		self.settings.custom_material_rework_warehouse = "Rework"
		self.warehouse.disabled = 1
		with self.assertRaises(frappe.ValidationError):
			bulk._work_order("WO", {"C"})
		self.warehouse.disabled = 0
		self.wo.wip_warehouse = "Rework"
		with self.assertRaisesRegex(frappe.ValidationError, "独立返工仓"):
			bulk._work_order("WO", {"C"})

	def test_changing_work_order_output_or_material_quantity_cannot_expand_approval(self):
		for target, field, value in [(self.wo,'qty',2), (self.wo,'production_item','OTHER-FG'),
			(self.wo,'source_warehouse','Stores'), (self.wo.required_items[0],'required_qty',2)]:
			with self.subTest(field=field):
				old = getattr(target, field)
				setattr(target, field, value)
				with self.assertRaises(frappe.ValidationError):
					bulk._work_order("WO", {"C"})
				setattr(target, field, old)

	def test_shared_auxiliaries_and_nested_boms_need_an_explicit_supported_route(self):
		self.bom.items.append(Record(item_code="AUX", stock_qty=1, is_stock_item=1))
		with self.assertRaisesRegex(frappe.ValidationError, "单一返工料"):
			bulk._work_order("WO", {"C"})
		self.bom.items.pop()
		self.bom.items[0].bom_no = "SUB-BOM"
		with self.assertRaisesRegex(frappe.ValidationError, "单一返工料"):
			bulk._work_order("WO", {"C"})
		self.bom.items[0].bom_no = None
		self.wo.required_items[0].source_warehouse = "Stores"
		with self.assertRaisesRegex(frappe.ValidationError, "单一返工料"):
			bulk._work_order("WO", {"C"})

	def test_company_and_native_read_permissions_still_apply(self):
		with self.assertRaises(frappe.PermissionError):
			bulk._work_order("WO", {"Other"})
		self.permissions.side_effect = frappe.PermissionError("no read")
		with self.assertRaises(frappe.PermissionError):
			bulk._work_order("WO", {"C"})

	def test_list_includes_bound_rework_and_plan_but_hides_ordinary_independent_wo(self):
		planned = deepcopy(self.wo)
		planned.name, planned.production_plan = "PLANNED-WO", "PLAN"
		self.documents[("Work Order", planned.name)] = planned
		candidates = [frappe._dict(name="ORDINARY", company="C", production_plan=None),
			frappe._dict(name="WO", company="C", production_plan=None),
			frappe._dict(name=planned.name, company="C", production_plan="PLAN")]
		self.get_all.side_effect = lambda dt, **kw: ["C"] if dt == "Company" else ["WO"] if kw.get("pluck") == "rework_order" else ["MHR"]
		with patch.object(bulk, "_access", return_value=("request", {}, {"C"})), \
			patch.object(frappe, "get_list", return_value=candidates) as listing, \
			patch.object(bulk, "_priority_caps", return_value={}), \
			patch.object(bulk, "_request_row", side_effect=lambda d, *a, **kw: {"name":d.name,"remaining_qty":1}):
			result = bulk.get_material_workbench(company="C")
		self.assertEqual([r['name'] for r in result['rows']], ['WO','PLANNED-WO'])
		self.assertEqual(listing.call_args.kwargs['filters']['company'], ['in',['C']])
		self.assertFalse(any(call.args == ('Work Order','ORDINARY') for call in self.get_doc.call_args_list))

	def test_request_revalidates_approval_and_keeps_priority_override_disabled(self):
		with patch.object(bulk, "_access", return_value=("request", {}, {"C"})), \
			patch.object(bulk, "_current_stock_facts", return_value={}), \
			patch.object(bulk.service, "load_work_order_stock_facts", return_value={}), \
			patch.object(bulk, "_request_state", return_value="state"), \
			patch.object(bulk, "_existing_draft", return_value=None), \
			patch.object(bulk.service, "request_material_issue", return_value={"stock_entry":"ISSUE"}) as issue:
			bulk.request_material("WO", {"work_order":"state"}, 1)
			issue.assert_called_once_with("WO", qty=1, allow_partial=0, override_priority=0)
			self.request.status = "Withdrawn"
			with self.assertRaises(frappe.ValidationError):
				bulk.request_material("WO", {"work_order":"state"}, 1)
			self.assertEqual(issue.call_count, 1)

	def test_confirmation_preview_rechecks_independent_rework_source(self):
		entry = Record(work_order="WO")
		self.transfer.docstatus = 2
		with self.assertRaisesRegex(frappe.ValidationError, "转仓"):
			bulk._confirmation_row(entry, {"C"})

	def test_confirmation_keeps_native_stock_validation_and_blocks_invalidated_rework(self):
		entry = Record(name="ISSUE", docstatus=0, work_order="WO", submit=MagicMock())
		with patch.object(bulk, "_access", return_value=("confirm", {}, {"C"})), \
			patch.object(bulk, "_stock_entry", return_value=entry), \
			patch.object(bulk, "_draft_state", return_value="draft"), \
			patch.object(bulk, "_requires_native_confirmation", return_value=False), \
			patch.object(bulk, "_current_stock_facts", return_value={}), \
			patch.object(bulk.service, "load_work_order_stock_facts", return_value={}), \
			patch.object(bulk, "_request_state", return_value="state"), \
			patch.object(bulk.service, "validate_guided_stock_entry_before_submit") as guard, \
			patch.object(bulk, "_assert_physical_stock") as physical:
			bulk.submit_material("ISSUE", "draft")
			guard.assert_called_once_with(entry)
			physical.assert_called_once_with(entry, self.wo)
			entry.submit.assert_called_once_with()
			self.transfer.docstatus = 2
			with self.assertRaises(frappe.ValidationError):
				bulk.submit_material("ISSUE", "draft")
			self.assertEqual(entry.submit.call_count, 1)

	def test_preview_does_not_offer_the_same_unreserved_rework_stock_twice(self):
		second = deepcopy(self.wo)
		second.name = "WO2"
		with patch.object(bulk, "_access", return_value=("request", {}, {"C"})), \
			patch.object(bulk, "_work_order", side_effect=[self.wo, second]), \
			patch.object(bulk, "_priority_caps", return_value={}), \
			patch.object(bulk.service, "load_work_order_stock_facts", return_value={}), \
			patch.object(bulk, "_request_state", return_value="state"), \
			patch.object(bulk, "_existing_draft", return_value=None), \
			patch.object(bulk.service, "remaining_material_issue_qty", return_value=1), \
			patch.object(bulk.service, "_current_available_by_item", return_value={('RM','Rework'):1}), \
			patch.object(bulk.service, "issueable_finished_qty", side_effect=lambda doc, available, facts: available[('RM','Rework')]), \
			patch.object(bulk.service, "_active_work_order_item_reserved_qty", return_value=0), \
			patch.object(bulk.service, "_guided_component_issue_quantities", side_effect=lambda doc, qty, facts: {
				('RM','Rework'): Record(qty=qty, template=Record(item_name="RM", stock_uom="Gram"))} if qty else {}):
			result = bulk.preview_material_requests(['WO','WO2'])
		self.assertEqual([r['request_qty'] for r in result['rows']], [1,0])
		self.assertEqual([r['can_select'] for r in result['rows']], [True,False])
# Fixture creation is administrative; every production/exception/handling/issue
# transition below uses the same restricted role that performs it in demo.
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate
from process_simplification.management_access import (
	PRODUCTION_MANAGER_ROLE, WAREHOUSE_OPERATOR_ROLE, ensure_management_access,
)
from process_simplification.tests import test_native_work_order_submit as native_fixtures
from process_simplification.tests import test_worker_reporting as reporting_fixtures


class TestReworkBulkMaterialsIntegration(IntegrationTestCase):
	TEST_COMPANY = reporting_fixtures.TestWorkerReporting.TEST_COMPANY
	TEST_COMPANY_ABBR = reporting_fixtures.TestWorkerReporting.TEST_COMPANY_ABBR
	OTHER_COMPANY = reporting_fixtures.TestWorkerReporting.OTHER_COMPANY
	OTHER_COMPANY_ABBR = reporting_fixtures.TestWorkerReporting.OTHER_COMPANY_ABBR
	_ensure_company = classmethod(reporting_fixtures.TestWorkerReporting._ensure_company.__func__)
	_make_user = reporting_fixtures.TestWorkerReporting._make_user
	_email = reporting_fixtures.TestWorkerReporting._email
	_grant_company = reporting_fixtures.TestWorkerReporting._grant_company
	_make_worker = reporting_fixtures.TestWorkerReporting._make_worker
	_make_employee = reporting_fixtures.TestWorkerReporting._make_employee
	_draft = native_fixtures.TestNativeWorkOrderSubmit._draft

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
		self.enterContext(self.change_settings("Stock Settings", {"enable_stock_reservation": 1}))
		self.enterContext(patch.dict(frappe.conf, {"enable_operation_dispatch_pool": 1}))
		self.supervisor = self._make_user(PRODUCTION_MANAGER_ROLE)
		self.warehouse_user = self._make_user(WAREHOUSE_OPERATOR_ROLE)
		self.worker_user = self._make_worker()
		for user in (self.supervisor, self.warehouse_user, self.worker_user):
			self._grant_company(user, self.TEST_COMPANY)
		self.worker = frappe.db.get_value("Employee", {"user_id": self.worker_user}, "name")

	def _rollback(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _approved_rework_order(self):
		from frappe.client import insert, submit
		from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry as map_issue
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
		from process_simplification.production_reporting import service as reporting
		from process_simplification.production_exceptions import handling, service as exceptions

		original = self._draft()
		warehouses = {}
		for suffix in ('WIP', 'FG', 'QUARANTINE', 'REWORK', 'SCRAP'):
			warehouses[suffix] = frappe.get_doc(dict(doctype="Warehouse", company=self.TEST_COMPANY,
				warehouse_name='REWORK-ISSUE-' + frappe.generate_hash(length=8) + '-' + suffix)).insert().name
		original.wip_warehouse = warehouses['WIP']
		original.fg_warehouse = warehouses['FG']
		original.save()
		frappe.db.set_value("Company", self.TEST_COMPANY, {
			"custom_material_quarantine_warehouse": warehouses['QUARANTINE'],
			"custom_material_rework_warehouse": warehouses['REWORK'],
			"default_scrap_warehouse": warehouses['SCRAP']})
		item = original.required_items[0].item_code
		make_stock_entry(item_code=item, to_warehouse=original.source_warehouse,
			company=self.TEST_COMPANY, qty=2, basic_rate=1,
			posting_date=add_days(nowdate(), -1), posting_time="12:00:00")
		with self.set_user(self.supervisor):
			submit(original.as_dict())
		with self.set_user(self.warehouse_user):
			draft = insert(map_issue(original.name, "Material Transfer for Manufacture", qty=2))
			submit(draft)
		card = frappe.get_doc("Job Card", frappe.get_all("Job Card", filters={"work_order": original.name}, pluck="name")[0])
		frappe.get_doc(dict(doctype="Operation Wage Rate", company=self.TEST_COMPANY,
			operation=card.operation, enabled=1, enable_piecework=1, piecework_rate=3,
			enable_time=0, hourly_rate=0, valid_from=nowdate())).insert()
		with self.set_user(self.supervisor):
			assignment = reporting.assign_workers(card.name, [{"employee":self.worker,"assigned_qty":2}], supervisor=self.supervisor)[0]
		with self.set_user(self.worker_user):
			options = exceptions.get_exception_options(assignment.name)
			request = exceptions.submit_exception(assignment.name, "Material Return", 1, "Material Defect",
				"Formal rework issue regression", frappe.generate_hash(length=20),
				material_key=options['materials'][0].key, material_action="Continue")
		with self.set_user(self.supervisor):
			exceptions.approve_exception(request.name)
		request.reload()
		with self.set_user(self.warehouse_user):
			submit(frappe.get_doc("Stock Entry", request.stock_entry).as_dict())
			options = handling.disposition_options(request.stock_entry)
			disposition = handling.create_request("Rework", [{"material_key":options['materials'][0].key,"qty":1}],
				frappe.generate_hash(length=20), work_order=original.name, source_stock_entry=request.stock_entry,
				rework_bom=original.bom_no, reason="Formal single-material rework")
			self.assertEqual(disposition.status, "Pending Approval")
		with self.set_user(self.supervisor):
			handling.approve(disposition.name)
		disposition.reload()
		with self.set_user(self.warehouse_user):
			handling.post(disposition.name)
		disposition.reload()
		self.assertEqual(disposition.status, "Completed")
		wo = frappe.get_doc("Work Order", disposition.rework_order)
		self.assertIsNone(wo.production_plan)
		with self.set_user(self.supervisor):
			submit(wo.as_dict())
		wo.reload()
		self.assertEqual(wo.docstatus, 1)
		self.assertEqual(wo.qty, 1)
		return wo, disposition, item, warehouses

	def test_real_roles_post_rework_then_preview_reserve_and_confirm_its_material(self):
		from erpnext.stock.utils import get_stock_balance
		wo, disposition, item, warehouses = self._approved_rework_order()
		with self.set_user(self.supervisor):
			page = bulk.get_material_workbench(company=self.TEST_COMPANY, search=wo.name)
			self.assertEqual([row['name'] for row in page['rows']], [wo.name])
			preview = bulk.preview_material_requests([wo.name])['rows'][0]
			self.assertTrue(preview['can_select'])
			self.assertEqual(preview['request_qty'], 1)
			self.assertEqual(preview['items'][0]['source_warehouse'], warehouses['REWORK'])
			created = bulk.request_material(wo.name, preview['expected_state'], 1)
		issue = frappe.get_doc("Stock Entry", created['stock_entry'])
		self.assertEqual(issue.docstatus, 0)
		self.assertEqual(issue.owner, self.supervisor)
		self.assertEqual(issue.items[0].s_warehouse, warehouses['REWORK'])
		self.assertEqual(issue.items[0].transfer_qty, 1)
		self.assertEqual(get_stock_balance(item, warehouses['REWORK']), 1)
		reservations = frappe.get_all("Stock Reservation Entry", filters={"voucher_type":"Work Order",
			"voucher_no":wo.name,"docstatus":1,"warehouse":warehouses['REWORK'],"item_code":item},
			fields=['reserved_qty','transferred_qty','consumed_qty','delivered_qty'])
		self.assertEqual(sum(r.reserved_qty-r.transferred_qty-r.consumed_qty-r.delivered_qty for r in reservations), 1)
		with self.set_user(self.warehouse_user):
			preview = bulk.preview_material_submissions([issue.name])['rows'][0]
			self.assertTrue(preview['can_select'])
			bulk.submit_material(issue.name, preview['expected_state'])
		issue.reload()
		self.assertEqual(issue.docstatus, 1)
		self.assertEqual(issue.modified_by, self.warehouse_user)
		self.assertEqual(get_stock_balance(item, warehouses['REWORK']), 0)
		self.assertEqual(get_stock_balance(item, warehouses['WIP']), 2)
		with self.set_user(self.supervisor):
			after = bulk.preview_material_requests([wo.name])['rows'][0]
			self.assertEqual(after['remaining_qty'], 0)
			self.assertFalse(after['can_select'])
		# Real native reversal restores net issue availability; no status/qty is forged.
		issue.cancel()
		with self.set_user(self.supervisor):
			after = bulk.preview_material_requests([wo.name])['rows'][0]
			self.assertEqual(after['remaining_qty'], 1)
			self.assertEqual(after['request_qty'], 1)
			self.assertTrue(after['can_select'])
		self.assertEqual(get_stock_balance(item, warehouses['REWORK']), 1)
		self.assertEqual(frappe.db.get_value("Material Handling Request", disposition.name, "status"), "Completed")