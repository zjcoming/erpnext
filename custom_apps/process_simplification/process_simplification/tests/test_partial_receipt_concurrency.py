"""Real receipt/review lock ordering across independent database connections.

Only the race fixture is committed. Its uniquely owned documents are removed in
cleanup after native stock cancellation; shared companies and masters survive.
"""

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextvars import Context
from threading import Event
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from erpnext.manufacturing.doctype.work_order.work_order import make_job_card, make_stock_entry
from process_simplification.management_access import WAREHOUSE_OPERATOR_ROLE, ensure_management_access
from process_simplification.production_reporting import service as reporting
from process_simplification.production_workflow import service as workflow
from process_simplification.tests import test_partial_receipt_integration as receipt_fixtures
from process_simplification.tests import test_worker_reporting as reporting_fixtures


class TestPartialReceiptConcurrency(IntegrationTestCase):
	# Borrow helpers, not a TestCase base or imported TestCase name: only the three
	# tests declared below may be collected from this module.
	TEST_COMPANY = receipt_fixtures.TestPartialReceiptIntegration.TEST_COMPANY
	TEST_COMPANY_ABBR = receipt_fixtures.TestPartialReceiptIntegration.TEST_COMPANY_ABBR
	OTHER_COMPANY = receipt_fixtures.TestPartialReceiptIntegration.OTHER_COMPANY
	OTHER_COMPANY_ABBR = receipt_fixtures.TestPartialReceiptIntegration.OTHER_COMPANY_ABBR
	_ensure_company = classmethod(reporting_fixtures.TestWorkerReporting._ensure_company.__func__)
	_draft = receipt_fixtures.TestPartialReceiptIntegration._draft
	_email = receipt_fixtures.TestPartialReceiptIntegration._email
	_make_user = receipt_fixtures.TestPartialReceiptIntegration._make_user
	_make_employee = receipt_fixtures.TestPartialReceiptIntegration._make_employee
	_make_worker = receipt_fixtures.TestPartialReceiptIntegration._make_worker
	_make_supervisor = receipt_fixtures.TestPartialReceiptIntegration._make_supervisor
	_grant_company = receipt_fixtures.TestPartialReceiptIntegration._grant_company
	_make_rate = receipt_fixtures.TestPartialReceiptIntegration._make_rate
	_assign = receipt_fixtures.TestPartialReceiptIntegration._assign
	_submit_as = receipt_fixtures.TestPartialReceiptIntegration._submit_as
	_submit = receipt_fixtures.TestPartialReceiptIntegration._submit
	_approve = receipt_fixtures.TestPartialReceiptIntegration._approve
	_flow = receipt_fixtures.TestPartialReceiptIntegration._flow
	_balance = receipt_fixtures.TestPartialReceiptIntegration._balance

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
		self._committed_fixture = None
		self._original_settings = {
			field: frappe.db.get_single_value("Manufacturing Settings", field)
			for field in ("disable_capacity_planning", "backflush_raw_materials_based_on")
		}
		self.addCleanup(self._cleanup_fixture)
		self.enterContext(self.change_settings("Manufacturing Settings", {
			"disable_capacity_planning": 1,
			"backflush_raw_materials_based_on": "Material Transferred for Manufacture",
		}))
		self.supervisor = self._make_supervisor()
		self.worker_user = self._make_worker()
		self.worker = frappe.db.get_value("Employee", {"user_id": self.worker_user}, "name")
		self.wage_manager = self._make_user("Production Wage Manager")
		self._grant_company(self.wage_manager, self.TEST_COMPANY)
		self.warehouse_user = self._make_user(WAREHOUSE_OPERATOR_ROLE)
		self._grant_company(self.warehouse_user, self.TEST_COMPANY)

	def _commit_fixture(self, wo):
		self.assertTrue(frappe.flags.in_test, "Committed fixture cleanup is test-runner only")
		items = [wo.production_item, *[row.item_code for row in wo.required_items]]
		self.assertTrue(all(item.startswith("NATIVE-WO-") for item in items))
		stock_entries = frappe.get_all(
			"Stock Entry Detail", filters={"item_code": ["in", items]}, pluck="parent"
		)
		self._committed_fixture = {
			"work_order": wo.name,
			"items": sorted(set(items)),
			"warehouses": sorted({wo.source_warehouse, wo.wip_warehouse, wo.fg_warehouse}),
			"bom": wo.bom_no,
			"operation": wo.operations[0].operation,
			"workstation": wo.operations[0].workstation,
			"stock_entries": sorted(set(stock_entries)),
			"users": [self.supervisor, self.worker_user, self.wage_manager, self.warehouse_user],
			"original_settings": self._original_settings,
		}
		frappe.db.commit()
		print("PARTIAL_CONCURRENCY_FIXTURE " + json.dumps(self._committed_fixture, sort_keys=True))

	def _delete_rows_and_children(self, doctype, names):
		names = list(dict.fromkeys(name for name in names if name))
		if not names:
			return
		for field in frappe.get_meta(doctype).get_table_fields():
			frappe.db.delete(field.options, {"parent": ["in", names], "parenttype": doctype})
		frappe.db.delete(doctype, {"name": ["in", names]})

	def _cleanup_fixture(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		fixture = self._committed_fixture
		if not fixture:
			return
		self.assertTrue(frappe.flags.in_test)
		wo_name = fixture["work_order"]
		items = fixture["items"]
		self.assertTrue(all(item.startswith("NATIVE-WO-") for item in items))
		try:
			cards = frappe.get_all("Job Card", filters={"work_order": wo_name}, pluck="name")
			assignments = frappe.get_all(
				"Job Card Worker Assignment", filters={"work_order": wo_name}, pluck="name"
			)
			reports = frappe.get_all(
				"Job Card Work Report", filters={"job_card": ["in", cards]}, pluck="name"
			)
			# Test-only cleanup of data made durable solely for the race. This is not
			# a business reversal API; it follows the existing reporting test harness.
			self._delete_rows_and_children("Job Card Work Report", reports)
			movements = frappe.get_all(
				"Job Card Assignment Movement", filters={"job_card": ["in", cards]}, pluck="name"
			)
			self._delete_rows_and_children("Job Card Assignment Movement", movements)
			self._delete_rows_and_children("Job Card Worker Assignment", assignments)
			frappe.db.set_value("Work Order", wo_name, "custom_worker_reporting_enabled", 0)
			for name in cards:
				frappe.db.set_value("Job Card", name, {
					"custom_worker_reporting_enabled": 0,
					"custom_worker_reporting_supervisor": None,
				})
			entries = frappe.get_all(
				"Stock Entry", filters={"name": ["in", fixture["stock_entries"]]},
				fields=["name", "docstatus"], order_by="creation desc, name desc",
			)
			# Cancel transfer before original receipt to restore real stock and GL.
			for entry in entries:
				if entry.docstatus == 1:
					frappe.get_doc("Stock Entry", entry.name).cancel()
			for name in cards:
				if frappe.db.get_value("Job Card", name, "docstatus") == 1:
					frappe.get_doc("Job Card", name).cancel()
			wo = frappe.get_doc("Work Order", wo_name)
			if wo.docstatus == 1:
				wo.cancel()
			self.assertTrue(all(
				abs(flt(row.actual_qty)) < 1e-9
				for row in frappe.get_all("Bin", filters={"item_code": ["in", items]}, fields=["actual_qty"])
			))
			# Immutable reversal rows belong exclusively to these unique test items.
			# Remove them after cancellation, not instead of exercising native reversal.
			frappe.db.delete("Stock Ledger Entry", {"item_code": ["in", items]})
			frappe.db.delete("GL Entry", {
				"voucher_type": "Stock Entry", "voucher_no": ["in", fixture["stock_entries"]],
			})
			self._delete_rows_and_children("Stock Entry", fixture["stock_entries"])
			self._delete_rows_and_children("Job Card", cards)
			self._delete_rows_and_children("Work Order", [wo_name])
			rates = frappe.get_all(
				"Operation Wage Rate", filters={"operation": fixture["operation"]}, pluck="name"
			)
			self._delete_rows_and_children("Operation Wage Rate", rates)
			self._delete_rows_and_children("BOM", [fixture["bom"]])
			reposts = set(frappe.get_all("Repost Item Valuation", filters={
				"item_code": ["in", items], "warehouse": ["in", fixture["warehouses"]],
			}, pluck="name"))
			reposts.update(frappe.get_all("Repost Item Valuation", filters={
				"voucher_type": "Stock Entry", "voucher_no": ["in", fixture["stock_entries"]],
			}, pluck="name"))
			self._delete_rows_and_children("Repost Item Valuation", reposts)
			frappe.db.delete("Bin", {"item_code": ["in", items]})
			self._delete_rows_and_children("Item", items)
			self._delete_rows_and_children("Operation", [fixture["operation"]])
			self._delete_rows_and_children("Workstation", [fixture["workstation"]])
			# Warehouse is a nested set: use native delete to preserve shared tree bounds.
			for name in fixture["warehouses"]:
				frappe.delete_doc("Warehouse", name, ignore_permissions=True, delete_permanently=True)
			users = fixture["users"]
			employees = frappe.get_all("Employee", filters={"user_id": ["in", users]}, pluck="name")
			contacts = frappe.get_all("Contact", filters={"user": ["in", users]}, pluck="name")
			frappe.db.delete("User Permission", {"user": ["in", users]})
			for doctype, names in (("Employee", employees), ("User", users), ("Contact", contacts)):
				for name in names:
					if frappe.db.exists(doctype, name):
						frappe.delete_doc(doctype, name, ignore_permissions=True, force=True, delete_permanently=True)
			owned_names = [wo_name, *cards, *assignments, *reports, *fixture["stock_entries"]]
			for doctype, field in (("Version", "docname"), ("Comment", "reference_name"),
				("ToDo", "reference_name"), ("Notification Log", "document_name")):
				frappe.db.delete(doctype, {field: ["in", owned_names]})
			self.assertFalse(frappe.db.exists("Work Order", wo_name))
			self.assertFalse(frappe.db.exists("Stock Ledger Entry", {"item_code": ["in", items]}))
			self.assertFalse(frappe.db.exists("Bin", {"item_code": ["in", items]}))
			self.assertFalse(any(frappe.db.exists("User", name) for name in users))
			frappe.db.commit()
			print("PARTIAL_CONCURRENCY_CLEANUP " + wo_name)
		except Exception:
			frappe.db.rollback()
			raise
		finally:
			# The fixture commit also made this test's temporary settings durable.
			for field, value in self._original_settings.items():
				frappe.db.set_single_value("Manufacturing Settings", field, value)
			frappe.db.commit()
			frappe.clear_document_cache("Manufacturing Settings", "Manufacturing Settings")

	def test_real_review_and_receipt_use_job_card_before_work_order_lock(self):
		wo, cards, assignments = self._flow(split=True)
		self._approve(self._submit(assignments[0], 2))
		with self.set_user(self.supervisor):
			request = workflow.request_manufacture(wo.name, qty=2)
		pending = self._submit(assignments[1], 2)
		self._commit_fixture(wo)
		site = frappe.local.site
		reviewer_holds_card, allow_review_parent, receipt_attempts_card = Event(), Event(), Event()
		traces = {"review": [], "receipt": []}

		def actor(kind):
			frappe.init(site=site)
			frappe.connect()
			frappe.flags.in_test = True
			frappe.set_user(self.supervisor if kind == "review" else self.warehouse_user)
			database = frappe.local.db
			connection = database.sql("select connection_id()")[0][0]
			original_sql = database.sql
			paused = False
			card_locked = False

			def observe_sql(query, *args, **kwargs):
				nonlocal paused, card_locked
				sql = " ".join(str(query).lower().split())
				card_table = "`tabjob card`" in sql or '"tabjob card"' in sql
				parent_table = "`tabwork order`" in sql or '"tabwork order"' in sql
				locks_card = card_table and "for update" in sql
				locks_parent = parent_table and "for update" in sql
				writes_parent = parent_table and sql.startswith("update ")
				if locks_card or locks_parent or writes_parent:
					traces[kind].append("JC" if locks_card else "WO")
				if kind == "review" and locks_parent and not paused:
					if not card_locked:
						raise AssertionError("Review attempted WO lock without its prior JC lock")
					paused = True
					reviewer_holds_card.set()
					if not allow_review_parent.wait(timeout=10):
						raise AssertionError("Controller did not release review's WO lock attempt")
				if kind == "receipt" and locks_card and not receipt_attempts_card.is_set():
					if "WO" in traces[kind][:-1]:
						raise AssertionError("Receipt locked/wrote WO before its first JC lock")
					receipt_attempts_card.set()
				result = original_sql(query, *args, **kwargs)
				if locks_card:
					card_locked = True
				return result

			try:
				database.sql("SET SESSION innodb_lock_wait_timeout = 5")
				with patch.object(database, "sql", side_effect=observe_sql):
					if kind == "review":
						report = reporting.approve_work_report(pending.name)
						result = {"status": report.status, "qty": flt(report.completed_qty),
							"wage": flt(report.wage_amount)}
					else:
						entry = frappe.get_doc("Stock Entry", request["stock_entry"]).submit()
						current = frappe.get_doc("Work Order", wo.name)
						result = {"docstatus": entry.docstatus, "produced": flt(current.produced_qty),
							"status": current.status,
							"draft_card": frappe.db.get_value("Job Card", cards[1].name, "docstatus"),
							"finished": self._balance(wo.production_item, wo.fg_warehouse),
							"wip": self._balance(wo.required_items[0].item_code, wo.wip_warehouse)}
				return {"connection": connection, **result}
			finally:
				# Exceptions, including deadlocks/timeouts, propagate to future.result.
				# No competing transaction is committed or counted as a safe conflict.
				frappe.db.rollback()
				frappe.destroy()

		with ThreadPoolExecutor(max_workers=2) as executor:
			review = executor.submit(Context().run, actor, "review")
			try:
				self.assertTrue(reviewer_holds_card.wait(timeout=10))
				receipt = executor.submit(Context().run, actor, "receipt")
				self.assertTrue(receipt_attempts_card.wait(timeout=10))
				with self.assertRaises(FutureTimeoutError):
					receipt.result(timeout=0.25)
				allow_review_parent.set()
				review_result = review.result(timeout=15)
				receipt_result = receipt.result(timeout=15)
			finally:
				allow_review_parent.set()
		self.assertNotEqual(review_result["connection"], receipt_result["connection"])
		self.assertEqual((review_result["status"], review_result["qty"], review_result["wage"]),
			("Approved", 2, 6))
		self.assertEqual((receipt_result["docstatus"], receipt_result["produced"],
			receipt_result["draft_card"], receipt_result["finished"], receipt_result["wip"]), (1, 2, 0, 2, 2))
		self.assertNotEqual(receipt_result["status"], "Completed")
		self.assertEqual(traces["review"][0], "JC")
		self.assertEqual(traces["receipt"][0], "JC")
		print("PARTIAL_CONCURRENCY_RESULT " + json.dumps({
			"review": review_result, "receipt": receipt_result, "trace": traces,
		}, sort_keys=True))
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value("Stock Entry", request["stock_entry"], "docstatus"), 0)
		self.assertEqual(frappe.db.get_value("Job Card Work Report", pending.name, "status"), "Pending Approval")
		self.assertEqual(wo.reload().produced_qty, 0)
		self.assertEqual(self._balance(wo.production_item, wo.fg_warehouse), 0)
		self.assertEqual(self._balance(wo.required_items[0].item_code, wo.wip_warehouse), 4)
		approved = frappe.get_all("Job Card Work Report", filters={
			"job_card": ["in", [card.name for card in cards]], "status": "Approved",
		}, fields=["completed_qty", "wage_amount"])
		self.assertEqual((len(approved), sum(row.completed_qty for row in approved),
			sum(row.wage_amount for row in approved)), (1, 2, 6))
		self.assertFalse(frappe.db.exists("Stock Ledger Entry", {"voucher_no": request["stock_entry"]}))

	def test_final_native_receipt_rejects_an_extra_managed_draft(self):
		wo, cards, assignments = self._flow(split=True)
		self._approve(self._submit(assignments[0], 2))
		make_job_card(wo.name, [{"name": wo.operations[0].name,
			"operation": wo.operations[0].operation, "qty": 1, "pending_qty": 1}], wo.bom_no)
		extra_names = set(frappe.get_all("Job Card", filters={"work_order": wo.name}, pluck="name"))
		extra_names.difference_update(card.name for card in cards)
		self.assertEqual(len(extra_names), 1)
		extra = frappe.get_doc("Job Card", extra_names.pop())
		extra_assignment = self._assign(extra)
		self._approve(self._submit(assignments[1], 2))
		self.assertEqual(wo.reload().operations[0].completed_qty, 4)
		self.assertEqual(extra.reload().docstatus, 0)
		with self.set_user(self.warehouse_user):
			entry = frappe.get_doc(make_stock_entry(wo.name, "Manufacture", qty=4)).insert()
			self.assertFalse(entry.custom_process_workflow_action)
			frappe.db.savepoint("final_managed_draft")
			with self.assertRaisesRegex(frappe.ValidationError, "Job Cards are still Draft"):
				entry.submit()
			frappe.db.rollback(save_point="final_managed_draft")
		self.assertEqual(entry.reload().docstatus, 0)
		self.assertEqual(wo.reload().produced_qty, 0)
		self.assertEqual(self._balance(wo.production_item, wo.fg_warehouse), 0)
		self.assertEqual(self._balance(wo.required_items[0].item_code, wo.wip_warehouse), 4)
		self.assertFalse(frappe.db.exists("Stock Ledger Entry", {"voucher_no": entry.name}))
		self.assertEqual(extra.reload().docstatus, 0)
		self.assertEqual(frappe.db.get_value("Job Card Worker Assignment", extra_assignment.name, "status"), "Active")

	def test_partial_receipt_keeps_the_next_managed_card_executable(self):
		# Reuse the already isolated real-role scenario, without collecting its suite.
		receipt_fixtures.TestPartialReceiptIntegration.test_two_managed_batches_post_first_while_second_remains_executable(self)
