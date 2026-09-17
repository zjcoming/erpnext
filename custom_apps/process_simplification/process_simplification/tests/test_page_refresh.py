from copy import deepcopy
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from process_simplification import page_refresh as refresh


class MemoryCache:
	def __init__(self):
		self.values = {}
		self.hashes = {}
		self.busy = False

	def get_value(self, key, **kwargs):
		return deepcopy(self.values.get(key))

	def set_value(self, key, value, **kwargs):
		self.values[key] = deepcopy(value)

	def hset(self, name, key, value):
		self.hashes.setdefault(name, {})[key] = deepcopy(value)

	def hgetall(self, name):
		return deepcopy(self.hashes.get(name, {}))

	def hdel(self, name, key):
		self.hashes.get(name, {}).pop(key, None)

	def make_key(self, name):
		return "test-site:" + name

	def lock(self, name, **kwargs):
		cache = self

		class Lock:
			def acquire(self, **kwargs):
				if cache.busy:
					return False
				cache.busy = True
				return True

			def release(self):
				cache.busy = False

		return Lock()


class TestPageRefresh(TestCase):
	def setUp(self):
		self.cache = MemoryCache()
		self.scope = {"companies": ["Factory A"], "stamp": "permissions-v1", "topics": list(refresh.TOPICS)}
		self.enterContext(patch.object(refresh, "_cache", return_value=self.cache))
		self.enterContext(patch.object(refresh, "_access", side_effect=lambda user: deepcopy(self.scope)))
		self.enterContext(patch.object(frappe.local, "session", frappe._dict(user="worker-a@test.invalid")))
		self.enterContext(patch.dict(frappe.flags, {"test_page_refresh_cache": True}))
		refresh.clear_changes()

	def tearDown(self):
		refresh.clear_changes()

	def test_snapshot_rejects_guest_and_filters_topics_without_business_queries(self):
		self.scope["topics"] = ["tasks", "notifications"]
		with patch.object(frappe.db, "sql", side_effect=AssertionError("no inventory SQL")):
			first = refresh.check_updates(["tasks", "orders", "notifications", "unknown"])
			self.assertEqual(set(first["versions"]), {"tasks", "notifications"})
			second = refresh.check_updates(["tasks", "notifications"], first["versions"])
			self.assertEqual(second["changed"], [])
		frappe.session.user = "Guest"
		with self.assertRaises(frappe.PermissionError):
			refresh.check_updates(["tasks"])

	def test_worker_change_does_not_invalidate_another_worker(self):
		a = refresh.versions_for("a", ["tasks"])
		b = refresh.versions_for("b", ["tasks"])
		self.cache.set_value(refresh._key("version", "user", "a", "tasks"), "new")
		self.assertNotEqual(a, refresh.versions_for("a", ["tasks"]))
		self.assertEqual(b, refresh.versions_for("b", ["tasks"]))

	def test_batch_master_change_invalidates_inventory_pages_without_exposing_a_batch_name(self):
		before = refresh.versions_for("a", ["orders", "warehouse", "production", "purchase"])
		refresh.document_changed(frappe._dict(doctype="Batch", name="PRIVATE-BATCH", disabled=1))
		self.assertEqual(frappe.local.ps_page_changes, {
			("all", topic) for topic in ("orders", "production", "purchase", "warehouse", "tasks", "dashboard")
		})
		refresh.flush_changes()
		after = refresh.versions_for("a", ["orders", "warehouse", "production", "purchase"])
		self.assertTrue(all(before[topic] != after[topic] for topic in before))

	def test_expiry_day_change_invalidates_versions_without_a_stock_posting(self):
		with patch.object(refresh, "nowdate", return_value="2026-09-14"):
			before = refresh.versions_for("a", ["orders", "warehouse", "production", "purchase"])
		with patch.object(refresh, "nowdate", return_value="2026-09-15"):
			after = refresh.versions_for("a", ["orders", "warehouse", "production", "purchase"])
		self.assertTrue(all(before[topic] != after[topic] for topic in before))

	def test_company_changes_do_not_invalidate_other_companies(self):
		a = {**self.scope, "companies": ["Factory A"]}
		b = {**self.scope, "companies": ["Factory B"]}
		before_a = refresh.versions_for("a", ["orders", "tasks"], a)
		before_b = refresh.versions_for("b", ["orders", "tasks"], b)
		self.cache.set_value(refresh._key("version", "company", "Factory A", "orders"), "changed")
		self.assertNotEqual(before_a, refresh.versions_for("a", ["orders", "tasks"], a))
		self.assertEqual(before_b, refresh.versions_for("b", ["orders", "tasks"], b))

	def test_rollback_discards_hints_and_commit_coalesces_them(self):
		commit, rollback = Mock(), Mock()
		with (
			patch.object(frappe.db, "after_commit", commit),
			patch.object(frappe.db, "after_rollback", rollback),
		):
			for _ in range(25):
				refresh.queue_changes({("company", "Factory A", "orders")})
			commit.add.assert_called_once_with(refresh.flush_changes)
			rollback.add.assert_called_once_with(refresh.clear_changes)
			self.assertEqual(len(frappe.local.ps_page_changes), 1)
			refresh.clear_changes()
			refresh.flush_changes()
			self.assertEqual(self.cache.values, {})
			refresh.queue_changes({("company", "Factory A", "orders")})
			refresh.flush_changes()
			self.assertEqual(len(self.cache.values), 1)

	def test_only_interested_visible_users_receive_content_free_hints(self):
		frappe.session.user = "a"
		refresh.check_updates(["tasks"])
		frappe.session.user = "b"
		refresh.check_updates(["tasks"])
		refresh.queue_changes({("user", "a", "tasks")})
		with patch.object(frappe, "publish_realtime") as publish:
			refresh.flush_changes()
			publish.assert_called_once_with(refresh.EVENT, {"topics": ["tasks"]}, user="a")

	def test_display_cache_is_user_scoped_copied_and_invalidated_by_data_changes(self):
		calculate = Mock(return_value={"rows": [{"qty": 3}]})
		first = refresh.cached_display("orders", calculate, {"company": "Factory A"})
		first["rows"][0]["qty"] = 999
		second = refresh.cached_display("orders", calculate, {"company": "Factory A"})
		self.assertEqual(second["rows"][0]["qty"], 3)
		calculate.assert_called_once()
		frappe.session.user = "worker-b@test.invalid"
		refresh.cached_display("orders", calculate, {"company": "Factory A"})
		self.assertEqual(calculate.call_count, 2)
		self.cache.set_value(refresh._key("version", "company", "Factory A", "orders"), "changed")
		refresh.cached_display("orders", calculate, {"company": "Factory A"})
		self.assertEqual(calculate.call_count, 3)

	def test_concurrent_display_computations_do_not_block_or_duplicate_work(self):
		self.cache.busy = True
		calculate = Mock()
		self.assertEqual(refresh.cached_display("production", calculate, {}), {"_refresh_pending": True})
		calculate.assert_not_called()

	def test_results_computed_during_a_change_are_not_cached_as_current(self):
		def calculate():
			self.cache.set_value(refresh._key("version", "company", "Factory A", "orders"), "changed")
			return {"old": True}

		self.assertEqual(
			refresh.cached_display("orders", calculate, {}), {"old": True, "_refresh_stale": True}
		)
		self.assertFalse(
			any(isinstance(value, dict) and value.get("old") for value in self.cache.values.values())
		)

	def test_permission_change_during_read_discards_the_result(self):
		def calculate():
			self.scope["stamp"] = "revoked"
			return {"sensitive": True}

		self.assertEqual(refresh.cached_display("orders", calculate, {}), {"_refresh_pending": True})

	def test_pagination_reuses_complete_allocation_result(self):
		with (
			patch.object(frappe, "has_permission", return_value=True),
			patch(
				"process_simplification.api.workbench.get_fulfillment_overview",
				return_value={"orders": [{"name": str(i)} for i in range(50)], "summary": {"orders": 50}},
			) as calculate,
		):
			one = refresh.fulfillment_overview(page=1, page_size=20)
			two = refresh.fulfillment_overview(page=2, page_size=20)
			self.assertEqual(one["orders"][0]["name"], "0")
			self.assertEqual(two["orders"][0]["name"], "20")
			calculate.assert_called_once()

	def test_access_version_and_cache_loss_force_resynchronization(self):
		first = refresh.check_updates(["tasks"])
		self.scope["stamp"] = "permissions-v2"
		self.assertEqual(refresh.check_updates(["tasks"], first["versions"])["changed"], ["tasks"])
		second = refresh.check_updates(["tasks"])
		self.cache.values.clear()
		self.assertEqual(refresh.check_updates(["tasks"], second["versions"])["changed"], ["tasks"])

	def test_cached_read_still_checks_current_native_permission(self):
		with (
			patch.object(frappe, "has_permission", side_effect=frappe.PermissionError),
			patch.object(refresh, "cached_display") as cache,
		):
			with self.assertRaises(frappe.PermissionError):
				refresh.fulfillment_overview()
			cache.assert_not_called()

	def test_notification_events_only_target_their_recipient(self):
		refresh.document_changed(frappe._dict(doctype="Notification Log", name="N1", for_user="a"))
		self.assertEqual(frappe.local.ps_page_changes, {("user", "a", "notifications")})

	def test_native_job_card_employee_table_does_not_break_a_business_save(self):
		refresh.document_changed(
			frappe._dict(doctype="Job Card", name="JC", work_order="WO", employee=[{"employee": "EMP"}])
		)
		self.assertIn(("work-order-workers", "WO"), frappe.local.ps_page_changes)

	def test_worker_assignments_coalesce_worker_resolution_for_the_whole_transaction(self):
		with patch.object(refresh, "_worker_users", return_value={"worker-a"}):
			for _ in range(20):
				refresh.document_changed(
					frappe._dict(doctype="Job Card", name="JC", work_order="WO", company="Factory A")
				)
		with patch.object(
			frappe, "get_all", side_effect=[["EMP-A", "EMP-B"], ["worker-a", "worker-b"]]
		) as query:
			refresh.flush_changes()
			self.assertEqual(query.call_count, 2)
		self.assertIsNotNone(self.cache.get_value(refresh._key("version", "user", "worker-b", "tasks")))

	def test_failed_rpc_does_not_publish_change_hints(self):
		with patch.object(refresh, "flush_changes") as flush:
			refresh._after_request(Mock(method="POST"), Mock(status_code=500))
			flush.assert_not_called()
