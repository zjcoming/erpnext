"""Native lifecycle regression for ERPNext PR #58467; no custom reporting fixtures."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from erpnext.manufacturing.doctype.work_order.work_order import make_job_card


class TestPendingQtyRegression(IntegrationTestCase):
	COMPANY = "_Test Pending Job Card"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("Company", cls.COMPANY):
			frappe.get_doc(dict(doctype="Company", company_name=cls.COMPANY, abbr="PJC",
				default_currency="INR", country="India",
				create_chart_of_accounts_based_on="Standard Template")).insert()
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.addCleanup(frappe.db.rollback)
		self.enterContext(self.change_settings("Manufacturing Settings", {
			"disable_capacity_planning": 1, "enforce_time_logs": 0,
			"overproduction_percentage_for_work_order": 0,
		}))

	def _partially_completed_work_order(self):
		prefix = "PENDING-58467-" + frappe.generate_hash(length=8)
		warehouse = frappe.get_doc(dict(doctype="Warehouse", warehouse_name=prefix,
			company=self.COMPANY)).insert().name
		items = []
		for suffix in ("RM", "FG"):
			items.append(frappe.get_doc(dict(doctype="Item", item_code=prefix + "-" + suffix,
				item_name=prefix + "-" + suffix, item_group="Products", stock_uom="Nos",
				is_stock_item=1, include_item_in_manufacturing=1, valuation_rate=10)).insert().name)
		workstation = frappe.get_doc(dict(doctype="Workstation", workstation_name=prefix)).insert().name
		operation = frappe.get_doc(dict(doctype="Operation", name=prefix, workstation=workstation)).insert().name
		bom = frappe.get_doc(dict(doctype="BOM", company=self.COMPANY, item=items[1],
			currency="INR", quantity=1, is_active=1, is_default=1, with_operations=1,
			track_semi_finished_goods=1,
			items=[dict(item_code=items[0], qty=1, uom="Nos", stock_uom="Nos", rate=10,
				operation_row_id=1)],
			operations=[dict(operation=operation, workstation=workstation, finished_good=items[1],
				finished_good_qty=1, is_final_finished_good=1, sequence_id=1, time_in_mins=1,
				source_warehouse=warehouse, fg_warehouse=warehouse, skip_material_transfer=1)]))
		bom.insert().submit()
		wo = frappe.get_doc(dict(doctype="Work Order", company=self.COMPANY,
			production_item=items[1], bom_no=bom.name, qty=5, stock_uom="Nos",
			source_warehouse=warehouse, wip_warehouse=warehouse, fg_warehouse=warehouse,
			reserve_stock=0, skip_transfer=1, transfer_material_against="Work Order",
			planned_start_date=now_datetime()))
		wo.get_items_and_operations_from_bom()
		wo.insert().submit()
		cards = frappe.get_all("Job Card", filters={"work_order": wo.name}, pluck="name")
		self.assertEqual(len(cards), 1)
		card = frappe.get_doc("Job Card", cards[0])
		self._complete_and_submit(card, qty=2, for_quantity=5, pending_qty=3)
		wo.reload()
		self.assertEqual((wo.qty, wo.produced_qty, wo.operations[0].completed_qty), (5, 0, 2))
		self.assertEqual((card.for_quantity, card.pending_qty, card.process_loss_qty,
			card.total_completed_qty, card.manufactured_qty, card.docstatus, card.status),
			(5, 3, 0, 2, 0, 1, "Work In Progress"))
		self.assertEqual(frappe.db.count("Stock Ledger Entry", {"item_code": ["in", items]}), 0)
		return wo, card, items, warehouse

	def _complete_and_submit(self, card, *, qty, for_quantity, pending_qty, started=None):
		started = started or add_to_date(now_datetime(), minutes=-10)
		card.append("time_logs", {"from_time": started})
		card.save()
		card.complete_job_card(qty=qty, for_quantity=for_quantity, pending_qty=pending_qty,
			process_loss_qty=0, end_time=add_to_date(started, minutes=1))
		card.reload().submit()

	def _make_follow_up(self, wo, qty, pending_qty=3):
		make_job_card(wo.name, [dict(name=wo.operations[0].name,
			operation=wo.operations[0].operation, qty=qty, pending_qty=pending_qty)], wo.bom_no)

	def test_pending_three_allows_follow_up_three_and_native_completion(self):
		wo, original, items, warehouse = self._partially_completed_work_order()
		before = original.reload().as_dict()
		self._make_follow_up(wo, 3)
		cards = frappe.get_all("Job Card", filters={"work_order": wo.name}, pluck="name")
		self.assertEqual(len(cards), 2)
		follow_up = frappe.get_doc("Job Card", next(name for name in cards if name != original.name))
		self.assertEqual((follow_up.for_quantity, follow_up.operation_id, follow_up.company),
			(3, wo.operations[0].name, self.COMPANY))
		self.assertEqual(original.reload().as_dict(), before)
		# Native manual cards default to transfer-required; select the same
		# supported skip-transfer mode used by the fixture's initial card.
		follow_up.skip_material_transfer = 1
		self._complete_and_submit(follow_up, qty=3, for_quantity=3, pending_qty=0,
			started=add_to_date(original.actual_end_date, minutes=1))
		wo.reload()
		self.assertEqual((wo.produced_qty, wo.operations[0].completed_qty), (0, 5))
		self.assertEqual((follow_up.docstatus, follow_up.total_completed_qty), (1, 3))
		self.assertEqual(frappe.db.count("Stock Ledger Entry", {"item_code": ["in", items]}), 0)

	def test_pending_release_keeps_request_and_total_quantity_limits(self):
		wo, original, items, warehouse = self._partially_completed_work_order()
		cards = frappe.get_all("Job Card", filters={"work_order": wo.name}, pluck="name")
		ledger = frappe.get_all("Stock Ledger Entry", filters={"item_code": ["in", items]},
			fields=["name", "actual_qty", "qty_after_transaction"], order_by="name")
		with self.assertRaisesRegex(frappe.ValidationError, "greater than pending quantity"):
			self._make_follow_up(wo, qty=4, pending_qty=3)
		# Exercise validate_job_card_qty itself as well, with an oversized card.
		frappe.db.savepoint("pending_overallocation")
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be greater than Qty To Manufacture"):
			self._make_follow_up(wo, qty=6, pending_qty=6)
		frappe.db.rollback(save_point="pending_overallocation")
		self.assertEqual(frappe.get_all("Job Card", filters={"work_order": wo.name}, pluck="name"), cards)
		self.assertEqual(frappe.get_all("Stock Ledger Entry", filters={"item_code": ["in", items]},
			fields=["name", "actual_qty", "qty_after_transaction"], order_by="name"), ledger)
		self.assertEqual(original.reload().pending_qty, 3)
