# Order Fulfillment Overview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-order-first workbench landing view with an owner-focused, delivery-date-prioritized overview of every unfinished Sales Order while preserving the existing Sales Order Item actions.

**Architecture:** Add a read-only aggregation API in the custom app that reuses `get_order_workbench` for each readable unfinished Sales Order, derives order-level KPIs/risk/sort keys, and returns order rows plus their existing item rows. Rebuild the Frappe Page as a client-filtered expandable order list; item actions continue to call the current reservation, production, and Delivery Note APIs, which remain responsible for authoritative write-time validation.

**Tech Stack:** ERPNext/Frappe 17, Python, Frappe ORM, JavaScript/jQuery Frappe Page API, CSS, Frappe `UnitTestCase`, Node built-in test runner.

## Global Constraints

- Work only on `rc/develop`; do not commit feature work to `develop`.
- Do not modify ERPNext core files.
- ERPNext standard documents remain the only source of truth; do not add a fulfillment ledger or cached status DocType.
- Overview loading is read-only and must not explode every BOM or create operational documents.
- Existing server-side permission and write-time coverage checks remain mandatory for every simplified action.
- Direct-stock orders remain visible because they still require delivery fulfillment.
- Default ordering is earliest pending delivery date ascending, then risk severity descending, then Sales Order creation ascending.
- UI copy is Chinese and the layout must remain usable on narrow screens.

---

## File Structure

- Modify `custom_apps/process_simplification/process_simplification/api/workbench.py`: own order discovery, order-level aggregation, KPI calculation, risk classification and deterministic sorting.
- Modify `custom_apps/process_simplification/process_simplification/process_simplification/page/order_workbench/order_workbench.js`: render KPI cards, filters, expandable Sales Order rows and existing item actions.
- Modify `custom_apps/process_simplification/process_simplification/public/css/process_simplification.css`: style the overview, risk badges, expansion table and responsive behavior.
- Modify `custom_apps/process_simplification/process_simplification/tests/test_simplified_flow.py`: unit-test aggregation, risk priority, direct-stock inclusion and sorting.
- Create `custom_apps/process_simplification/process_simplification/tests/js/order_fulfillment_overview.test.js`: test pure filtering, counters and HTML escaping/view helpers.
- Modify `custom_apps/process_simplification/README.md`: document the new page responsibility and its relationship to the future production workbench.
- Modify `openspec/changes/simplify-manufacturing-flow/specs/order-fulfillment-workbench/spec.md`: record the approved cross-order overview requirements.

### Task 1: Add the cross-order aggregation API

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/api/workbench.py`
- Test: `custom_apps/process_simplification/process_simplification/tests/test_simplified_flow.py`

**Interfaces:**
- Consumes: `get_order_workbench(sales_order: str) -> dict` and readable submitted Sales Orders.
- Produces: `build_fulfillment_order(order, rows, today=None) -> dict`.
- Produces: `get_fulfillment_overview() -> {checked_at, summary, orders}`.

- [ ] **Step 1: Write failing aggregation and sorting tests**

Add tests using representative `frappe._dict` rows:

```python
def test_direct_stock_order_is_included_and_marked_ready_to_ship(self):
	order = frappe._dict(name="SO-READY", customer="C1", customer_name="C1", creation="2026-08-01")
	rows = [{"pending_qty": 10, "reserved_qty": 10, "uncovered_qty": 0, "active_work_order_qty": 0,
		"delivered_qty": 0, "order_qty": 10, "delivery_date": "2026-08-06", "next_actions": []}]

	result = build_fulfillment_order(order, rows, today="2026-08-02")

	self.assertEqual(result["status_code"], "ready_to_ship")
	self.assertTrue(result["direct_ship"])
```

Cover mixed item states, earliest pending item date, missing date, overdue priority, same-date risk ordering and KPI counts by order rather than item.

- [ ] **Step 2: Run the focused module and verify failure**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_simplified_flow
```

Expected: failure because overview aggregation functions do not exist.

- [ ] **Step 3: Implement deterministic order aggregation**

`build_fulfillment_order` must return stable fields:

```python
{
	"name": str,
	"customer": str,
	"customer_name": str,
	"transaction_date": str | None,
	"delivery_date": str | None,
	"has_multiple_delivery_dates": bool,
	"item_count": int,
	"order_qty": float,
	"delivered_qty": float,
	"pending_qty": float,
	"reserved_qty": float,
	"active_work_order_qty": float,
	"completed_qty": float,
	"uncovered_qty": float,
	"delivery_timing": "overdue" | "today" | "within_7_days" | "later" | "missing",
	"days_to_delivery": int | None,
	"status_code": str,
	"status_label": str,
	"risk_level": "red" | "orange" | "blue" | "green" | "gray",
	"risk_score": int,
	"risk_label": str,
	"direct_ship": bool,
	"needs_production": bool,
	"rows": list[dict],
}
```

Only pending rows participate in earliest-date and fulfillment calculations. Clamp each reserved contribution to that row's pending quantity. Treat `create_work_order` in `next_actions` or positive active production as production demand. Never treat completed-but-unreserved output as deliverable stock.

- [ ] **Step 4: Implement the whitelisted overview endpoint**

Call `frappe.has_permission("Sales Order", "read", throw=True)`, then use `frappe.get_list` so user permissions remain effective. Select submitted, not-closed, not-completed, not-fully-delivered orders. Recalculate each order through `get_order_workbench`; discard any whose rows now have zero pending quantity. Return summary counts for total, overdue, due within seven days, needs production and direct ship, plus `now_datetime()` as `checked_at`.

- [ ] **Step 5: Run tests and commit**

```bash
git add custom_apps/process_simplification/process_simplification/api/workbench.py \
  custom_apps/process_simplification/process_simplification/tests/test_simplified_flow.py
git commit -m "feat: aggregate unfinished order fulfillment"
```

### Task 2: Build the owner-facing fulfillment overview page

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/process_simplification/page/order_workbench/order_workbench.js`
- Modify: `custom_apps/process_simplification/process_simplification/public/css/process_simplification.css`
- Create: `custom_apps/process_simplification/process_simplification/tests/js/order_fulfillment_overview.test.js`

**Interfaces:**
- Consumes: `get_fulfillment_overview()` response from Task 1.
- Produces: pure helpers `filterFulfillmentOrders(orders, filters)`, `overviewSummary(orders)`, and `orderOverviewHtml(order, helpers)` for Node tests.
- Preserves: existing action names `reserve_stock`, `create_work_order`, `reserve_completed_stock`, `create_delivery_note`, `view_sales_order`, and `view_work_orders`.

- [ ] **Step 1: Write failing Node tests**

```javascript
test("risk-only filtering keeps overdue and blocked orders", () => {
	const visible = filterFulfillmentOrders(fixture, { riskOnly: true });
	assert.deepEqual(visible.map((row) => row.name), ["SO-OVERDUE", "SO-BLOCKED"]);
});

test("order HTML escapes customer and item labels", () => {
	const html = orderOverviewHtml(unsafeOrder, helpers);
	assert.doesNotMatch(html, /<img/);
	assert.match(html, /&lt;img/);
});
```

Also cover search by order/customer/product, delivery window filtering, status filtering and recalculated visible counters.

- [ ] **Step 2: Run Node tests and verify failure**

```bash
node --test custom_apps/process_simplification/process_simplification/tests/js/order_fulfillment_overview.test.js
```

Expected: failure because the overview helpers are not exported.

- [ ] **Step 3: Replace the single-order landing UI**

Render the five KPI cards, one-line filters, default sort explanation and expandable order rows. The collapsed row shows Sales Order/customer, earliest delivery date, delivered/order quantity, reserved/pending coverage, active production/production need, highest risk and a “查看并处理” toggle. Expanded product rows show the current item metrics and actions.

If the route is `/app/order-workbench/<sales-order>`, prefill search with that Sales Order and expand it. This preserves Quick Sales Order's existing redirect while making the new overview the default landing page.

- [ ] **Step 4: Preserve actions and refresh from standard documents**

Reuse the current action-to-method map. After any write action, reload the entire overview. `view_work_orders` continues to fetch details; `view_sales_order` routes to the standard form. The order-level material-check action passes all supported pending item identities through `frappe.route_options.selected_rows` and routes to `shortage-purchase-planning`.

- [ ] **Step 5: Add export and responsive styling**

Export only the currently visible order summaries as UTF-8 CSV using the browser Blob API. Add CSS for KPI cards, filters, risk-colored order rows, numeric alignment, details expansion, horizontal item-table scrolling and narrow-screen stacking. Do not hide fulfillment facts on narrow screens.

- [ ] **Step 6: Run frontend tests, format and commit**

```bash
node --test custom_apps/process_simplification/process_simplification/tests/js/order_fulfillment_overview.test.js
git diff --check
git add custom_apps/process_simplification/process_simplification/process_simplification/page/order_workbench/order_workbench.js \
  custom_apps/process_simplification/process_simplification/public/css/process_simplification.css \
  custom_apps/process_simplification/process_simplification/tests/js/order_fulfillment_overview.test.js
git commit -m "feat: add owner order fulfillment overview"
```

### Task 3: Document, validate and exercise the end-to-end flow

**Files:**
- Modify: `custom_apps/process_simplification/README.md`
- Modify: `openspec/changes/simplify-manufacturing-flow/specs/order-fulfillment-workbench/spec.md`

**Interfaces:**
- Consumes: final API and page behavior.
- Produces: durable requirements and verification evidence.

- [ ] **Step 1: Extend the OpenSpec requirement**

Add scenarios for all unfinished orders, date/risk sorting, direct-stock inclusion, mixed item states, route focus, permission-aware discovery and recalculation after actions. Keep production scheduling and automatic procurement out of scope.

- [ ] **Step 2: Update the app README**

Explain that “订单履约总览” covers sales-to-delivery risk, while a future production workbench covers only production execution. Document that overview loading is read-only and that item actions still create standard ERPNext documents.

- [ ] **Step 3: Run full verification**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification
openspec validate simplify-manufacturing-flow --strict
node --test custom_apps/process_simplification/process_simplification/tests/js/*.test.js
git diff --check
```

Expected: zero test failures, strict OpenSpec validation success, Node tests pass and no whitespace errors.

- [ ] **Step 4: Browser walkthrough**

Verify the page with at least one overdue production order, one active-production order and one direct-stock order. Confirm default ordering, expansion, filters, route focus, direct-stock visibility, action refresh, export and narrow-width scrolling.

- [ ] **Step 5: Commit and push**

```bash
git add custom_apps/process_simplification/README.md \
  openspec/changes/simplify-manufacturing-flow/specs/order-fulfillment-workbench/spec.md
git commit -m "docs: define order fulfillment overview"
git push origin rc/develop
```

## Self-Review Result

- Spec coverage: every approved prototype element and boundary has an implementation or verification step.
- Placeholder scan: no deferred implementation markers remain.
- Type consistency: backend order fields match the frontend helper contract and test fixtures.
- Scope: one cross-order read model and one page replacement; production scheduling and procurement automation remain separate.
