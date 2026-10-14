"""Stock-ledger regression for the exact upstream v16 PR #59945."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_to_date, flt, nowdate

from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.tests import test_pending_qty_regression as fixtures


class TestPendingMaterialShareRegression(IntegrationTestCase):
	COMPANY = fixtures.TestPendingQtyRegression.COMPANY
	_partially_completed_work_order = fixtures.TestPendingQtyRegression._partially_completed_work_order
	_complete_and_submit = fixtures.TestPendingQtyRegression._complete_and_submit
	_make_follow_up = fixtures.TestPendingQtyRegression._make_follow_up

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
			"backflush_raw_materials_based_on": "BOM",
			"overproduction_percentage_for_work_order": 0,
		}))

	def _balance(self, item, warehouse):
		return flt(frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty"))

	def test_pending_material_remains_for_follow_up_and_total_consumption_matches_production(self):
		wo, original, items, warehouse = self._partially_completed_work_order()
		make_stock_entry(item_code=items[0], qty=5, to_warehouse=warehouse,
			company=self.COMPANY, basic_rate=10, posting_date=add_days(nowdate(), -1),
			posting_time="12:00:00")
		first = frappe.get_doc(original.make_stock_entry_for_semi_fg_item())
		self.assertEqual(flt(next(row.qty for row in first.items if row.item_code == items[0])), 2)
		self.assertEqual(flt(next(row.qty for row in first.items if row.is_finished_item)), 2)
		first.submit()
		self.assertEqual(self._balance(items[0], warehouse), 3)
		self.assertEqual(self._balance(items[1], warehouse), 2)
		self.assertEqual((original.reload().for_quantity, original.pending_qty), (5, 3))
		self._make_follow_up(wo.reload(), 3)
		names = frappe.get_all("Job Card", filters={"work_order": wo.name}, pluck="name")
		follow_up = frappe.get_doc("Job Card", next(name for name in names if name != original.name))
		follow_up.skip_material_transfer = 1
		self._complete_and_submit(follow_up, qty=3, for_quantity=3, pending_qty=0,
			started=add_to_date(original.actual_end_date, minutes=1))
		second = frappe.get_doc(follow_up.make_stock_entry_for_semi_fg_item())
		self.assertEqual(flt(next(row.qty for row in second.items if row.item_code == items[0])), 3)
		second.submit()
		self.assertEqual(self._balance(items[0], warehouse), 0)
		self.assertEqual(self._balance(items[1], warehouse), 5)
		self.assertEqual((wo.reload().produced_qty, wo.status), (5, "Completed"))
		ledger = frappe.get_all("Stock Ledger Entry", filters={
			"voucher_no": ["in", [first.name, second.name]], "is_cancelled": 0,
		}, fields=["item_code", "actual_qty"])
		self.assertEqual(sum(row.actual_qty for row in ledger if row.item_code == items[0]), -5)
		self.assertEqual(sum(row.actual_qty for row in ledger if row.item_code == items[1]), 5)
