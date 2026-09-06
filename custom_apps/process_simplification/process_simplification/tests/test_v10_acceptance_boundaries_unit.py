"""Additional v1.0 acceptance assertions, 2026-09-03.

These are isolated unit tests. No database connection, business document write,
Redis request, browser interaction, or acceptance approval is implied. Case IDs
are in every method name. Rule-pending rush choices are tested only for approved
conservation invariants, without deciding who should win the remaining stock.
"""
from copy import deepcopy
from datetime import date
from itertools import permutations
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from process_simplification.api import executive_dashboard as dashboard
from process_simplification.api import quick_order
from process_simplification.api.production_readiness import (
    allocate_work_order_readiness,
    build_work_order_graph,
)
from process_simplification.api.utils import SimplifiedFlowError
from process_simplification.api.workbench import allocate_finished_stock


class TestV10QuickOrderInputBoundaries(TestCase):
    def setUp(self):
        self.enterContext(patch.object(quick_order, "nowdate", return_value="2026-09-03"))

    @staticmethod
    def payload():
        return {
            "customer": "CASE-CUSTOMER", "delivery_date": "2026-09-03",
            "po_no": "CASE-PO", "remarks": "同批交货",
            "items": [{"item_code": "CASE-FG", "qty": 10, "rate": 25}],
        }

    def test_V10_SO_004_missing_customer_is_a_named_blocker(self):
        for value in (None, "", "   "):
            with self.subTest(customer=value):
                payload = self.payload()
                payload["customer"] = value
                with self.assertRaisesRegex(SimplifiedFlowError, "客户不能为空"):
                    quick_order.normalize_quick_order_payload(payload)

    def test_V10_SO_004_empty_lines_are_a_named_blocker(self):
        for value in (None, [], "[]"):
            with self.subTest(items=value):
                payload = self.payload()
                payload["items"] = value
                with self.assertRaisesRegex(SimplifiedFlowError, "至少添加一个产品"):
                    quick_order.normalize_quick_order_payload(payload)

    def test_V10_SO_004_missing_product_reports_the_correct_line(self):
        payload = self.payload()
        payload["items"].append({"item_code": " ", "qty": 1, "rate": 1})
        before = deepcopy(payload)
        with self.assertRaisesRegex(SimplifiedFlowError, "第 2 行产品不能为空"):
            quick_order.normalize_quick_order_payload(payload)
        self.assertEqual(payload, before, "invalid line must not erase other entered values")

    def test_V10_SO_005_zero_negative_and_non_numeric_quantity_are_rejected(self):
        for value in (0, "0", -1, "-0.01", "not-a-number", None, ""):
            with self.subTest(qty=value):
                payload = self.payload()
                payload["items"][0]["qty"] = value
                with self.assertRaisesRegex(SimplifiedFlowError, "第 1 行数量必须大于 0"):
                    quick_order.normalize_quick_order_payload(payload)

    def test_V10_SO_005_non_finite_quantities_are_rejected(self):
        # A non-finite quantity cannot represent a count or inventory movement.
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(qty=value):
                payload = self.payload()
                payload["items"][0]["qty"] = value
                with self.assertRaises(SimplifiedFlowError):
                    quick_order.normalize_quick_order_payload(payload)

    def test_V10_SO_006_zero_negative_prices_are_rejected(self):
        for value in (0, "0", -0.01, "-0.01"):
            with self.subTest(rate=value):
                payload = self.payload()
                payload["items"][0]["rate"] = value
                with self.assertRaisesRegex(SimplifiedFlowError, "第 1 行成交单价必须大于 0"):
                    quick_order.normalize_quick_order_payload(payload)

    def test_V10_SO_006_minimum_positive_price_is_preserved(self):
        payload = self.payload()
        payload["items"][0]["rate"] = "0.01"
        result = quick_order.normalize_quick_order_payload(payload)
        row = result.get("items")[0]
        self.assertEqual(row["qty"], 10)
        self.assertEqual(row["rate"], 0.01)
        self.assertAlmostEqual(row["qty"] * row["rate"], 0.10)

    def test_V10_SO_006_non_finite_prices_are_rejected(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(rate=value):
                payload = self.payload()
                payload["items"][0]["rate"] = value
                with self.assertRaises(SimplifiedFlowError):
                    quick_order.normalize_quick_order_payload(payload)

    def test_V10_SO_007_yesterday_rejected_today_and_tomorrow_allowed(self):
        payload = self.payload()
        payload["delivery_date"] = "2026-09-02"
        with self.assertRaisesRegex(SimplifiedFlowError, "不能早于今天"):
            quick_order.normalize_quick_order_payload(payload)
        for value in ("2026-09-03", "2026-09-04"):
            with self.subTest(delivery_date=value):
                payload["delivery_date"] = value
                self.assertEqual(quick_order.normalize_quick_order_payload(payload).delivery_date, value)

    def test_V10_SO_007_malformed_and_impossible_dates_are_named_blockers(self):
        for value in ("not-a-date", "2026-02-30", "2026-13-01"):
            with self.subTest(delivery_date=value):
                payload = self.payload()
                payload["delivery_date"] = value
                with self.assertRaisesRegex(SimplifiedFlowError, "交付日期格式不正确"):
                    quick_order.normalize_quick_order_payload(payload)


class _ExpiringCache:
    """Clock-controlled cache double; it never contacts Redis."""
    def __init__(self):
        self.now = 1000
        self.entries = {}
        self.last_ttl = None

    def set_value(self, key, value, *, expires_in_sec, shared):
        if not shared:
            raise AssertionError("review context must use the same shared cache namespace")
        self.last_ttl = expires_in_sec
        self.entries[key] = (deepcopy(value), self.now + expires_in_sec)

    def get_value(self, key, *, shared):
        if not shared:
            raise AssertionError("review context lookup must use shared namespace")
        record = self.entries.get(key)
        if record is None or self.now >= record[1]:
            return None
        return deepcopy(record[0])


class TestV10ReviewTokenBoundaries(TestCase):
    def setUp(self):
        self.cache = _ExpiringCache()
        self.session = frappe._dict(user="case-sales-a@example.com")
        self.enterContext(patch.object(frappe, "cache", self.cache))
        self.enterContext(patch.object(frappe, "session", self.session))
        self.enterContext(patch.object(frappe, "generate_hash", return_value="case-review-token"))
        self.enterContext(patch.object(quick_order, "nowdate", return_value="2026-09-03"))

    def issue(self, payload=None):
        normalized = quick_order.normalize_quick_order_payload(payload or TestV10QuickOrderInputBoundaries.payload())
        return quick_order._issue_review_token({
            "intent_digest": quick_order._quick_order_intent_digest(normalized),
            "review_fingerprint": "reviewed-stock-and-commercial-facts",
        })

    def test_V10_SO_016_review_token_is_valid_only_before_cache_expiry(self):
        token = self.issue()
        self.assertGreater(self.cache.last_ttl, 0)
        self.cache.now += self.cache.last_ttl - 1
        self.assertEqual(quick_order._get_review_token(token).user, self.session.user)
        self.cache.now += 1
        with self.assertRaisesRegex(SimplifiedFlowError, "已过期"):
            quick_order._get_review_token(token)

    def test_V10_SO_016_cross_user_review_context_is_rejected(self):
        token = self.issue()
        self.session.user = "case-sales-b@example.com"
        with self.assertRaisesRegex(SimplifiedFlowError, "不属于当前用户"):
            quick_order._get_review_token(token)

    def test_V10_SO_016_missing_and_unknown_tokens_do_not_create_context(self):
        for value in ("", "unknown-token"):
            with self.subTest(token=value):
                with self.assertRaises(SimplifiedFlowError):
                    quick_order._get_review_token(value)
        self.assertEqual(self.cache.entries, {})

    def test_V10_SO_016_token_keeps_reviewed_intent_and_facts_separate(self):
        token = self.issue()
        stored = quick_order._get_review_token(token)
        self.assertEqual(stored.review_fingerprint, "reviewed-stock-and-commercial-facts")
        self.assertEqual(stored.user, "case-sales-a@example.com")
        self.assertEqual(len(stored.intent_digest), 64)

    def test_V10_SO_013_changed_order_intent_is_rejected_before_any_write(self):
        original = TestV10QuickOrderInputBoundaries.payload()
        token = self.issue(original)
        variants = []
        for field, value in (("customer", "OTHER-CUSTOMER"), ("delivery_date", "2026-09-04"), ("po_no", "OTHER-PO"), ("remarks", "改为分批")):
            payload = deepcopy(original)
            payload[field] = value
            variants.append((field, payload))
        for field, value in (("qty", 11), ("rate", 26), ("item_code", "OTHER-FG")):
            payload = deepcopy(original)
            payload["items"][0][field] = value
            variants.append((field, payload))
        with (
            patch.object(frappe, "has_permission", return_value=True),
            patch.object(quick_order, "_evaluate_quick_order") as evaluate,
            patch.object(quick_order, "_create_idempotency_record") as write_record,
        ):
            for field, payload in variants:
                with self.subTest(changed_field=field):
                    with self.assertRaisesRegex(SimplifiedFlowError, "订单内容已修改"):
                        quick_order.submit_quick_sales_order(payload, review_token=token, idempotency_key="same-request")
            evaluate.assert_not_called()
            write_record.assert_not_called()


def _finished_row(name, pending, free, reserved=0, delivery="2026-09-20", creation="2026-09-01 08:00:00"):
    return {
        "company": "CASE-A", "item_code": "CASE-FG", "warehouse": "CASE-FG-WH",
        "sales_order": "SO-" + name, "sales_order_item": "SOI-" + name,
        "delivery_date": delivery, "order_creation": creation,
        "pending_qty": pending, "reserved_qty": reserved,
        "available_stock_snapshot_qty": free, "active_work_order_qty": 0,
    }


def _raw_graph(name, required, issued=0, reserved=0, delivery="2026-09-20"):
    return build_work_order_graph(
        {"name": "PP-" + name, "planned_date": delivery, "posting_date": "2026-09-01", "creation": "2026-09-01 08:00:00"},
        [{"name": "WO-" + name, "qty": required, "production_item": "FG-" + name,
          "production_plan_item": "PPI-" + name, "status": "Not Started",
          "sales_order": "SO-" + name, "sales_order_item": "SOI-" + name,
          "order_delivery_date": delivery, "order_creation": "2026-09-01 08:00:00", "sales_order_item_idx": 1}],
        [{"parent": "WO-" + name, "name": "WOI-" + name, "item_code": "CASE-RM", "source_warehouse": "CASE-RM-WH",
          "required_qty": required, "transferred_qty": issued, "stock_reserved_qty": reserved}],
        [], active_bom_items={"FG-" + name},
    )


class TestV10RushConservation(TestCase):
    def test_V10_RUSH_001_new_urgent_order_reallocates_only_free_finished_stock(self):
        original = _finished_row("OLD", 10, 10)
        before = allocate_finished_stock([original])
        self.assertEqual(before[0].finished_stock_coverage_qty, 10)
        urgent = _finished_row("URGENT", 6, 10, delivery="2026-09-10")
        rows = {r.sales_order_item: r for r in allocate_finished_stock([original, urgent])}
        self.assertEqual(rows["SOI-URGENT"].finished_stock_coverage_qty, 6)
        self.assertEqual(rows["SOI-OLD"].finished_stock_coverage_qty, 4)
        self.assertEqual(rows["SOI-OLD"].production_required_qty, 6)
        self.assertEqual(sum(r.finished_stock_coverage_qty for r in rows.values()), 10)
        self.assertEqual(sum(r.reserved_qty for r in rows.values()), 0)
        self.assertEqual(original["available_stock_snapshot_qty"], 10)

    def test_V10_RUSH_002_urgent_order_cannot_take_existing_actual_reservation(self):
        rows = {r.sales_order_item: r for r in allocate_finished_stock([
            _finished_row("OLD", 8, 2, reserved=8),
            _finished_row("URGENT", 6, 2, delivery="2026-09-10"),
        ])}
        self.assertEqual(rows["SOI-OLD"].reserved_qty, 8)
        self.assertEqual(rows["SOI-OLD"].finished_stock_coverage_qty, 8)
        self.assertEqual(rows["SOI-URGENT"].available_to_reserve, 2)
        self.assertEqual(rows["SOI-URGENT"].production_required_qty, 4)
        self.assertEqual(sum(r.finished_stock_coverage_qty for r in rows.values()), 10)

    def test_V10_RUSH_005_exact_original_work_order_reservation_is_protected(self):
        plans = allocate_work_order_readiness([
            _raw_graph("OLD", 70, reserved=70),
            _raw_graph("URGENT", 50, delivery="2026-09-10"),
        ], {("CASE-RM", "CASE-RM-WH"): {"actual_qty": 100, "available_qty": 100, "production_committed_qty": 70, "free_qty": 30}})
        materials = {p.name: next(iter(p.work_orders_by_name.values())).required_items[0] for p in plans}
        self.assertEqual(materials["PP-OLD"].available_qty, 70)
        self.assertEqual(materials["PP-URGENT"].available_qty, 30)
        self.assertEqual(materials["PP-URGENT"].shortage_qty, 20)
        self.assertEqual(sum(m.available_qty for m in materials.values()), 100)

    def test_V10_RUSH_007_issued_wip_is_not_recounted_as_source_stock(self):
        plans = allocate_work_order_readiness([
            _raw_graph("OLD", 60, issued=60),
            _raw_graph("URGENT", 60, delivery="2026-09-10"),
        ], {("CASE-RM", "CASE-RM-WH"): {"actual_qty": 40, "available_qty": 40}})
        materials = {p.name: next(iter(p.work_orders_by_name.values())).required_items[0] for p in plans}
        self.assertEqual(materials["PP-OLD"].net_transferred_qty, 60)
        self.assertEqual(materials["PP-OLD"].required_qty, 0)
        self.assertEqual(materials["PP-URGENT"].available_qty, 40)
        self.assertEqual(materials["PP-URGENT"].shortage_qty, 20)
        self.assertEqual(sum(m.available_qty for m in materials.values()), 40)

    def test_V10_RUSH_008_partial_issue_conservation_without_choosing_pending_priority_policy(self):
        # Only the invariant is approved: 30 already in OLD WIP cannot be taken
        # back, and only the 70 remaining at source can be allocated once.
        for order in permutations([
            _raw_graph("OLD", 80, issued=30),
            _raw_graph("URGENT", 60, delivery="2026-09-10"),
        ]):
            with self.subTest(input_order=[p.name for p in order]):
                plans = allocate_work_order_readiness(order, {("CASE-RM", "CASE-RM-WH"): {"actual_qty": 70, "available_qty": 70}})
                materials = {p.name: next(iter(p.work_orders_by_name.values())).required_items[0] for p in plans}
                self.assertEqual(materials["PP-OLD"].net_transferred_qty, 30)
                self.assertEqual(materials["PP-OLD"].required_qty, 50)
                self.assertEqual(sum(m.available_qty for m in materials.values()), 70)
                self.assertEqual(sum(m.shortage_qty for m in materials.values()), 40)
                self.assertEqual(30 + sum(m.available_qty for m in materials.values()), 100)

    def test_V10_RUSH_020_equal_delivery_is_stable_across_input_order(self):
        original = _finished_row("OLD", 6, 10, creation="2026-09-01 08:00:00")
        urgent = _finished_row("URGENT", 6, 10, creation="2026-09-01 09:00:00")
        for sequence in permutations([original, urgent]):
            with self.subTest(input_order=[r["sales_order"] for r in sequence]):
                rows = {r.sales_order_item: r for r in allocate_finished_stock(sequence)}
                self.assertEqual(rows["SOI-OLD"].finished_stock_coverage_qty, 6)
                self.assertEqual(rows["SOI-URGENT"].finished_stock_coverage_qty, 4)
                self.assertEqual(rows["SOI-URGENT"].production_required_qty, 2)
                self.assertEqual(sum(r.finished_stock_coverage_qty for r in rows.values()), 10)

    def test_V10_ALLOC_001_other_company_item_or_warehouse_cannot_fill_the_target_gap(self):
        target = _finished_row("TARGET", 15, 10)
        different_warehouse = {**_finished_row("WH2", 7, 7), "warehouse": "OTHER-WH"}
        different_item = {**_finished_row("ITEM2", 9, 9), "item_code": "OTHER-FG"}
        different_company = {**_finished_row("COMPANY2", 12, 12), "company": "OTHER-COMPANY"}
        rows = {r.sales_order_item: r for r in allocate_finished_stock([target, different_warehouse, different_item, different_company])}
        self.assertEqual(rows["SOI-TARGET"].finished_stock_coverage_qty, 10)
        self.assertEqual(rows["SOI-TARGET"].production_required_qty, 5)
        self.assertEqual(rows["SOI-WH2"].finished_stock_coverage_qty, 7)
        self.assertEqual(rows["SOI-ITEM2"].finished_stock_coverage_qty, 9)
        self.assertEqual(rows["SOI-COMPANY2"].finished_stock_coverage_qty, 12)


class TestV10DashboardDateBoundaries(TestCase):
    def test_V10_DASH_003_inclusive_366_days_allowed_and_367_rejected(self):
        self.assertEqual(dashboard.normalize_period("2024-01-01", "2024-12-31"), (date(2024, 1, 1), date(2024, 12, 31)))
        with self.assertRaises(frappe.ValidationError):
            dashboard.normalize_period("2024-01-01", "2025-01-01")

    def test_V10_DASH_003_single_day_leap_day_and_reversed_dates(self):
        self.assertEqual(dashboard.normalize_period("2024-02-29", "2024-02-29"), (date(2024, 2, 29), date(2024, 2, 29)))
        with self.assertRaises(frappe.ValidationError):
            dashboard.normalize_period("2026-09-03", "2026-09-02")

    def test_V10_DASH_003_invalid_date_is_not_silently_replaced(self):
        for value in ("not-a-date", "2026-02-30", "2026-13-01"):
            with self.subTest(from_date=value):
                with self.assertRaises((ValueError, frappe.ValidationError)):
                    dashboard.normalize_period(value, "2026-09-03")

    def test_V10_DASH_003_previous_period_is_contiguous_and_same_length(self):
        examples = [
            (date(2024, 2, 1), date(2024, 2, 29), date(2024, 1, 3), date(2024, 1, 31)),
            (date(2026, 1, 1), date(2026, 1, 1), date(2025, 12, 31), date(2025, 12, 31)),
            (date(2026, 1, 1), date(2026, 1, 31), date(2025, 12, 1), date(2025, 12, 31)),
        ]
        for current_from, current_to, previous_from, previous_to in examples:
            with self.subTest(current_from=current_from, current_to=current_to):
                self.assertEqual(dashboard.previous_period(current_from, current_to), (previous_from, previous_to))
                self.assertEqual((current_to-current_from).days, (previous_to-previous_from).days)
                self.assertEqual((current_from-previous_to).days, 1)

    def test_V10_DASH_004_zero_comparison_is_unavailable_and_decline_keeps_sign(self):
        for current in (0, 100, -100, None):
            with self.subTest(current=current):
                self.assertIsNone(dashboard.percentage_change(current, 0))
        self.assertEqual(dashboard.percentage_change(75, 100), -25.0)
        self.assertEqual(dashboard.percentage_change(125, 100), 25.0)
        self.assertEqual(dashboard.percentage_change(0, 100), -100.0)

    def test_V10_DASH_004_six_month_trend_fills_missing_months_across_year_boundary(self):
        fake_db = Mock()
        fake_db.sql.return_value = [
            frappe._dict(month_key="2025-09", order_count=2, order_amount=12.30),
            frappe._dict(month_key="2026-01", order_count=1, order_amount=5),
        ]
        with patch.object(frappe, "db", fake_db, create=True):
            rows = dashboard._order_trend("CASE-COMPANY", date(2026, 1, 15))
        self.assertEqual([r["month"] for r in rows], ["2025-08", "2025-09", "2025-10", "2025-11", "2025-12", "2026-01"])
        self.assertEqual([r["order_count"] for r in rows], [0, 2, 0, 0, 0, 1])
        self.assertEqual([r["order_amount"] for r in rows], [0, 12.30, 0, 0, 0, 5])
        self.assertEqual(fake_db.sql.call_args.args[1], {"company": "CASE-COMPANY", "from_date": date(2025, 8, 1), "to_date": date(2026, 1, 15)})

    def test_V10_DASH_004_empty_six_month_trend_returns_zero_rows_without_gaps(self):
        fake_db = Mock()
        fake_db.sql.return_value = []
        with patch.object(frappe, "db", fake_db, create=True):
            rows = dashboard._order_trend("CASE-COMPANY", date(2026, 1, 15))
        self.assertEqual(len(rows), 6)
        self.assertEqual(sum(r["order_count"] for r in rows), 0)
        self.assertEqual(sum(r["order_amount"] for r in rows), 0)
