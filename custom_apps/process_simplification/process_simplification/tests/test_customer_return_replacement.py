"""Real customer-return replacement chains on the isolated integration-test site.

No fixture commits: settings, masters, reservations and stock postings all roll back.
Run this module only on the disposable/local test site, never on demo or production.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from erpnext.manufacturing.doctype.work_order.work_order import OverProductionError, WorkOrder, stop_unstop
from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return
from erpnext.stock.doctype.material_request.material_request import MaterialRequest
from process_simplification.api import actions
from process_simplification.api.production_readiness import get_production_plan_readiness
from process_simplification.api.shortage import calculate_plan_purchase_shortages
from process_simplification.management_access import PRODUCTION_MANAGER_ROLE
from process_simplification.production_workflow import bulk_materials, customer_returns, service
from process_simplification.production_workflow.work_order_close import close_work_order
from process_simplification.tests import test_worker_reporting as reporting_fixtures


class TestCustomerReturnReplacement(IntegrationTestCase):
	TEST_COMPANY = reporting_fixtures.TestWorkerReporting.TEST_COMPANY
	TEST_COMPANY_ABBR = reporting_fixtures.TestWorkerReporting.TEST_COMPANY_ABBR
	_ensure_company = classmethod(reporting_fixtures.TestWorkerReporting._ensure_company.__func__)
	_make_user = reporting_fixtures.TestWorkerReporting._make_user
	_email = reporting_fixtures.TestWorkerReporting._email
	_grant_company = reporting_fixtures.TestWorkerReporting._grant_company

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.addCleanup(self._rollback_fixture)
		self._ensure_company(self.TEST_COMPANY, self.TEST_COMPANY_ABBR)
		self.company = self.TEST_COMPANY
		self.prefix = "RETURN-REPLACEMENT-" + frappe.generate_hash(length=8)
		self.raw, self.wip, self.fg, self.quarantine = [
			frappe.get_doc(dict(doctype="Warehouse", warehouse_name=self.prefix + "-" + label,
				company=self.company)).insert().name
			for label in ("RAW", "WIP", "FG", "QUARANTINE")
		]
		self.enterContext(self.change_settings("Stock Settings", {
			"enable_stock_reservation": 1, "default_warehouse": self.raw,
		}))
		self.enterContext(self.change_settings("Manufacturing Settings", {
			"overproduction_percentage_for_sales_order": 0,
		}))
		frappe.db.set_value("Company", self.company, {
			"default_wip_warehouse": self.wip, "default_fg_warehouse": self.fg,
			"custom_default_semi_finished_warehouse": None,
			"custom_material_quarantine_warehouse": self.quarantine,
		})
		frappe.clear_document_cache("Company", self.company)
		self.material, self.finished = [
			frappe.get_doc(dict(doctype="Item", item_code=self.prefix + "-" + label,
				item_name=self.prefix + "-" + label, item_group="All Item Groups",
				stock_uom="Nos", is_stock_item=1, is_purchase_item=1, is_sales_item=1,
				include_item_in_manufacturing=1, valuation_rate=1)).insert().name
			for label in ("RM", "FG")
		]
		self.bom = frappe.get_doc(dict(doctype="BOM", item=self.finished, company=self.company,
			currency=frappe.get_cached_value("Company", self.company, "default_currency"),
			quantity=1, is_active=1, is_default=1, default_source_warehouse=self.raw,
			items=[dict(item_code=self.material, qty=1, rate=1, source_warehouse=self.raw)]))
		self.bom.insert().submit()
		customer_group = frappe.get_doc(dict(doctype="Customer Group",
			customer_group_name=self.prefix + "-GROUP", parent_customer_group="All Customer Groups", is_group=0)).insert().name
		territory = frappe.get_doc(dict(doctype="Territory", territory_name=self.prefix + "-TERRITORY",
			parent_territory="All Territories", is_group=0)).insert().name
		self.customer = frappe.get_doc(dict(doctype="Customer", customer_name=self.prefix + "-CUSTOMER",
			customer_type="Company", customer_group=customer_group, territory=territory)).insert().name

	def _rollback_fixture(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.clear_document_cache("Company", self.TEST_COMPANY)
		frappe.clear_document_cache("Stock Settings", "Stock Settings")
		frappe.clear_document_cache("Manufacturing Settings", "Manufacturing Settings")

	def _order(self, qty=1):
		doc = frappe.get_doc(dict(doctype="Sales Order", company=self.company,
			customer=self.customer, currency=frappe.get_cached_value("Company", self.company, "default_currency"),
			transaction_date=nowdate(), delivery_date=add_days(nowdate(), 7),
			items=[dict(item_code=self.finished, qty=qty, rate=8, warehouse=self.fg,
				bom_no=self.bom.name, delivery_date=add_days(nowdate(), 7))]))
		doc.insert().submit()
		return doc

	def _stock_receipt(self, item, warehouse, qty):
		doc = frappe.get_doc(dict(doctype="Stock Entry", company=self.company,
			stock_entry_type="Material Receipt", items=[dict(item_code=item, qty=qty,
				t_warehouse=warehouse, basic_rate=1)]))
		doc.insert().submit()
		return doc

	def _plan(self, order, qty=1, return_item=None):
		kwargs = dict(sales_order=order.name, sales_order_item=order.items[0].name, qty=qty)
		if return_item:
			kwargs["return_delivery_note_item"] = return_item
		result = actions.create_work_order(**kwargs)
		self.assertEqual(len(result["work_orders"]), 1)
		return frappe.get_doc("Work Order", result["work_orders"][0])

	def _manufacture(self, work_order, receive_raw=True):
		if receive_raw:
			self._stock_receipt(self.material, self.raw, work_order.qty)
		issue = service.request_material_issue(work_order.name)
		frappe.get_doc("Stock Entry", issue["stock_entry"]).submit()
		receipt = service.request_manufacture(work_order.name)
		frappe.get_doc("Stock Entry", receipt["stock_entry"]).submit()
		work_order.reload()
		self.assertEqual(work_order.produced_qty, work_order.qty)
		self.assertEqual(work_order.status, "Completed")

	def _deliver(self, order, qty):
		actions.reserve_completed_stock(order.name, order.items[0].name, qty)
		result = actions.create_delivery_note(order.name, order.items[0].name)
		doc = frappe.get_doc("Delivery Note", result["delivery_note"])
		doc.submit()
		self.assertEqual(sum(flt(row.stock_qty) for row in doc.items), qty)
		return doc

	def _return(self, delivery, qty=1, submit=True):
		doc = make_sales_return(delivery.name)
		self.assertEqual(len(doc.items), 1)
		doc.items[0].qty = -qty
		doc.items[0].warehouse = self.quarantine
		doc.set_warehouse = self.quarantine
		doc.insert()
		# The mapper sets is_return after constructing its target. Native DN
		# builds return status updaters in __init__; the UI's separate submit
		# request loads a fresh instance, and the fixture must do the same.
		doc = frappe.get_doc("Delivery Note", doc.name)
		if submit:
			doc.submit()
			row = doc.items[0]
			returned_to_order = frappe.db.sql("""SELECT COALESCE(SUM(-i.qty),0)
				FROM `tabDelivery Note Item` i JOIN `tabDelivery Note` d ON d.name=i.parent
				WHERE d.docstatus=1 AND d.is_return=1 AND i.so_detail=%s""", (row.so_detail,))[0][0]
			returned_to_delivery = frappe.db.sql("""SELECT COALESCE(SUM(-i.stock_qty),0)
				FROM `tabDelivery Note Item` i JOIN `tabDelivery Note` d ON d.name=i.parent
				WHERE d.docstatus=1 AND d.is_return=1 AND i.dn_detail=%s""", (row.dn_detail,))[0][0]
			self.assertEqual(flt(frappe.db.get_value("Sales Order Item", row.so_detail, "returned_qty")),
				flt(returned_to_order), "Return fixture must update the native cumulative SO quantity")
			self.assertEqual(flt(frappe.db.get_value("Delivery Note Item", row.dn_detail, "returned_qty")),
				flt(returned_to_delivery), "Return fixture must update the original delivery row")
		return doc

	def _delivered(self, qty=1):
		order = self._order(qty)
		original = self._plan(order, qty)
		self._manufacture(original)
		return order, original, self._deliver(order, qty)

	def _returned(self, qty=1):
		order, original, delivery = self._delivered(qty)
		return order, original, self._return(delivery, qty)

	def _options(self, order):
		return customer_returns.get_replacement_options(order.name, order.items[0].name)["options"]

	def _remaining(self, order, return_item):
		return sum(flt(row["remaining_qty"]) for row in self._options(order)
			if row["return_item"] == return_item)

	def _documents(self):
		return {doctype: sorted(frappe.get_all(doctype, filters={"company": self.company}, pluck="name"))
			for doctype in ("Material Request", "Production Plan", "Work Order", "Stock Reservation Entry")}

	def _reject_plan(self, order, qty=1, return_item=None):
		before = self._documents()
		with self.assertRaises(frappe.ValidationError):
			self._plan(order, qty, return_item)
		self.assertEqual(self._documents(), before, "Rejected replacement must not leave a partial native chain")

	def test_ordinary_order_still_uses_sales_order_production_plan(self):
		order = self._order()
		self.assertEqual(self._options(order), [])
		work_order = self._plan(order)
		self.assertEqual((work_order.sales_order, work_order.sales_order_item),
			(order.name, order.items[0].name))
		self.assertFalse(work_order.material_request)
		self.assertTrue(work_order.production_plan)
		self.assertEqual(work_order.docstatus, 1)

	def test_positive_draft_cancelled_and_foreign_return_rows_are_rejected(self):
		order = self._order(2)
		original = self._plan(order)
		self._manufacture(original)
		delivery = self._deliver(order, 1)
		other = self._order()
		# A real uncovered quantity remains on this SO, so these failures cannot
		# be explained by the ordinary no-net-demand guard alone.
		self._reject_plan(order, return_item=delivery.items[0].name)
		draft = self._return(delivery, submit=False)
		self._reject_plan(order, return_item=draft.items[0].name)
		draft.submit()
		self._reject_plan(other, return_item=draft.items[0].name)
		draft.cancel()
		self._reject_plan(order, return_item=draft.items[0].name)
		self.assertEqual(self._options(order), [])
		self.assertEqual(original.reload().status, "Completed")

	def test_submitted_return_creates_native_mr_plan_work_order_with_all_sources(self):
		order, original, returned = self._returned()
		with self.assertRaises(OverProductionError):
			actions.create_work_order(order.name, order.items[0].name, 1)
		user = self._make_user(PRODUCTION_MANAGER_ROLE)
		self._grant_company(user, self.company)
		captured = []
		create_request = customer_returns.create_replacement_request

		def capture_created_request(*args, **kwargs):
			request = create_request(*args, **kwargs)
			captured.append(request)
			return request

		with self.set_user(user):
			self.assertTrue(frappe.has_permission("Material Request", "read"))
			self.assertFalse(frappe.has_permission("Material Request", "create"))
			self.assertFalse(frappe.has_permission("Material Request", "submit"))
			self.assertEqual(self._remaining(order, returned.items[0].name), 1)
			with patch.object(customer_returns, "create_replacement_request", side_effect=capture_created_request):
				replacement = self._plan(order, return_item=returned.items[0].name)
			generic = frappe.get_doc(dict(doctype="Material Request", company=self.company,
				material_request_type="Manufacture", schedule_date=add_days(nowdate(), 7),
				items=[dict(item_code=self.finished, qty=1, warehouse=self.fg,
					schedule_date=add_days(nowdate(), 7))]))
			with self.assertRaises(frappe.PermissionError):
				generic.insert()
			self.assertFalse(frappe.has_permission("Material Request", "create"))
			self.assertFalse(frappe.has_permission("Material Request", "submit"))
		self.assertEqual(len(captured), 1)
		self.assertFalse(captured[0].flags.get("ignore_permissions"))
		self.assertNotIn(customer_returns._REQUEST_FLAG, frappe.flags)
		self.assertNotIn(customer_returns._PLAN_FLAG, frappe.flags)
		request = frappe.get_doc("Material Request", replacement.material_request)
		plan = frappe.get_doc("Production Plan", replacement.production_plan)
		self.assertEqual((request.docstatus, request.material_request_type), (1, "Manufacture"))
		self.assertEqual(request.get(customer_returns.RETURN_FIELD), returned.name)
		self.assertEqual(request.get(customer_returns.RETURN_ITEM_FIELD), returned.items[0].name)
		self.assertEqual(len(request.items), 1)
		self.assertEqual(request.items[0].stock_qty, 1)
		self.assertEqual((replacement.sales_order, replacement.sales_order_item,
			replacement.material_request, replacement.material_request_item),
			(order.name, order.items[0].name, request.name, request.items[0].name))
		self.assertEqual((plan.docstatus, plan.get_items_from), (1, "Material Request"))
		self.assertEqual(len(plan.po_items), 1)
		row = plan.po_items[0]
		self.assertEqual((row.sales_order, row.sales_order_item, row.material_request, row.material_request_item),
			(order.name, order.items[0].name, request.name, request.items[0].name))
		self.assertEqual(replacement.production_plan_item, row.name)
		self.assertFalse(replacement.get("custom_replenishes_work_order"))
		self.assertEqual(original.reload().status, "Completed")
		self.assertEqual(sum(flt(doc.qty) for doc in (original, replacement)), 2)
		self.assertEqual(flt(frappe.db.get_single_value("Manufacturing Settings",
			"overproduction_percentage_for_sales_order")), 0)
		readiness = get_production_plan_readiness(self.company, [order.items[0].name])
		self.assertIn(order.items[0].name, readiness)
		graph_orders = [row for graph in readiness[order.items[0].name] for row in graph["work_orders"]]
		self.assertIn(replacement.name, {row["name"] for row in graph_orders})
		shortages = calculate_plan_purchase_shortages(readiness, [order.items[0].name])
		material = [row for row in shortages if row["item_code"] == self.material]
		self.assertEqual(len(material), 1)
		self.assertEqual(material[0]["shortage_qty"], 1)
		self.assertEqual({row["work_order"] for row in material[0]["sources"]}, {replacement.name})
		self.assertEqual(bulk_materials._work_order(replacement.name, None).name, replacement.name)

	def test_each_return_row_quota_is_independent_of_larger_net_demand(self):
		order, original, delivery = self._delivered(2)
		first, second = self._return(delivery), self._return(delivery)
		self.assertEqual(self._remaining(order, first.items[0].name), 1)
		self._reject_plan(order, qty=2, return_item=first.items[0].name)
		self._plan(order, return_item=first.items[0].name)
		self.assertEqual(self._remaining(order, first.items[0].name), 0)
		self.assertEqual(self._remaining(order, second.items[0].name), 1)

	def test_net_gap_limits_replacement_even_with_larger_return_quota(self):
		order, original, returned = self._returned(2)
		self._stock_receipt(self.finished, self.fg, 1)
		self._reject_plan(order, qty=2, return_item=returned.items[0].name)
		replacement = self._plan(order, return_item=returned.items[0].name)
		self.assertEqual(replacement.qty, 1)
		self._reject_plan(order, return_item=returned.items[0].name)

	def test_duplicate_and_stopped_request_do_not_release_return_quota(self):
		order, original, returned = self._returned()
		replacement = self._plan(order, return_item=returned.items[0].name)
		self._reject_plan(order, return_item=returned.items[0].name)
		request = frappe.get_doc("Material Request", replacement.material_request)
		request.update_status("Stopped")
		stop_unstop(replacement.name, "Stopped")
		self.assertEqual(self._remaining(order, returned.items[0].name), 0)
		self._reject_plan(order, return_item=returned.items[0].name)

	def test_completed_replacement_does_not_recycle_old_return_quota(self):
		order, original, returned = self._returned()
		replacement = self._plan(order, return_item=returned.items[0].name)
		self._manufacture(replacement)
		second_delivery = self._deliver(order, 1)
		second_return = self._return(second_delivery)
		self.assertEqual(replacement.reload().status, "Completed")
		self.assertEqual(self._remaining(order, returned.items[0].name), 0)
		self._reject_plan(order, return_item=returned.items[0].name)
		self.assertEqual(self._remaining(order, second_return.items[0].name), 1)
		self._plan(order, return_item=second_return.items[0].name)

	def test_active_replacement_blocks_source_return_cancellation(self):
		order, original, returned = self._returned()
		replacement = self._plan(order, return_item=returned.items[0].name)
		before = frappe.db.get_value("Bin", {"item_code": self.finished, "warehouse": self.quarantine}, "actual_qty")
		frappe.db.savepoint("return_cancel_denied")
		with self.assertRaises(frappe.ValidationError):
			returned.cancel()
		frappe.db.rollback(save_point="return_cancel_denied")
		self.assertEqual(returned.reload().docstatus, 1)
		self.assertEqual(replacement.reload().docstatus, 1)
		self.assertEqual(frappe.db.get_value("Bin", {"item_code": self.finished,
			"warehouse": self.quarantine}, "actual_qty"), before)

	def test_return_cancel_contention_is_clear_and_rolls_back(self):
		order, original, returned = self._returned()

		def snapshot():
			return {
				"delivery_notes": [frappe.get_doc("Delivery Note", name).as_dict()
					for name in (returned.return_against, returned.name)],
				"sales_order_item": frappe.db.get_value("Sales Order Item", order.items[0].name,
					"*", as_dict=True),
				"stock_ledger": frappe.get_all("Stock Ledger Entry",
					filters={"item_code": ("in", [self.material, self.finished])}, fields=["*"], order_by="name"),
				"reservations": frappe.get_all("Stock Reservation Entry",
					filters={"item_code": ("in", [self.material, self.finished])}, fields=["*"], order_by="name"),
				"stock": frappe.get_all("Bin", filters={"item_code": ("in", [self.material, self.finished])},
					fields=["item_code", "warehouse", "actual_qty", "stock_value", "reserved_stock"], order_by="name"),
			}

		before = snapshot()
		for error_type, code in ((frappe.QueryDeadlockError, 1020), (frappe.QueryTimeoutError, 1205)):
			with self.subTest(error=error_type.__name__):
				frappe.db.savepoint("return_cancel_contention")
				try:
					with patch.object(customer_returns, "get_committed_qty",
						side_effect=error_type(code, "injected return commitment conflict")) as committed:
						with self.assertRaisesRegex(frappe.ValidationError,
							"退货单或补产计划正在更新，请刷新后重试。") as caught:
							frappe.get_doc("Delivery Note", returned.name).cancel()
						committed.assert_called_once_with(returned.items[0].name, lock=True)
					self.assertNotIsInstance(caught.exception, (frappe.QueryDeadlockError, frappe.QueryTimeoutError))
				finally:
					# Reproduce the request owner's rollback without committing the seed.
					frappe.db.rollback(save_point="return_cancel_contention")
				self.assertEqual(snapshot(), before,
					"Rejected cancellation must preserve return/source DN, SOI, SLE, SRE and stock")

	def test_unexecuted_cancelled_chain_releases_quota(self):
		order, original, returned = self._returned()
		replacement = self._plan(order, return_item=returned.items[0].name)
		request = frappe.get_doc("Material Request", replacement.material_request)
		plan = frappe.get_doc("Production Plan", replacement.production_plan)
		replacement.cancel()
		# Native WO cancellation updates the linked plan/MR. Refresh just as the
		# next UI action would, instead of cancelling a stale in-memory copy.
		plan.reload().cancel()
		request.reload().cancel()
		self.assertEqual(self._remaining(order, returned.items[0].name), 1)
		self._plan(order, return_item=returned.items[0].name)

	def test_executed_closed_chain_cannot_release_quota_or_cancel_request(self):
		order, original, returned = self._returned()
		replacement = self._plan(order, return_item=returned.items[0].name)
		self._manufacture(replacement)
		close_work_order(replacement.name, "Closed")
		self.assertEqual(replacement.reload().status, "Closed")
		request = frappe.get_doc("Material Request", replacement.material_request)
		with self.assertRaises(frappe.ValidationError):
			customer_returns.before_cancel_replacement_request(request)
		self.assertEqual(self._remaining(order, returned.items[0].name), 0)

	def test_untrusted_material_request_cannot_claim_return_source(self):
		order, original, returned = self._returned()
		request = frappe.get_doc(dict(doctype="Material Request", company=self.company,
			material_request_type="Manufacture", schedule_date=add_days(nowdate(), 7),
			items=[dict(item_code=self.finished, qty=1, warehouse=self.fg,
				bom_no=self.bom.name, sales_order=order.name, sales_order_item=order.items[0].name,
				schedule_date=add_days(nowdate(), 7))]))
		request.set(customer_returns.RETURN_FIELD, returned.name)
		request.set(customer_returns.RETURN_ITEM_FIELD, returned.items[0].name)
		before = self._documents()
		with self.assertRaises(frappe.ValidationError):
			request.insert()
		self.assertEqual(self._documents(), before)
		self.assertEqual(self._remaining(order, returned.items[0].name), 1)

	def test_mr_submit_failure_restores_scoped_permissions_and_rolls_back(self):
		order, original, returned = self._returned()
		before = self._documents()
		captured = []
		submit = MaterialRequest.submit

		def fail_after_submit(doc):
			captured.append(doc)
			submit(doc)
			raise RuntimeError("injected failure after derived MR submit")

		with patch.object(MaterialRequest, "submit", fail_after_submit):
			with self.assertRaisesRegex(RuntimeError, "injected failure"):
				self._plan(order, return_item=returned.items[0].name)
		self.assertEqual(len(captured), 1)
		self.assertFalse(captured[0].flags.get("ignore_permissions"))
		self.assertNotIn(customer_returns._REQUEST_FLAG, frappe.flags)
		self.assertEqual(self._documents(), before)
		self.assertFalse(frappe.db.exists("Material Request Item", {"parent": captured[0].name}))
		self.assertEqual(self._remaining(order, returned.items[0].name), 1)

	def test_native_copy_cannot_create_an_extra_work_order_from_same_request(self):
		order, original, returned = self._returned()
		replacement = self._plan(order, return_item=returned.items[0].name)
		copy = frappe.copy_doc(replacement)
		# Copy normally clears some references. An API caller restoring them must
		# still be denied by the insertion boundary, before another draft exists.
		for key in ("material_request", "material_request_item", "sales_order", "sales_order_item",
			"production_plan", "production_plan_item"):
			copy.set(key, replacement.get(key))
		before = self._documents()
		with self.assertRaisesRegex(frappe.ValidationError, "只能随对应计划生成"):
			copy.insert()
		self.assertEqual(self._documents(), before)
		self.assertEqual(self._remaining(order, returned.items[0].name), 0)

	def test_submitted_work_order_cannot_clear_or_swap_its_source(self):
		order, original, returned = self._returned()
		replacement = self._plan(order, return_item=returned.items[0].name)
		generic = frappe.get_doc(dict(doctype="Material Request", company=self.company,
			material_request_type="Manufacture", schedule_date=add_days(nowdate(), 7),
			items=[dict(item_code=self.finished, qty=1, warehouse=self.fg,
				schedule_date=add_days(nowdate(), 7))])).insert().submit()
		source = (replacement.material_request, replacement.material_request_item,
			replacement.sales_order, replacement.sales_order_item)
		for target, detail in ((None, None), (generic.name, generic.items[0].name)):
			with self.subTest(material_request=target):
				doc = frappe.get_doc("Work Order", replacement.name)
				doc.material_request, doc.material_request_item = target, detail
				with self.assertRaises(frappe.ValidationError):
					doc.save()
				doc.reload()
				self.assertEqual((doc.material_request, doc.material_request_item,
					doc.sales_order, doc.sales_order_item), source)
		self.assertEqual(self._remaining(order, returned.items[0].name), 0)

	def _returned_two_level_order(self):
		semi = frappe.get_doc(dict(doctype="Item", item_code=self.prefix + "-SEMI",
			item_name=self.prefix + "-SEMI", item_group="All Item Groups", stock_uom="Nos",
			is_stock_item=1, is_purchase_item=1, include_item_in_manufacturing=1, valuation_rate=1)).insert().name
		semi_bom = frappe.get_doc(dict(doctype="BOM", item=semi, company=self.company,
			currency=self.bom.currency, quantity=1, is_active=1, is_default=1,
			default_source_warehouse=self.raw, items=[dict(item_code=self.material,
				qty=1, rate=1, source_warehouse=self.raw)])).insert().submit()
		self.bom = frappe.get_doc(dict(doctype="BOM", item=self.finished, company=self.company,
			currency=self.bom.currency, quantity=1, is_active=1, is_default=1,
			default_source_warehouse=self.raw, items=[dict(item_code=semi, bom_no=semi_bom.name,
				qty=1, rate=1, source_warehouse=self.raw)])).insert().submit()
		order = self._order()
		initial = actions.create_work_order(order.name, order.items[0].name, 1)
		self.assertEqual(len(initial["work_orders"]), 2)
		originals = {doc.production_item: doc for doc in
			(frappe.get_doc("Work Order", name) for name in initial["work_orders"])}
		self._manufacture(originals[semi])
		self._manufacture(originals[self.finished], receive_raw=False)
		returned = self._return(self._deliver(order, 1))
		return order, returned, semi

	def test_two_level_replacement_keeps_mr_only_on_finished_work_order(self):
		order, returned, semi = self._returned_two_level_order()
		result = actions.create_work_order(order.name, order.items[0].name, 1,
			return_delivery_note_item=returned.items[0].name)
		self.assertEqual(len(result["work_orders"]), 2)
		orders = {doc.production_item: doc for doc in
			(frappe.get_doc("Work Order", name) for name in result["work_orders"])}
		self.assertEqual(set(orders), {semi, self.finished})
		root, child = orders[self.finished], orders[semi]
		request = frappe.get_doc("Material Request", root.material_request)
		self.assertEqual(len(request.items), 1)
		self.assertEqual((request.items[0].item_code, request.items[0].stock_qty), (self.finished, 1))
		self.assertEqual(root.material_request_item, request.items[0].name)
		self.assertFalse(child.material_request)
		self.assertFalse(child.material_request_item)
		self.assertTrue(child.production_plan_sub_assembly_item)
		self.assertEqual(root.production_plan, child.production_plan)
		for doc in orders.values():
			self.assertEqual((doc.docstatus, doc.qty, doc.sales_order, doc.sales_order_item),
				(1, 1, order.name, order.items[0].name))
			self.assertEqual(bulk_materials._work_order(doc.name, None).name, doc.name)
		readiness = get_production_plan_readiness(self.company, [order.items[0].name])
		graphs = [plan for plan in readiness[order.items[0].name] if plan["name"] == root.production_plan]
		self.assertEqual(len(graphs), 1)
		self.assertEqual({row["name"] for row in graphs[0]["work_orders"]}, {root.name, child.name})
		self.assertEqual(customer_returns.get_committed_qty(returned.items[0].name), 1)

	def test_native_omitted_root_or_child_rolls_back_entire_replacement(self):
		order, returned, semi = self._returned_two_level_order()
		before = self._documents()
		insert = WorkOrder.insert
		for omitted_item in (self.finished, semi):
			with self.subTest(omitted_item=omitted_item):
				skipped = []

				def skip_one_native_work_order(doc, *args, **kwargs):
					if doc.production_item == omitted_item:
						skipped.append(doc.production_item)
						# ProductionPlan.create_work_order catches this native error
						# and proceeds; the guided adapter must reject the partial set.
						raise OverProductionError("injected omitted work order")
					return insert(doc, *args, **kwargs)

				with patch.object(WorkOrder, "insert", skip_one_native_work_order):
					with self.assertRaisesRegex(frappe.ValidationError, "未完整生成"):
						actions.create_work_order(order.name, order.items[0].name, 1,
							return_delivery_note_item=returned.items[0].name)
				self.assertEqual(skipped, [omitted_item])
				self.assertEqual(self._documents(), before)
				self.assertEqual(self._remaining(order, returned.items[0].name), 1)
				self.assertNotIn(customer_returns._REQUEST_FLAG, frappe.flags)
				self.assertNotIn(customer_returns._PLAN_FLAG, frappe.flags)
