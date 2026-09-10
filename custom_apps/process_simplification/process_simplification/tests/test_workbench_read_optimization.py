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

	def test_batch_queries_cover_all_orders_in_bounded_chunks_without_status_filter(self):
		orders = [frappe._dict(name=f"SO-{index}") for index in range(401)]
		self.get_all.side_effect = lambda doctype, **kwargs: (
			[frappe._dict(name="missing", parent=name, item_code="FG") for name in kwargs["filters"]["parent"][1]]
			if doctype == "Sales Order Item" else []
		)
		with workbench_read_context():
			workbench._prefetch_workbench_order_reads(orders)
			workbench._prefetch_workbench_order_reads(orders)
			for order in orders:
				self.assertEqual(workbench.get_work_orders(order.name, "missing", "FG"), [])
				self.assertEqual(workbench.get_effective_reserved_qty(order.name, "missing"), 0)
		self.assertEqual(self.get_all.call_count, 9)
		for doctype, field in (("Work Order", "sales_order"), ("Stock Reservation Entry", "voucher_no")):
			calls = [call for call in self.get_all.call_args_list if call.args[0] == doctype]
			self.assertEqual([len(call.kwargs["filters"][field][1]) for call in calls], [200, 200, 1])
			self.assertEqual([name for call in calls for name in call.kwargs["filters"][field][1]], [order.name for order in orders])
			for call in calls:
				self.assertEqual(call.kwargs["filters"]["docstatus"], 1)
				self.assertNotIn("status", call.kwargs["filters"])
				if doctype == "Work Order":
					self.assertEqual(call.kwargs["order_by"], "creation asc")
				else:
					self.assertEqual(call.kwargs["filters"]["voucher_type"], "Sales Order")

	def test_batch_work_orders_preserve_order_item_filter_history_fields_and_copies(self):
		rows = [frappe._dict(
			name=name, sales_order=order, sales_order_item=item, production_item=product,
			qty=10, produced_qty=produced, process_loss_qty=1, status=status, creation=name,
		) for name, order, item, product, produced, status in (
			("WO-COMPLETED", "SO-A", "ROW", "FG", 8, "Completed"),
			("WO-OTHER-ORDER", "SO-B", "ROW", "FG", 0, "Not Started"),
			("WO-OTHER-ITEM", "SO-A", "ANOTHER", "FG", 0, "Not Started"),
			("WO-ACTIVE", "SO-A", "ROW", "FG", 2, "In Process"),
		)]
		items = [frappe._dict(name=item, parent=order, item_code="FG") for order, item in (
			("SO-A", "ROW"), ("SO-A", "ANOTHER"), ("SO-B", "ROW"), ("SO-A", "missing"),
		)]
		self.get_all.side_effect = [items, rows, []]
		with workbench_read_context():
			workbench._prefetch_workbench_order_reads([frappe._dict(name="SO-A"), frappe._dict(name="SO-B")])
			actual = workbench.get_work_orders("SO-A", "ROW", "FG")
			self.assertEqual([row.name for row in actual], ["WO-COMPLETED", "WO-ACTIVE"])
			self.assertEqual(workbench.get_completed_qty(actual), 10)
			self.assertEqual(workbench.get_active_work_order_qty(actual), 7)
			self.assertEqual(set(actual[0]), set(workbench._WORK_ORDER_FIELDS))
			actual[0].produced_qty = 99
			rows[0].produced_qty = 88
			self.assertEqual(workbench.get_work_orders("SO-A", "ROW", "FG")[0].produced_qty, 8)
			self.assertEqual([row.name for row in workbench.get_work_orders("SO-A", "ROW")], ["WO-COMPLETED", "WO-ACTIVE"])
			self.assertEqual(workbench.get_work_orders("SO-A", "missing", "FG"), [])
		self.assertEqual(self.get_all.call_count, 3)

	def test_batch_reservations_clamp_each_entry_before_summing_and_keep_order_item_keys(self):
		entries = [frappe._dict(
			voucher_no=order, voucher_detail_no=item, reserved_qty=reserved,
			delivered_qty=delivered, transferred_qty=transferred, consumed_qty=consumed,
		) for order, item, reserved, delivered, transferred, consumed in (
			("SO-A", "ROW", 10, 2, 1, 3),
			("SO-A", "ROW", 2, 5, 0, 0),
			("SO-A", "ROW", 3, 0, 0, 0),
			("SO-B", "ROW", 50, 0, 0, 0),
			("SO-A", "ANOTHER", 20, 0, 0, 0),
		)]
		items = [frappe._dict(name=item, parent=order, item_code="FG") for order, item in (
			("SO-A", "ROW"), ("SO-A", "ANOTHER"), ("SO-B", "ROW"), ("SO-A", "missing"),
		)]
		self.get_all.side_effect = [items, [], entries]
		with workbench_read_context():
			workbench._prefetch_workbench_order_reads([frappe._dict(name="SO-A"), frappe._dict(name="SO-B")])
			self.assertEqual(workbench.get_effective_reserved_qty("SO-A", "ROW"), 7)
			self.assertEqual(workbench.get_effective_reserved_qty("SO-B", "ROW"), 50)
			self.assertEqual(workbench.get_effective_reserved_qty("SO-A", "ANOTHER"), 20)
			self.assertEqual(workbench.get_effective_reserved_qty("SO-A", "missing"), 0)
		self.assertEqual(self.get_all.call_count, 3)

	def test_batch_prefetch_outside_calculation_is_noop_and_mutation_reservations_stay_fresh(self):
		workbench._prefetch_workbench_order_reads([frappe._dict(name="SO")])
		self.get_all.assert_not_called()
		self.get_all.side_effect = [
			[frappe._dict(reserved_qty=5, delivered_qty=1)],
			[frappe._dict(reserved_qty=5, delivered_qty=3)],
		]
		self.assertEqual(workbench.get_effective_reserved_qty("SO", "ROW"), 4)
		self.assertEqual(workbench.get_effective_reserved_qty("SO", "ROW"), 2)

	def test_batch_reservations_preserve_builtin_fractional_and_mixed_magnitude_sum(self):
		quantities = {"FRACTION": [0.1] * 10, "LARGE": [1e16, 1, 1]}
		items = [frappe._dict(name=name, parent="SO", item_code="FG") for name in quantities]
		entries = [frappe._dict(voucher_no="SO", voucher_detail_no=name, reserved_qty=value)
			for name, values in quantities.items() for value in values]
		self.get_all.side_effect = [items, [], entries]
		with workbench_read_context():
			workbench._prefetch_workbench_order_reads([frappe._dict(name="SO")])
			for name, values in quantities.items():
				self.assertEqual(workbench.get_effective_reserved_qty("SO", name), sum(values))

	def test_batch_identity_change_does_not_expose_another_user_or_site_snapshot(self):
		self.get_all.side_effect = lambda doctype, **kwargs: (
			[frappe._dict(name="ROW", parent="SO", item_code="FG")] if doctype == "Sales Order Item" else []
		)
		with workbench_read_context():
			workbench._prefetch_workbench_order_reads([frappe._dict(name="SO")])
			self.assertEqual(workbench.get_work_orders("SO", "ROW", "FG"), [])
			self.local.session.user = "another@example.com"
			workbench.get_work_orders("SO", "ROW", "FG")
			workbench.get_effective_reserved_qty("SO", "ROW")
			self.local.site = "another.localhost"
			workbench.get_work_orders("SO", "ROW", "FG")
			workbench.get_effective_reserved_qty("SO", "ROW")
		self.assertEqual(self.get_all.call_count, 7)
		for call in self.get_all.call_args_list[3:]:
			self.assertIn("ROW", call.kwargs["filters"].values())

	def test_failed_batch_does_not_publish_partial_snapshots(self):
		items = [frappe._dict(name="ROW", parent="SO", item_code="FG")]
		self.get_all.side_effect = [items, [], ValueError("reservation read failed"), items, [], []]
		with workbench_read_context():
			with self.assertRaisesRegex(ValueError, "reservation read failed"):
				workbench._prefetch_workbench_order_reads([frappe._dict(name="SO")])
			workbench._prefetch_workbench_order_reads([frappe._dict(name="SO")])
			self.assertEqual(workbench.get_work_orders("SO", "ROW", "FG"), [])
			self.assertEqual(workbench.get_effective_reserved_qty("SO", "ROW"), 0)
		self.assertEqual(self.get_all.call_count, 6)

	def test_noncanonical_helper_arguments_and_stored_products_use_original_sql_collation(self):
		for stored_product, argument in (("FG-A", "fg-a"), ("fg-a", "FG-A"), ("OTHER", "FG-A")):
			with self.subTest(stored_product=stored_product, argument=argument):
				self.get_all.reset_mock()
				items = [frappe._dict(name="ROW", parent="SO", item_code="FG-A")]
				rows = [frappe._dict(name="WO", sales_order="SO", sales_order_item="ROW", production_item=stored_product)]
				original_query_result = [frappe._dict(name="SQL-RESULT")]
				self.get_all.side_effect = [items, rows, [], original_query_result]
				with workbench_read_context():
					workbench._prefetch_workbench_order_reads([frappe._dict(name="SO")])
					self.assertEqual(workbench.get_work_orders("SO", "ROW", argument), original_query_result)
				self.assertEqual(self.get_all.call_args.kwargs["filters"], {
					"sales_order": "SO", "sales_order_item": "ROW", "docstatus": 1, "production_item": argument,
				})

	def test_noncanonical_stored_anchor_keys_fall_back_without_guessing_database_collation(self):
		for order, item in (("so", "ROW"), ("SO", "row")):
			with self.subTest(order=order, item=item):
				items = [frappe._dict(name="ROW", parent="SO", item_code="FG")]
				rows = [frappe._dict(name="WO", sales_order=order, sales_order_item=item, production_item="FG")]
				reservations = [frappe._dict(voucher_no=order, voucher_detail_no=item, reserved_qty=3)]
				self.get_all.side_effect = [items, rows, reservations, [frappe._dict(name="SQL-WO")], [frappe._dict(reserved_qty=3)]]
				with workbench_read_context():
					workbench._prefetch_workbench_order_reads([frappe._dict(name="SO")])
					self.assertEqual(workbench.get_work_orders("SO", "ROW", "FG")[0].name, "SQL-WO")
					self.assertEqual(workbench.get_effective_reserved_qty("SO", "ROW"), 3)

	def test_equal_creation_times_retain_original_filtered_query_order(self):
		items = [frappe._dict(name="ROW", parent="SO", item_code="FG")]
		rows = [frappe._dict(name=name, sales_order="SO", sales_order_item="ROW", production_item="FG", creation="2026-09-10") for name in ("WO-1", "WO-2")]
		self.get_all.side_effect = [items, rows, [], list(reversed(rows))]
		with workbench_read_context():
			workbench._prefetch_workbench_order_reads([frappe._dict(name="SO")])
			self.assertEqual([row.name for row in workbench.get_work_orders("SO", "ROW", "FG")], ["WO-2", "WO-1"])
		self.assertEqual(self.get_all.call_args.kwargs["order_by"], "creation asc")

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
			if doctype == "Sales Order Item":
				return [frappe._dict(name=item.name, parent=name, item_code=item.item_code)
					for name in kwargs["filters"]["parent"][1] for item in documents[name].items]
			if doctype == "Work Order":
				filters = kwargs["filters"]
				order_filter = filters["sales_order"]
				names = order_filter[1] if isinstance(order_filter, list) else [order_filter]
				rows = [frappe._dict(
					name=f"WO-{item.name}", production_item=item.item_code,
					sales_order=name, sales_order_item=item.name,
					qty=self.work_order_qty, produced_qty=0, process_loss_qty=0, status="Not Started",
				) for name in names for item in documents[name].items
					if not filters.get("sales_order_item") or item.name == filters["sales_order_item"]]
				return [frappe._dict({field: row.get(field) for field in kwargs["fields"]}) for row in rows]
			if doctype == "Stock Reservation Entry":
				filters = kwargs["filters"]
				order_filter = filters["voucher_no"]
				names = order_filter[1] if isinstance(order_filter, list) else [order_filter]
				return [frappe._dict(
					voucher_no=name, voucher_detail_no=item.name,
					reserved_qty=1, delivered_qty=0, transferred_qty=0, consumed_qty=0,
				) for name in names for item in documents[name].items
					if not filters.get("voucher_detail_no") or item.name == filters["voucher_detail_no"]]
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
		stack.enter_context(patch.object(workbench, "_prefetch_workbench_order_reads"))
		stack.enter_context(patch.object(workbench, "get_default_bom", setup.get_default_bom.__wrapped__))
		stack.enter_context(patch.object(workbench, "get_work_orders", workbench.get_work_orders.__wrapped__))
		stack.enter_context(patch.object(production, "get_work_orders", production.get_work_orders.__wrapped__))
		return stack

	def _query_counts(self):
		return {
			"work_orders": sum(call.args[0] == "Work Order" for call in self.get_all.call_args_list),
			"reservations": sum(call.args[0] == "Stock Reservation Entry" for call in self.get_all.call_args_list),
			"bom": self.db.get_value.call_count,
		}

	def test_100_order_overview_matches_uncached_results_with_fewer_queries(self):
		self._install_overview_fixture()
		with self._without_query_reuse():
			expected = production.get_production_overview(page_size=0, include_assignment_counts=False)
		self.assertEqual(self._query_counts(), {"work_orders": 600, "reservations": 300, "bom": 600})
		self.get_all.reset_mock()
		self.db.get_value.reset_mock()
		actual = production.get_production_overview(page_size=0, include_assignment_counts=False)
		self.assertEqual(actual, expected)
		self.assertEqual(self._query_counts(), {"work_orders": 1, "reservations": 1, "bom": 10})
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
		self.assertEqual(self._query_counts(), {"work_orders": 2, "reservations": 2, "bom": 6})
		self.db.rollback.assert_not_called()

	def test_prefetch_uses_readable_orders_and_retains_each_document_permission_check(self):
		documents = self._install_overview_fixture(order_count=3)
		with patch.object(frappe, "has_permission", side_effect=lambda doctype, permission, doc=None, **kwargs: doc != "SO-001"):
			actual = production.get_production_overview(page_size=0, include_assignment_counts=False)
		self.assertEqual({row["sales_order"] for row in actual["demands"]}, {"SO-000", "SO-002"})
		for call in self.get_all.call_args_list:
			if call.args[0] in ("Work Order", "Stock Reservation Entry"):
				filters = call.kwargs["filters"]
				self.assertEqual(filters.get("sales_order") or filters.get("voucher_no"), ["in", ["SO-000", "SO-002"]])
		documents["SO-001"].check_permission.assert_not_called()
		for name in ("SO-000", "SO-002"):
			documents[name].check_permission.assert_called_once_with("read")

	def test_direct_order_workbench_keeps_original_single_row_query_path(self):
		self._install_overview_fixture(order_count=1)
		actual = workbench.get_order_workbench("SO-000")
		self.assertEqual(len(actual["rows"]), 3)
		self.assertEqual(self._query_counts(), {"work_orders": 3, "reservations": 3, "bom": 3})
		for call in self.get_all.call_args_list:
			if call.args[0] in ("Work Order", "Stock Reservation Entry"):
				self.assertIn("SO-000", call.kwargs["filters"].values())

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
		self.assertEqual(self._query_counts(), {"work_orders": 2, "reservations": 2, "bom": 6})
		self.db.rollback.assert_called_once_with()
		self.db.commit.assert_not_called()
		self.assertEqual(setup.get_default_bom("FG-0"), "BOM-FG-0-AFTER-ROLLBACK")
		self.assertEqual(self.db.get_value.call_count, 7)
