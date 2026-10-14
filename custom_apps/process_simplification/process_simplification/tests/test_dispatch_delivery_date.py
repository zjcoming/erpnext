"""Execute both personnel-list queries against a small read-only SQL fixture.

The fixture isolates list ordering/pagination from stock and wage services. It
uses SQLite's compatible SELECT/join/date operations; live MariaDB/role checks
remain part of deployment acceptance.
"""

from contextlib import ExitStack
import re
import sqlite3
from unittest import TestCase
from unittest.mock import patch

import frappe

from process_simplification.api import production_planning
from process_simplification.production_reporting import dispatch_pool


class TestDispatchDeliveryDate(TestCase):
	def setUp(self):
		self.db = sqlite3.connect(":memory:")
		self.db.row_factory = sqlite3.Row
		self.addCleanup(self.db.close)
		columns = {
			"Job Card": "work_order, company, production_item, operation, operation_id, workstation, sequence_id, for_quantity, total_completed_qty, process_loss_qty, docstatus, is_corrective_job_card, track_semi_finished_goods, is_subcontracted",
			"Work Order": "company, docstatus, status, production_plan, production_plan_item, production_plan_sub_assembly_item, sales_order, sales_order_item, expected_delivery_date",
			"Production Plan": "company, docstatus",
			"Production Plan Item": "parent, parenttype, sales_order, sales_order_item",
			"Production Plan Sub Assembly Item": "parent, parenttype, sales_order, sales_order_item",
			"Item": "item_name, description, stock_uom",
			"Sales Order": "company",
			"Sales Order Item": "parent, parenttype, delivery_date",
			"Job Card Prearrangement": "job_card, company, status",
			"Job Card Worker Assignment": "job_card",
			"Job Card Work Report": "job_card",
			"Job Card Assignment Movement": "job_card",
			"Job Card Time Log": "parent, parenttype",
			"Job Card Operation": "parent, parenttype, parentfield",
		}
		for table, fields in columns.items():
			self.db.execute(f"create table `tab{table}` (name text primary key, {fields})")
		self.insert("Production Plan", "PP", company="C", docstatus=1)
		self.insert("Item", "SEMI", item_name="半成品", stock_uom="Nos")
		self.insert("Sales Order", "SO", company="C")
		self.insert("Sales Order Item", "SO-LATE", parent="SO", parenttype="Sales Order", delivery_date="2026-10-12")
		self.insert("Sales Order Item", "SO-EARLY", parent="SO", parenttype="Sales Order", delivery_date="2026-10-10")
		self.stack = ExitStack()
		self.addCleanup(self.stack.close)
		self.stack.enter_context(patch.object(dispatch_pool, "_require_access", return_value=None))
		self.stack.enter_context(patch.object(frappe.db, "sql", side_effect=self.select))
		self.stack.enter_context(patch.object(frappe, "get_list", return_value=["C"]))
		self.stack.enter_context(patch.object(dispatch_pool.service, "get_work_order_assignment_context", return_value={"job_cards": []}))

	def insert(self, table, name, **values):
		values = {"name": name, **values}
		self.db.execute(f"insert into `tab{table}` ({','.join(values)}) values ({','.join('?' for _ in values)})", tuple(values.values()))

	def select(self, query, values=None, *, as_dict=False):
		self.assertTrue(query.strip().lower().startswith("select"), "Personnel reads must not mutate records")
		query = re.sub(r"%\((\w+)\)s", r":\1", query)
		rows = self.db.execute(query, values or {}).fetchall()
		return [frappe._dict(dict(row)) if as_dict else tuple(row) for row in rows]

	def order(self, name, **overrides):
		values = dict(company="C", docstatus=1, status="Not Started", production_plan="PP", sales_order="SO", sales_order_item="SO-LATE")
		values.update(overrides)
		self.insert("Work Order", name, **values)
		self.insert("Job Card", "JC-" + name, work_order=name, company="C", production_item="SEMI", operation="Cut", operation_id="OP", sequence_id=1, for_quantity=2, docstatus=0)
		self.insert("Job Card Prearrangement", "PLAN-" + name, job_card="JC-" + name, company="C", status="Planned")

	def pool(self, saved, **kwargs):
		return production_planning._saved_pool(filters={}, **kwargs) if saved else dispatch_pool.get_operation_dispatch_pool(**kwargs)

	def test_both_lists_resolve_exact_line_before_pagination_without_backfilling(self):
		self.order("A-LATE")
		self.order("Z-EARLY", sales_order_item="SO-EARLY")
		self.order("Z-OVERRIDE", expected_delivery_date="2026-10-09")
		self.order("Z-UNKNOWN", sales_order_item=None)
		before = tuple(self.db.iterdump())
		for saved in (False, True):
			with self.subTest(saved=saved):
				first = self.pool(saved, page=1, page_length=2)
				second = self.pool(saved, page=2, page_length=2)
				self.assertEqual(first["pagination"]["total_count"], 4)
				self.assertEqual([(row.work_order, row.delivery_date) for row in first["rows"]], [("Z-OVERRIDE", "2026-10-09"), ("Z-EARLY", "2026-10-10")])
				self.assertEqual([(row.work_order, row.delivery_date) for row in second["rows"]], [("A-LATE", "2026-10-12"), ("Z-UNKNOWN", None)])
		self.assertEqual(tuple(self.db.iterdump()), before)

	def test_missing_work_order_links_resolve_only_its_own_plan_row(self):
		self.insert("Production Plan Item", "PPI", parent="PP", parenttype="Production Plan", sales_order="SO", sales_order_item="SO-EARLY")
		self.insert("Production Plan Sub Assembly Item", "SUB", parent="PP", parenttype="Production Plan", sales_order="SO", sales_order_item="SO-LATE")
		self.insert("Production Plan Sub Assembly Item", "FOREIGN-SUB", parent="ANOTHER-PP", parenttype="Production Plan", sales_order="SO", sales_order_item="SO-EARLY")
		self.order("FG", sales_order=None, sales_order_item=None, production_plan_item="PPI")
		self.order("SEMI", sales_order=None, sales_order_item="", production_plan_sub_assembly_item="SUB")
		self.order("INVALID", sales_order_item=None, production_plan_sub_assembly_item="FOREIGN-SUB")
		self.order("DIRECT", sales_order_item="SO-LATE", production_plan_item="PPI")
		for saved in (False, True):
			rows = self.pool(saved, page=1, page_length=20)["rows"]
			self.assertEqual({row.work_order: row.delivery_date for row in rows}, {"FG": "2026-10-10", "SEMI": "2026-10-12", "INVALID": None, "DIRECT": "2026-10-12"})

	def test_same_delivery_date_has_stable_order_across_page_boundaries(self):
		for name in ("WO-C", "WO-A", "WO-B"):
			self.order(name)
		for saved in (False, True):
			first = self.pool(saved, page=1, page_length=2)["rows"]
			second = self.pool(saved, page=2, page_length=2)["rows"]
			repeated = self.pool(saved, page=1, page_length=2)["rows"]
			self.assertEqual([row.work_order for row in first + second], ["WO-A", "WO-B", "WO-C"])
			self.assertEqual(first, repeated)

	def test_order_and_company_mismatch_cannot_supply_a_delivery_date(self):
		self.insert("Sales Order", "OTHER", company="OTHER-COMPANY")
		self.insert("Sales Order Item", "FOREIGN-LINE", parent="OTHER", parenttype="Sales Order", delivery_date="2026-10-01")
		self.order("CROSS-COMPANY", sales_order="OTHER", sales_order_item="FOREIGN-LINE")
		self.order("WRONG-PARENT", sales_order="ANOTHER-SO", sales_order_item="SO-EARLY")
		for saved in (False, True):
			rows = self.pool(saved, page=1, page_length=20)["rows"]
			self.assertEqual(len(rows), 2)
			self.assertTrue(all(row.delivery_date is None for row in rows))

	def test_saved_stopped_plan_keeps_the_resolved_date_for_supervisor_cleanup(self):
		self.order("STOPPED", status="Stopped", sales_order_item="SO-EARLY")
		self.assertEqual(self.pool(False, page=1, page_length=20)["rows"], [])
		rows = self.pool(True, page=1, page_length=20)["rows"]
		self.assertEqual([(row.work_order, row.delivery_date) for row in rows], [("STOPPED", "2026-10-10")])
