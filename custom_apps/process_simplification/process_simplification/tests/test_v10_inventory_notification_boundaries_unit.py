"""Bounded follow-up pure tests for seven previously unmapped v1.0 cases.

Input snapshots and notification/report dependencies are mocked. Assertions
cover the approved quantity and status contracts, never real posting or audio.
"""
from contextlib import ExitStack
from datetime import date
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
import sys

import frappe
from process_simplification import notifications
from process_simplification.api import executive_dashboard as dashboard
from process_simplification.api.production_readiness import allocate_work_order_readiness, build_work_order_graph
from process_simplification.production_workflow import service as workflow


def raw_graph(name, qty, delivery):
    return build_work_order_graph(
        {"name": "PP-"+name, "planned_date": delivery, "posting_date": "2026-09-01", "creation": "2026-09-01 08:00:00"},
        [{"name": "WO-"+name, "qty": qty, "production_item": "FG-"+name, "production_plan_item": "PPI-"+name,
          "status": "Not Started", "sales_order": "SO-"+name, "sales_order_item": "SOI-"+name,
          "order_delivery_date": delivery, "order_creation": "2026-09-01 08:00:00", "sales_order_item_idx": 1}],
        [{"parent": "WO-"+name, "name": "WOI-"+name, "item_code": "CASE-RM", "source_warehouse": "CASE-RM-WH", "required_qty": qty, "transferred_qty": 0}],
        [], active_bom_items={"FG-"+name},
    )


class TestV10DecimalStockBoundaries(TestCase):
    def test_V10_ALLOC_008_exact_decimal_demand_must_not_create_a_phantom_purchase_gap(self):
        # Independent arithmetic: Decimal('0.55')+Decimal('54.45')+Decimal('0.55') == 55.55.
        plans = allocate_work_order_readiness([
            raw_graph("1", 0.55, "2026-09-10"), raw_graph("2", 54.45, "2026-09-11"), raw_graph("3", 0.55, "2026-09-12"),
        ], {("CASE-RM", "CASE-RM-WH"): {"actual_qty": 55.55, "available_qty": 55.55}})
        items = [next(iter(p.work_orders_by_name.values())).required_items[0] for p in plans]
        self.assertAlmostEqual(sum(row.available_qty for row in items), 55.55)
        self.assertEqual([row.current_gap_qty for row in items], [0, 0, 0], "exactly covered decimal demand must expose no positive phantom gap")
        self.assertEqual([row.shortage_qty for row in items], [0, 0, 0])
        self.assertEqual({row.status for row in items}, {"ready_now"})

    def test_V10_ALLOC_009_real_one_cent_quantity_gap_is_preserved(self):
        for actual, expected in ((9.99, 0.01), (10, 0)):
            with self.subTest(actual=actual):
                item = allocate_work_order_readiness([raw_graph("MIN", 10, "2026-09-10")], {
                    ("CASE-RM", "CASE-RM-WH"): {"actual_qty": actual, "available_qty": actual},
                })[0].work_orders_by_name["WO-MIN"].required_items[0]
                self.assertAlmostEqual(item.shortage_qty, expected, places=10)
                self.assertEqual(item.status, "new_purchase_required" if expected else "ready_now")


class TestV10WithdrawStateBoundaries(TestCase):
    def test_V10_STOCK_007_submitted_and_cancelled_entries_cannot_use_draft_withdrawal(self):
        for status in (1, 2):
            with self.subTest(docstatus=status):
                doc = frappe._dict(name="STE-CASE", work_order="WO-CASE", company="CASE-A", docstatus=status, custom_process_workflow_action="Material Issue Request")
                fake_db = Mock()
                fake_db.get_value.side_effect = [doc, "WO-CASE", "STE-CASE"]
                with (
                    patch.object(workflow, "require_reviewer"),
                    patch.object(frappe, "db", fake_db, create=True),
                    patch.object(frappe, "get_doc", return_value=doc),
                    patch.object(workflow, "release_issue_request_reservations") as release,
                    patch.object(frappe, "delete_doc") as delete,
                ):
                    with self.assertRaisesRegex(frappe.ValidationError, "库存单已提交或取消"):
                        workflow.withdraw_stock_request(doc.name)
                    release.assert_not_called()
                    delete.assert_not_called()


class TestV10StockNotificationSemantics(TestCase):
    @staticmethod
    def doc():
        return frappe._dict(name="STE-CASE", work_order="WO-CASE", company="CASE-A", custom_process_workflow_action="Material Issue Request")

    def test_V10_NOTICE_002_issue_request_is_pending_posting_and_links_exact_draft(self):
        with patch.object(notifications, "responsibility_recipients", return_value=["warehouse-a@example.com"]) as recipients, patch.object(notifications, "notify_users") as notify:
            notifications.notify_production_stock_request(self.doc(), request_type="issue")
            recipients.assert_called_once_with("CASE-A", notifications.WAREHOUSE_RESPONSIBILITY)
            self.assertEqual(notify.call_args.args[0], ["warehouse-a@example.com"])
            payload = notify.call_args.kwargs
            self.assertIn("待库房处理", payload["subject"])
            self.assertIn("生产发料", payload["subject"])
            self.assertIn("提交前不会变化", payload["description"])
            self.assertEqual(payload["document_name"], "STE-CASE")
            self.assertEqual(payload["link"], "/app/stock-entry/STE-CASE")

    def test_V10_NOTICE_002_receipt_request_does_not_claim_finished_stock_exists(self):
        with patch.object(notifications, "responsibility_recipients", return_value=["warehouse-a@example.com"]), patch.object(notifications, "notify_users") as notify:
            notifications.notify_production_stock_request(self.doc(), request_type="receipt")
            payload = notify.call_args.kwargs
            self.assertIn("待库房处理", payload["subject"])
            self.assertIn("完工入库", payload["subject"])
            self.assertIn("等待库房复核并提交", payload["description"])
            self.assertIn("提交前不会变化", payload["description"])

    def test_V10_NOTICE_003_partial_net_issue_still_requires_material_and_cannot_claim_dispatch(self):
        # A stale gross header says full issue; current row-level remaining=4 must win.
        work_order = frappe._dict(name="WO-CASE", qty=10, material_transferred_for_manufacturing=10)
        with patch.object(frappe, "get_doc", return_value=work_order), patch.object(workflow, "remaining_material_issue_qty", return_value=4), patch.object(notifications, "responsibility_recipients", return_value=["production-a@example.com"]), patch.object(notifications, "notify_users") as notify:
            notifications.notify_production_stock_completed(self.doc())
            payload = notify.call_args.kwargs
            self.assertIn("仍待补料", payload["subject"])
            self.assertNotIn("可派工", payload["subject"])
            self.assertIn("补齐前不会开放正式派工", payload["description"])
            self.assertEqual(payload["link"], "/app/production-workbench")

    def test_V10_NOTICE_003_complete_net_issue_notifies_correct_company_production_route(self):
        with patch.object(frappe, "get_doc", return_value=frappe._dict(name="WO-CASE")), patch.object(workflow, "remaining_material_issue_qty", return_value=0), patch.object(notifications, "responsibility_recipients", return_value=["production-a@example.com"]) as recipients, patch.object(notifications, "notify_users") as notify:
            notifications.notify_production_stock_completed(self.doc())
            recipients.assert_called_once_with("CASE-A", notifications.PRODUCTION_DISPATCH_RESPONSIBILITY)
            self.assertEqual(notify.call_args.args[0], ["production-a@example.com"])
            self.assertIn("发料已完成，可派工", notify.call_args.kwargs["subject"])
            self.assertEqual(notify.call_args.kwargs["link"], "/app/production-workbench")


class TestV10ReportFailureBoundaries(TestCase):
    def test_V10_DASH_006_profit_preserves_report_total_and_explicitly_includes_returns(self):
        execute = Mock(return_value=([], [frappe._dict(selling_amount=80, gross_profit=20, **{"gross_profit_%": 25})]))
        with patch.dict(sys.modules, {"erpnext.accounts.report.gross_profit.gross_profit": SimpleNamespace(execute=execute)}):
            result = dashboard._gross_profit("CASE-A", date(2026, 8, 1), date(2026, 8, 31))
        self.assertEqual(result, {"available": True, "invoiced_net_sales": 80, "gross_profit": 20, "gross_margin_percent": 25})
        self.assertEqual(execute.call_args.args[0]["include_returned_invoices"], 1)
        self.assertEqual(execute.call_args.args[0]["company"], "CASE-A")

    def test_V10_DASH_006_report_failure_is_unavailable_with_explanation_not_real_zero_profit(self):
        execute = Mock(side_effect=RuntimeError("controlled report failure"))
        with patch.dict(sys.modules, {"erpnext.accounts.report.gross_profit.gross_profit": SimpleNamespace(execute=execute)}), patch.object(frappe, "logger") as logger:
            result = dashboard._gross_profit("CASE-A", date(2026, 8, 1), date(2026, 8, 31))
        self.assertIs(result["available"], False)
        self.assertTrue(result["message"])
        logger.return_value.exception.assert_called_once()

    def test_V10_DASH_008_ageing_failure_is_unavailable_and_explained(self):
        execute = Mock(side_effect=RuntimeError("controlled ageing failure"))
        with patch.dict(sys.modules, {"erpnext.stock.report.stock_ageing.stock_ageing": SimpleNamespace(execute=execute)}), patch.object(frappe, "logger"):
            result = dashboard._stock_ageing("CASE-A", date(2026, 9, 3))
        self.assertIs(result["available"], False)
        self.assertTrue(result["message"])

    def test_V10_DASH_008_current_ageing_date_is_independent_of_order_reporting_period(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(dashboard, "require_owner_access"))
            stack.enter_context(patch.object(dashboard, "_companies", return_value=[frappe._dict(name="CASE-A", default_currency="CNY")]))
            stack.enter_context(patch.object(dashboard, "_order_totals", side_effect=lambda *args: {"order_count": 0, "order_amount": 0}))
            for name in ("_order_trend", "_gross_profit", "_inventory_summary", "_order_health", "_overdue_orders"):
                stack.enter_context(patch.object(dashboard, name, return_value={}))
            stack.enter_context(patch.object(dashboard, "today", return_value="2026-09-03"))
            ageing = stack.enter_context(patch.object(dashboard, "_stock_ageing", return_value={"available": False, "message": "controlled failure"}))
            result = dashboard.get_dashboard(company="CASE-A", from_date="2026-01-01", to_date="2026-01-31")
        self.assertEqual(result["period"], {"from_date": "2026-01-01", "to_date": "2026-01-31"})
        ageing.assert_called_once_with("CASE-A", date(2026, 9, 3))
        self.assertIs(result["stock_ageing"]["available"], False)
        self.assertIn("orders", result, "ageing failure must not destroy independent order metrics")
