# Order and Production Workbenches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate customer-order fulfillment from owner production decisions by narrowing the existing order workbench and adding a production-demand workbench organized by Sales Order Item and delivery date.

**Architecture:** Keep ERPNext standard documents as the only source of truth. Extend the existing item workbench with explicit finished-stock coverage and production-demand quantities, add a read-only production aggregation API that reuses the current BOM material coverage engine, and build a new Frappe Page whose write actions call the existing server-side Work Order, shortage, and finished-goods reservation APIs.

**Tech Stack:** ERPNext/Frappe 17, Python, Frappe ORM/query builder, JavaScript/jQuery Frappe Page API, CSS, Frappe `UnitTestCase`, Node built-in test runner.

## Global Constraints

- Work only on `rc/develop`; do not put feature commits on `develop`.
- Modify only the custom app plus Superpowers docs; do not modify ERPNext/Frappe core.
- Do not use or update OpenSpec.
- Do not add a fulfillment, production-demand, or material-allocation DocType.
- Sales Order Item name is the production-demand identity.
- All statuses are derived from current standard documents.
- Every write action must perform a fresh server-side validation.
- Default order is delivery date ascending, then risk descending, then unplanned quantity descending, then creation ascending.
- Production workbench phase one is for the owner; shop-floor execution stays in standard Work Order/Stock Entry and a later worker page.
- UI copy is Chinese and narrow screens must use labelled cards instead of clipped tables.

---

## File Structure

- Modify `process_simplification/api/utils.py`: extend the item workbench row contract with finished-stock and production-demand fields.
- Modify `process_simplification/api/workbench.py`: calculate non-overlapping finished-stock coverage and make order actions hand off production work.
- Create `process_simplification/api/production.py`: own production-demand aggregation, state/risk derivation, material-source projection and independent Work Order discovery.
- Modify `process_simplification/api/actions.py`: validate Work Order creation against the new `unplanned_production_qty` contract.
- Create `process_simplification/page/production_workbench/*`: production Page metadata and client UI.
- Modify `process_simplification/page/order_workbench/order_workbench.js`: rename the page, show the new quantities and route production actions to the new workbench.
- Modify `process_simplification/public/css/process_simplification.css`: shared order/production cards and mobile layouts.
- Modify workspace and sidebar JSON: add production workbench navigation and rename the order entry.
- Modify Python and Node tests: protect formulas, sorting, filters, HTML escaping, action ownership and mobile labels.
- Modify `custom_apps/process_simplification/README.md`: document the two workbench boundaries.

### Task 1: Establish the production-demand quantity contract

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/api/utils.py`
- Modify: `custom_apps/process_simplification/process_simplification/api/workbench.py`
- Modify: `custom_apps/process_simplification/process_simplification/api/actions.py`
- Test: `custom_apps/process_simplification/process_simplification/tests/test_simplified_flow.py`

**Interfaces:**
- Produces row fields `available_to_reserve`, `finished_stock_coverage_qty`, `production_required_qty`, `unplanned_production_qty`, and `overplanned_qty`.
- Preserves `uncovered_qty` as an alias of `unplanned_production_qty` during this migration so the shortage page remains compatible.
- Changes `create_work_order()` validation and default quantity to `unplanned_production_qty`.

- [ ] **Step 1: Write failing quantity tests**

Add a pure helper contract and tests with literal expectations:

```python
def test_production_quantities_do_not_duplicate_available_finished_stock(self):
	result = calculate_production_quantities(
		pending_qty=100,
		reserved_qty=20,
		available_to_reserve=30,
		active_work_order_qty=40,
	)
	self.assertEqual(result.finished_stock_coverage_qty, 50)
	self.assertEqual(result.production_required_qty, 50)
	self.assertEqual(result.unplanned_production_qty, 10)

def test_overplanned_work_order_is_reported_without_negative_unplanned_qty(self):
	result = calculate_production_quantities(10, 0, 0, 15)
	self.assertEqual(result.unplanned_production_qty, 0)
	self.assertEqual(result.overplanned_qty, 5)
```

- [ ] **Step 2: Run the focused Python module and verify RED**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_simplified_flow
```

Expected: failure because `calculate_production_quantities` and the new fields do not exist.

- [ ] **Step 3: Implement the minimal calculation and row fields**

Create a small immutable result in `workbench.py` and calculate:

```python
effective_reserved = min(max(reserved_qty, 0), max(pending_qty, 0))
available_finished = min(max(available_to_reserve, 0), max(pending_qty - effective_reserved, 0))
finished_coverage = effective_reserved + available_finished
production_required = max(pending_qty - finished_coverage, 0)
unplanned = max(production_required - active_work_order_qty, 0)
overplanned = max(active_work_order_qty - production_required, 0)
```

Only query `get_available_qty_to_reserve` when a valid order-line warehouse exists. Populate the new WorkbenchRow fields and keep `uncovered_qty=unplanned` for compatibility.

- [ ] **Step 4: Move production action ownership**

For a row with `unplanned_production_qty > 0` and a valid BOM, expose `open_production_workbench` instead of `create_work_order`. Keep stock reservation, completed-stock reservation, Delivery Note and Sales Order actions in the order workbench.

- [ ] **Step 5: Make Work Order creation validate the new quantity**

In `actions.create_work_order`, reject when `unplanned_production_qty <= 0`, default to that quantity and reject requested quantities above it. This is the authoritative write-time recheck used by the new production page.

- [ ] **Step 6: Run the focused test and commit**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_simplified_flow
git diff --check
git add custom_apps/process_simplification/process_simplification/api/utils.py \
  custom_apps/process_simplification/process_simplification/api/workbench.py \
  custom_apps/process_simplification/process_simplification/api/actions.py \
  custom_apps/process_simplification/process_simplification/tests/test_simplified_flow.py
git commit -m "refactor: separate order and production quantities"
```

### Task 2: Add the production-demand aggregation API

**Files:**
- Create: `custom_apps/process_simplification/process_simplification/api/production.py`
- Test: `custom_apps/process_simplification/process_simplification/tests/test_production_workbench.py`

**Interfaces:**
- Consumes `get_order_workbench(sales_order)` rows and `calculate_material_coverage(demands, company, need_by_date, defaults)`.
- Produces `build_production_demand(order, row, today) -> dict`.
- Produces `attach_material_coverage(demands, coverage) -> list[dict]` using each coverage material's `sources`.
- Produces whitelisted `get_production_overview() -> {checked_at, summary, demands, other_work_orders}`.

- [ ] **Step 1: Write failing state and sorting tests**

Use literal row fixtures to prove:

```python
def test_unplanned_demand_is_visible_without_a_work_order(self):
	demand = build_production_demand(order, row_with_unplanned_10, today="2026-08-02")
	self.assertEqual(demand["status_code"], "unplanned")
	self.assertEqual(demand["unplanned_production_qty"], 10)

def test_stock_only_row_is_excluded_from_production_overview(self):
	self.assertIsNone(build_production_demand(order, stock_covered_row, today="2026-08-02"))
```

Also cover missing BOM, overdue risk, active production, completed-unreserved handback, overplanned quantity, same-date risk sorting, missing dates and aggregation counts.

- [ ] **Step 2: Run the new module and verify RED**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_production_workbench
```

Expected: import failure because `api.production` does not exist.

- [ ] **Step 3: Implement production demand/state derivation**

Return stable fields for source identity, customer, delivery timing, finished-stock coverage, production requirement, Work Order coverage, unplanned quantity, produced quantity, completed-unreserved quantity, status, risk, actions, material summary and linked Work Orders. Include a row when production is required, an active Work Order exists, overplanned quantity exists, or completed output awaits reservation.

- [ ] **Step 4: Attach global material risk without claiming exclusive stock**

Build one BOM demand per visible production demand using `production_required_qty`, include `demand_key`, source order/item, delivery date and finished item in each source, and call the existing exploded-BOM coverage engine once per company. Project each aggregate material back to every source with both `source_required_qty` and `total_required_qty`. Mark the demand as shortage when a contributing material has `new_purchase_required`; label shared materials explicitly when they have more than one source.

- [ ] **Step 5: Discover independent active Work Orders**

Read submitted, non-terminal Work Orders with no Sales Order/Sales Order Item link, return their standard identity, product, status, quantity, produced quantity and expected delivery date under `other_work_orders`. Do not create custom actions beyond viewing the standard Work Order.

- [ ] **Step 6: Run the new tests and commit**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_production_workbench
git diff --check
git add custom_apps/process_simplification/process_simplification/api/production.py \
  custom_apps/process_simplification/process_simplification/tests/test_production_workbench.py
git commit -m "feat: add production demand overview api"
```

### Task 3: Build the production workbench and handoff from orders

**Files:**
- Create: `custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/__init__.py`
- Create: `custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/production_workbench.json`
- Create: `custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/production_workbench.py`
- Create: `custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/production_workbench.js`
- Create: `custom_apps/process_simplification/process_simplification/tests/js/production_workbench.test.js`
- Modify: `custom_apps/process_simplification/process_simplification/process_simplification/page/order_workbench/order_workbench.js`
- Modify: `custom_apps/process_simplification/process_simplification/process_simplification/page/order_workbench/order_workbench.json`

**Interfaces:**
- Consumes `get_production_overview()` and existing `actions.create_work_order`, `actions.reserve_completed_stock`.
- Produces pure client helpers `filterProductionDemands`, `productionSummary`, `productionDemandHtml`, and `refreshProductionOverview` for Node tests.
- Route focus uses `/app/production-workbench/<sales-order-item>`.

- [ ] **Step 1: Write failing Node behavior tests**

Cover filters, visible KPI recalculation, HTML escaping, complete mobile `data-label` attributes, BOM material fields, Work Order links, and route focus. Add an order HTML assertion proving production actions render `open_production_workbench` and no longer render `create_work_order`.

- [ ] **Step 2: Run Node tests and verify RED**

```bash
node --test custom_apps/process_simplification/process_simplification/tests/js/production_workbench.test.js \
  custom_apps/process_simplification/process_simplification/tests/js/order_fulfillment_overview.test.js
```

Expected: failure because the production page/helpers and handoff action do not exist.

- [ ] **Step 3: Implement the production page**

Render six KPI cards, search/date/status/risk/customer filters, the deterministic sort note and expandable demand cards. Expanded sections show quantity relationships, linked Work Orders and material coverage. Export is not included in phase one. HTML-escape every server value and encode every route component.

- [ ] **Step 4: Wire limited owner actions**

- `create_work_order`: show a confirmation dialog with the current suggested quantity and call the existing server action.
- `check_materials`: reload the overview.
- `handle_shortage`: set `frappe.route_options.selected_rows` to the source Sales Order Item and route to `shortage-purchase-planning`.
- `reserve_completed_stock`: call the existing action.
- `view_work_order`, `view_sales_order`: open standard forms.

Reload the overview after every write.

- [ ] **Step 5: Change order workbench production actions into handoffs**

Rename the page title to `订单工作台`. Render finished-stock coverage, production requirement and unplanned production fields. `open_production_workbench` routes to the focused production page; remove the order-level detailed material-check button and direct Work Order dialog.

- [ ] **Step 6: Run Node tests and commit**

```bash
node --test custom_apps/process_simplification/process_simplification/tests/js/*.test.js
git diff --check
git add custom_apps/process_simplification/process_simplification/process_simplification/page/order_workbench \
  custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench \
  custom_apps/process_simplification/process_simplification/tests/js
git commit -m "feat: add owner production workbench"
```

### Task 4: Add navigation, responsive UI, documentation and full verification

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/public/css/process_simplification.css`
- Modify: `custom_apps/process_simplification/process_simplification/process_simplification/workspace/process_simplification/process_simplification.json`
- Modify: `custom_apps/process_simplification/process_simplification/workspace_sidebar/process_simplification.json`
- Modify: `custom_apps/process_simplification/README.md`

**Interfaces:**
- Produces desktop and mobile presentation for both workbenches.
- Produces workspace/sidebar entries `订单工作台` and `生产工作台`.

- [ ] **Step 1: Add shared responsive CSS**

Use grid-based collapsed cards on desktop. At widths below 768px, turn demand facts, linked Work Orders and material rows into one- or two-column cards with visible labels; stack action buttons at full width and remove table minimum widths for production details.

- [ ] **Step 2: Add Page navigation fixtures**

Add `生产工作台` after `订单工作台` in Workspace and Workspace Sidebar, with the Page route `production-workbench` and Manufacturing User/System Manager access. Rename `订单履约总览` navigation copy to `订单工作台` without changing its stable route.

- [ ] **Step 3: Update README**

Replace the “future production workbench” language with the implemented responsibilities, quantity formulas, material-risk caveat, write-time validation and explicit shop-floor exclusions.

- [ ] **Step 4: Run complete automated verification**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification
node --test custom_apps/process_simplification/process_simplification/tests/js/*.test.js
git diff --check
```

Expected: zero Python/Node failures and no whitespace errors.

- [ ] **Step 5: Apply fixtures and perform browser smoke testing**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost migrate
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost clear-cache
```

Verify one stock-only order remains in the order workbench, one unplanned production demand appears without a Work Order, one active Work Order expands correctly, one material shortage shows BOM details, route focus works, and both pages remain readable at 390px width.

- [ ] **Step 6: Commit final integration changes**

```bash
git add custom_apps/process_simplification/process_simplification/public/css/process_simplification.css \
  custom_apps/process_simplification/process_simplification/process_simplification/workspace/process_simplification/process_simplification.json \
  custom_apps/process_simplification/process_simplification/workspace_sidebar/process_simplification.json \
  custom_apps/process_simplification/README.md
git commit -m "docs: define workbench responsibilities"
```

## Self-Review Result

- Spec coverage: order responsibility, production demand, Work Order aggregation, BOM risk, safe actions, navigation and mobile behavior each map to an implementation task.
- Placeholder scan: no deferred code steps or OpenSpec steps remain.
- Type consistency: the backend production-demand fields match the planned JavaScript fixtures and action arguments.
- Scope: shop-floor execution, capacity planning, automatic Purchase Orders and payroll remain excluded.

