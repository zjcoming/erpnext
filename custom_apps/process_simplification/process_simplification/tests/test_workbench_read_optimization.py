from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from process_simplification.api import production, setup, workbench
from process_simplification.workbench_read import workbench_read_context


class TestWorkbenchReadOptimization(TestCase):
	def setUp(self):
		self.local = self.enterContext(patch.object(frappe, "local", SimpleNamespace(
			site="test.localhost", session=SimpleNamespace(user="manager@example.com"),
			request=None, form_dict={}, message_log=[], document_cache={},
		)))
		self.db = self.enterContext(patch.object(frappe, "db", Mock(), create=True))
		self.get_all = self.enterContext(patch.object(frappe, "get_all", Mock()))

	def test_mutation_helpers_outside_read_scope_always_query_again(self):
		self.db.get_value.side_effect = ["BOM-OLD", "BOM-NEW"]
		self.get_all.side_effect = [[frappe._dict(qty=8)], [frappe._dict(qty=3)]]
		self.assertEqual(setup.get_default_bom("FG"), "BOM-OLD")
		self.assertEqual(setup.get_default_bom("FG"), "BOM-NEW")
		self.assertEqual(workbench.get_work_orders("SO", "SOI", "FG")[0].qty, 8)
		self.assertEqual(workbench.get_work_orders("SO", "SOI", "FG")[0].qty, 3)
		self.assertEqual(self.db.get_value.call_count, 2)
		self.assertEqual(self.get_all.call_count, 2)

	def test_nested_reads_reuse_queries_and_return_independent_rows(self):
		original = [frappe._dict(name="WO", required_items=[frappe._dict(qty=8)])]
		self.get_all.return_value = original
		with workbench_read_context():
			first = workbench.get_work_orders("SO", "SOI", "FG")
			first[0].required_items[0].qty = 1
			original[0].required_items[0].qty = 2
			with workbench_read_context():
				second = workbench.get_work_orders(sales_order="SO", sales_order_item="SOI", item_code="FG")
				self.assertEqual(second[0].required_items[0].qty, 8)
			self.assertEqual(workbench.get_work_orders("SO", "SOI", "FG"), second)
		self.get_all.assert_called_once()
		self.assertIsNot(first, second)
		self.assertIsNot(first[0], second[0])

	def test_query_keys_preserve_item_filter_and_user_and_site_scope(self):
		self.get_all.return_value = []
		with workbench_read_context():
			workbench.get_work_orders("SO", "SOI", "FG")
			workbench.get_work_orders("SO", "SOI")
			self.local.session.user = "warehouse@example.com"
			workbench.get_work_orders("SO", "SOI", "FG")
			self.local.site = "another.localhost"
			workbench.get_work_orders("SO", "SOI", "FG")
		self.assertEqual(self.get_all.call_count, 4)
		self.assertEqual(self.get_all.call_args_list[0].kwargs["filters"]["production_item"], "FG")
		self.assertNotIn("production_item", self.get_all.call_args_list[1].kwargs["filters"])

	def test_variant_fallback_and_missing_bom_are_reused_without_changing_lookup(self):
		self.db.get_value.side_effect = [None, "TEMPLATE", "BOM-TEMPLATE", None, None]
		with workbench_read_context():
			self.assertEqual(setup.get_default_bom("VARIANT"), "BOM-TEMPLATE")
			self.assertEqual(setup.get_default_bom(item_code="VARIANT"), "BOM-TEMPLATE")
			self.assertIsNone(setup.get_default_bom("NO-BOM"))
			self.assertIsNone(setup.get_default_bom("NO-BOM"))
		self.assertEqual(self.db.get_value.call_count, 5)
		self.assertEqual(self.db.get_value.call_args_list[2].args, (
			"BOM", {"item": "TEMPLATE", "is_default": 1, "is_active": 1, "docstatus": 1}, "name",
		))

	def test_scope_is_discarded_after_exception_and_next_calculation_is_fresh(self):
		self.db.get_value.side_effect = ["BOM-OLD", "BOM-NEW", "BOM-LATEST"]
		with self.assertRaisesRegex(ValueError, "failed read"):
			with workbench_read_context():
				self.assertEqual(setup.get_default_bom("FG"), "BOM-OLD")
				raise ValueError("failed read")
		with workbench_read_context():
			self.assertEqual(setup.get_default_bom("FG"), "BOM-NEW")
		self.assertEqual(setup.get_default_bom("FG"), "BOM-LATEST")
		self.assertEqual(self.db.get_value.call_count, 3)

	def test_failed_query_is_not_cached_even_when_caller_handles_error(self):
		self.get_all.side_effect = [ValueError("temporary error"), []]
		with workbench_read_context():
			with self.assertRaises(ValueError):
				workbench.get_work_orders("SO", "SOI", "FG")
			self.assertEqual(workbench.get_work_orders("SO", "SOI", "FG"), [])
			self.assertEqual(workbench.get_work_orders("SO", "SOI", "FG"), [])
		self.assertEqual(self.get_all.call_count, 2)

	def _install_overview_fixture(self, order_count=100):
		"""Exercise real aggregation, shared-stock allocation, filters and pagination."""
		self.work_order_qty = 6
		self.bom_suffix = "INITIAL"
		orders = []
		documents = {}
		for order_index in range(order_count):
			order = frappe._dict(
				name=f"SO-{order_index:03d}", company="Factory", customer=f"CUSTOMER-{order_index % 2}",
				customer_name=f"Customer {order_index % 2}", transaction_date="2026-09-01",
				delivery_date=f"2026-09-{11 + order_index % 3}", creation=f"2026-09-01 00:{order_index // 60:02d}:{order_index % 60:02d}",
			)
			items = [frappe._dict(
				name=f"SOI-{order_index:03d}-{item_index}", idx=item_index + 1,
				item_code=f"FG-{(order_index * 3 + item_index) % 10}", item_name="Historical name",
				warehouse="FG-WH", delivery_date=order.delivery_date,
				qty=10, stock_qty=10, delivered_qty=0, conversion_factor=1,
			) for item_index in range(3)]
			orders.append(order)
			documents[order.name] = SimpleNamespace(**order, items=items, check_permission=Mock())

		def get_all(doctype, **kwargs):
			if doctype == "Work Order":
				filters = kwargs["filters"]
				return [frappe._dict(
					name=f"WO-{filters['sales_order_item']}", production_item=filters.get("production_item"),
					qty=self.work_order_qty, produced_qty=0, process_loss_qty=0, status="Not Started",
				)]
			if doctype == "Stock Reservation Entry":
				return [frappe._dict(reserved_qty=1, delivered_qty=0, transferred_qty=0, consumed_qty=0)]
			if doctype == "Stock Entry":
				return []
			if doctype == "Item":
				return [frappe._dict(name=code, item_name=f"Current {code}") for code in kwargs["filters"]["name"][1]]
			raise AssertionError(f"Unexpected query: {doctype}")

		self.get_all.side_effect = get_all
		self.db.get_value.side_effect = lambda doctype, filters, field: f"BOM-{filters['item']}-{self.bom_suffix}"
		self.enterContext(patch.object(frappe, "has_permission", return_value=True))
		self.enterContext(patch.object(frappe, "get_doc", side_effect=lambda doctype, name: documents[name]))
		self.enterContext(patch.object(frappe, "get_list", side_effect=lambda doctype, **kwargs: orders if doctype == "Sales Order" else []))
		self.enterContext(patch.object(workbench, "_", lambda message: message))
		self.enterContext(patch.object(workbench, "ensure_submitted_sales_order"))
		self.enterContext(patch.object(workbench, "get_available_qty_to_reserve", return_value=35))
		self.enterContext(patch.object(workbench, "now_datetime", return_value=datetime(2026, 9, 10, 12)))
		self.enterContext(patch.object(production, "now_datetime", return_value=datetime(2026, 9, 10, 12)))
		self.readiness = self.enterContext(patch.object(production, "get_production_plan_readiness", return_value={}))
		self.enterContext(patch("process_simplification.api.production_readiness.get_production_plan_readiness", return_value={}))
		return documents

	def _without_query_reuse(self):
		stack = ExitStack()
		stack.enter_context(patch.object(workbench, "get_default_bom", setup.get_default_bom.__wrapped__))
		stack.enter_context(patch.object(workbench, "get_work_orders", workbench.get_work_orders.__wrapped__))
		stack.enter_context(patch.object(production, "get_work_orders", production.get_work_orders.__wrapped__))
		return stack

	def _query_counts(self):
		return {
			"work_orders": sum(call.args[0] == "Work Order" for call in self.get_all.call_args_list),
			"bom": self.db.get_value.call_count,
		}

	def test_100_order_overview_matches_uncached_results_with_fewer_queries(self):
		self._install_overview_fixture()
		with self._without_query_reuse():
			expected = production.get_production_overview(page_size=0, include_assignment_counts=False)
		self.assertEqual(self._query_counts(), {"work_orders": 600, "bom": 600})
		self.get_all.reset_mock()
		self.db.get_value.reset_mock()
		actual = production.get_production_overview(page_size=0, include_assignment_counts=False)
		self.assertEqual(actual, expected)
		self.assertEqual(self._query_counts(), {"work_orders": 300, "bom": 10})
		self.assertEqual(len(actual["demands"]), 300)
		self.assertEqual(sum(row["available_to_reserve"] for row in actual["demands"]), 350)

	def test_filtering_and_pagination_keep_global_stock_allocation_and_permissions(self):
		documents = self._install_overview_fixture(order_count=6)
		arguments = {"page": 2, "page_size": 2, "filters": {"customer": "CUSTOMER-1"}, "include_assignment_counts": False}
		with self._without_query_reuse():
			expected = production.get_production_overview(**arguments)
		actual = production.get_production_overview(**arguments)
		self.assertEqual(actual, expected)
		self.assertEqual(actual["pagination"]["total_count"], 9)
		self.assertEqual(len(actual["demands"]), 2)
		for document in documents.values():
			self.assertEqual(document.check_permission.call_count, 2)
			self.assertEqual(document.check_permission.call_args.args, ("read",))

	def test_successive_internal_calculations_read_after_mutation(self):
		self._install_overview_fixture(order_count=1)
		first = production.get_production_overview(page_size=0, include_assignment_counts=False)
		self.work_order_qty = 2
		second = production.get_production_overview(page_size=0, include_assignment_counts=False)
		self.assertEqual(first["demands"][0]["active_work_order_qty"], 6)
		self.assertEqual(second["demands"][0]["active_work_order_qty"], 2)
		self.assertEqual(self._query_counts(), {"work_orders": 6, "bom": 6})
		self.db.rollback.assert_not_called()

	def test_rpc_retry_discards_read_scope_before_reloading_after_rollback(self):
		self._install_overview_fixture(order_count=1)
		self.local.request = object()
		self.local.form_dict = {"cmd": f"{production.__name__}.get_production_overview"}
		self.readiness.side_effect = [frappe.QueryDeadlockError(1020), {}]
		def rolled_back():
			self.work_order_qty = 2
			self.bom_suffix = "AFTER-ROLLBACK"
		self.db.rollback.side_effect = rolled_back
		result = production.get_production_overview(page_size=0, include_assignment_counts=False)
		self.assertEqual(result["demands"][0]["active_work_order_qty"], 2)
		self.assertEqual(self._query_counts(), {"work_orders": 6, "bom": 6})
		self.db.rollback.assert_called_once_with()
		self.db.commit.assert_not_called()
		self.assertEqual(setup.get_default_bom("FG-0"), "BOM-FG-0-AFTER-ROLLBACK")
		self.assertEqual(self.db.get_value.call_count, 7)
