# Production Plan Work Order Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace exploded-finished-BOM startability with Production Plan Work Order readiness, and make one-click purchasing buy only final leaf raw-material shortages for active plans.

**Architecture:** Sales Order Items remain demand sources, Production Plans group production, and submitted Work Orders plus current stock determine execution readiness. A new `production_readiness.py` module loads plan/work-order facts, builds the sub-assembly graph, allocates current stock by Production Plan date, and emits leaf purchase requirements; existing `shortage.py` continues to own stock/supply snapshots and Material Request creation.

**Tech Stack:** Python 3, Frappe/ERPNext v17 document APIs and query builder, JavaScript Desk pages, Frappe `UnitTestCase`/`IntegrationTestCase`, Node test runner.

## Global Constraints

- Work only on `codex/production-plan-work-order-readiness`, based on `rc/develop`.
- Do not use OpenSpec.
- Do not modify Frappe or ERPNext core code; change only `process_simplification` and repository design/plan documents.
- Any item with an active BOM or an in-house Production Plan sub-assembly row is manufactured even when `is_purchase_item == 1`.
- Only final leaf raw materials may enter one-click purchasing.
- Allocate raw-material stock, semi-finished stock, Material Requests, and Purchase Orders by Production Plan planned date; this normally preserves earlier Sales Order delivery priority.
- `ready_now` uses current physical stock only. Planned child output, Material Requests, and Purchase Orders are future supply and cannot make a Work Order startable now.
- Preserve ERPNext standard Stock Entry negative-stock validation as the final execution guard.
- Never populate native Material Request Item or Purchase Order Item `sales_order_item` for shared raw-material traceability.
- Use TDD for every behavior change and run each focused test once in RED and again in GREEN.

---

## File Structure

- Create `custom_apps/process_simplification/process_simplification/api/production_readiness.py`: plan/work-order loading, graph construction, priority sorting, direct-material allocation, plan summaries, and leaf purchase requirement generation.
- Modify `custom_apps/process_simplification/process_simplification/api/production.py`: attach plan readiness to order production demands and stop using exploded BOM coverage for demands with active Production Plans.
- Modify `custom_apps/process_simplification/process_simplification/api/workbench.py`: attach lightweight Production Plan summaries to Sales Order Item rows.
- Modify `custom_apps/process_simplification/process_simplification/api/shortage.py`: calculate coverage from normalized leaf requirements, switch shortage endpoints to active plans, and revalidate before Material Request creation.
- Modify `custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/production_workbench.js`: render Production Plans and Work Order dependency rows.
- Modify `custom_apps/process_simplification/process_simplification/process_simplification/page/order_workbench/order_workbench.js`: render Production Plan links and progress.
- Modify `custom_apps/process_simplification/process_simplification/process_simplification/page/shortage_purchase_planning/shortage_purchase_planning.js`: show Production Plan and Work Order purchase sources and preserve source identifiers for server revalidation.
- Modify `custom_apps/process_simplification/process_simplification/public/css/process_simplification.css`: minimal Work Order tree and plan-summary styles.
- Create `custom_apps/process_simplification/process_simplification/tests/test_production_readiness.py`: pure unit coverage for graph, priority, allocation, and leaf classification.
- Modify `custom_apps/process_simplification/process_simplification/tests/test_production_plan_subassembly.py`: real multi-level readiness transition integration test.
- Modify `custom_apps/process_simplification/process_simplification/tests/test_production_workbench.py`: production API composition and filtering tests.
- Modify `custom_apps/process_simplification/process_simplification/tests/test_aggregated_shortage.py`: plan-based purchasing and revalidation tests.
- Modify `custom_apps/process_simplification/process_simplification/tests/js/production_workbench.test.js`: plan/work-order rendering and filters.
- Modify `custom_apps/process_simplification/process_simplification/tests/js/order_fulfillment_overview.test.js`: Production Plan link rendering.

### Task 1: Pure Production Plan graph and priority model

**Files:**
- Create: `custom_apps/process_simplification/process_simplification/api/production_readiness.py`
- Create: `custom_apps/process_simplification/process_simplification/tests/test_production_readiness.py`

**Interfaces:**
- Produces: `plan_priority_key(plan: Mapping) -> tuple`.
- Produces: `build_work_order_graph(plan: Mapping, work_orders: list[Mapping], required_items: list[Mapping], sub_assemblies: list[Mapping], active_bom_items: set[str]) -> frappe._dict`.
- Produces each Work Order row with `name`, `production_item`, `bom_level`, `parent_work_order`, `child_work_orders`, `priority_date`, `required_items`, and `is_finished_good`.

- [ ] **Step 1: Write failing priority and graph tests**

Add literal fixtures for two plans and a three-level Work Order chain. Assert:

```python
self.assertLess(
	plan_priority_key({"planned_date": "2026-08-20", "creation": "2026-08-01", "name": "PP-EARLY"}),
	plan_priority_key({"planned_date": "2026-08-25", "creation": "2026-07-01", "name": "PP-LATE"}),
)
self.assertEqual(graph.work_orders_by_name["WO-FG"].child_work_orders, ["WO-SA"])
self.assertEqual(graph.work_orders_by_name["WO-SA"].parent_work_order, "WO-FG")
self.assertEqual(graph.execution_order, ["WO-LEAF", "WO-SA", "WO-FG"])
```

The fixtures must use `production_plan_sub_assembly_item` to match Work Orders to sub-assembly rows and `parent_item_code` to construct parent/child edges.

- [ ] **Step 2: Run Task 1 tests and verify RED**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_production_readiness
```

Expected: import failure because `production_readiness.py` does not exist.

- [ ] **Step 3: Implement priority normalization and graph construction**

Implement the public functions and these rules:

```python
def plan_priority_key(plan):
	return (
		str(plan.get("planned_date") or plan.get("posting_date") or "9999-12-31"),
		str(plan.get("creation") or ""),
		str(plan.get("name") or ""),
	)
```

Resolve a top-level Work Order from `production_plan_item`; resolve a sub-assembly Work Order from `production_plan_sub_assembly_item`; sort execution by descending `bom_level`, then Work Order creation/name. Store an active-BOM item without a matching child Work Order as a manufactured dependency so later tasks can report `production_task_missing` rather than purchasing it.

- [ ] **Step 4: Run Task 1 tests and verify GREEN**

Run the Step 2 command. Expected: all `test_production_readiness` tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add custom_apps/process_simplification/process_simplification/api/production_readiness.py \
  custom_apps/process_simplification/process_simplification/tests/test_production_readiness.py
git commit -m "feat(process-simplification): model production plan work order graph"
```

### Task 2: Allocate current stock and derive Work Order readiness

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/api/production_readiness.py`
- Test: `custom_apps/process_simplification/process_simplification/tests/test_production_readiness.py`

**Interfaces:**
- Consumes: Task 1 graph rows.
- Produces: `allocate_work_order_readiness(plans: list[Mapping], stock_snapshots: Mapping[tuple[str, str], Mapping]) -> list[frappe._dict]`.
- Produces: `summarize_plan(plan: Mapping) -> dict` with `ready_work_order_count`, `waiting_subassembly_count`, `purchase_shortage_work_order_count`, `awaiting_supply_work_order_count`, `blocked_work_order_count`, `completed_work_order_count`, and `total_work_order_count`.

- [ ] **Step 1: Write failing direct-material readiness tests**

Cover these independent mutations with literal expected statuses:

```python
self.assertEqual(by_name["WO-LEAF"].readiness_status, "ready_now")
self.assertEqual(by_name["WO-SA"].readiness_status, "waiting_subassembly")
self.assertEqual(by_name["WO-FG"].readiness_status, "waiting_subassembly")
```

Add cases proving:

- `required_qty - transferred_qty` is the allocated demand.
- Two plans competing for the same raw material allocate it to the earlier plan date only.
- The same date uses plan creation/name as a stable tie-break.
- A manufactured item with `is_purchase_item == 1` remains `waiting_subassembly` or `production_task_missing` and never becomes `purchase_shortage`.
- PO/MR quantities do not affect `ready_now`.

- [ ] **Step 2: Run the readiness cases and verify RED**

Run the Task 1 test command. Expected: failure because allocation/status functions are absent.

- [ ] **Step 3: Implement the minimal allocation engine**

Allocate stock pools by `(item_code, source_warehouse)` in plan priority and deepest-first execution order. Use `available_qty` from the supplied snapshot; do not subtract production reservations a second time. For each required item return:

```python
{
	"item_code": item_code,
	"warehouse": source_warehouse,
	"required_qty": remaining_required,
	"available_qty": allocated_qty,
	"current_gap_qty": max(remaining_required - allocated_qty, 0),
	"supply_type": "manufactured" if manufactured else "purchased",
	"child_work_order": child_name,
}
```

Determine the Work Order status from current stock gaps and dependencies, with transferred/in-progress/completed states taking precedence over shortage labels.

- [ ] **Step 4: Run readiness tests and verify GREEN**

Run the Task 1 command. Expected: all pure readiness tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add custom_apps/process_simplification/process_simplification/api/production_readiness.py \
  custom_apps/process_simplification/process_simplification/tests/test_production_readiness.py
git commit -m "feat(process-simplification): calculate work order startability"
```

### Task 3: Load live plan facts and integrate the production API

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/api/production_readiness.py`
- Modify: `custom_apps/process_simplification/process_simplification/api/production.py`
- Modify: `custom_apps/process_simplification/process_simplification/api/workbench.py`
- Test: `custom_apps/process_simplification/process_simplification/tests/test_production_workbench.py`

**Interfaces:**
- Produces: `get_production_plan_readiness(company: str | None = None, sales_order_items: Iterable[str] | None = None) -> dict[str, list[dict]]`, keyed by Sales Order Item.
- Production demand rows gain `production_plans`, `work_order_summary`, and all related Work Orders.
- Order workbench rows gain `production_plans: list[{name, planned_date, status, completed_work_order_count, total_work_order_count}]`.

- [ ] **Step 1: Write failing API composition tests**

Patch only the database-loading boundary to return one plan with a leaf and top Work Order. Assert the production demand exposes both Work Orders, reports one ready Work Order, and no longer calls `calculate_material_coverage` for that planned demand. Add an unplanned-demand case proving the existing `unplanned` status remains.

- [ ] **Step 2: Run focused backend tests and verify RED**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_production_workbench
```

Expected: missing plan fields and the existing exploded-BOM coverage call still occurs.

- [ ] **Step 3: Implement batched live loading**

Load all selected active Work Orders, Work Order Items, Production Plans, Production Plan Items, and Production Plan Sub Assembly Items in batched queries. Include submitted Work Orders that are not Completed, Stopped, Closed, or Cancelled; retain completed Work Orders only for progress counts. Resolve `planned_date` from sub-assembly `schedule_date`, plan-item `planned_start_date`, then plan `posting_date`.

Use `get_material_stock_snapshot()` for one cached `(item, warehouse)` fact per request. Keep the new module read-only.

- [ ] **Step 4: Attach plan results and replace the false top-level status**

For demands with plans:

- Do not call `attach_priority_material_coverage`.
- Set `ready_to_start` only when `ready_work_order_count > 0`.
- Set `material_shortage` when a Work Order has a new leaf purchase shortage.
- Add `waiting_subassembly` when no Work Order is ready and at least one waits on upstream output.
- Preserve `in_production`, `partially_completed`, `unplanned`, and master-data blockers where their underlying facts apply.

For demands without an active plan, keep `unplanned`; do not claim BOM-based current readiness.

- [ ] **Step 5: Run Task 3 tests and verify GREEN**

Run the Step 2 command. Expected: all production workbench backend tests pass after updating obsolete exploded-BOM assertions to the new plan contract.

- [ ] **Step 6: Commit Task 3**

```bash
git add custom_apps/process_simplification/process_simplification/api/production_readiness.py \
  custom_apps/process_simplification/process_simplification/api/production.py \
  custom_apps/process_simplification/process_simplification/api/workbench.py \
  custom_apps/process_simplification/process_simplification/tests/test_production_workbench.py
git commit -m "feat(process-simplification): expose plan work order readiness"
```

### Task 4: Generate and cover only leaf purchase requirements

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/api/production_readiness.py`
- Modify: `custom_apps/process_simplification/process_simplification/api/shortage.py`
- Test: `custom_apps/process_simplification/process_simplification/tests/test_production_readiness.py`
- Test: `custom_apps/process_simplification/process_simplification/tests/test_aggregated_shortage.py`

**Interfaces:**
- Produces: `get_plan_purchase_requirements(company: str, sales_order_items: Iterable[str] | None = None) -> list[dict]`.
- Produces: `calculate_requirement_coverage(requirements, company, defaults=None, *, fact_cache=None, prior_consumed=None) -> frappe._dict` in `shortage.py`.
- `check_shortage` and `check_all_shortages` return only plan-derived leaf shortages.

- [ ] **Step 1: Write failing leaf-purchase tests**

Build a plan where `FG -> SA -> RM`, set both `SA.is_purchase_item` and `RM.is_purchase_item`, and assert:

```python
self.assertEqual([row["item_code"] for row in requirements], ["RM"])
self.assertEqual(requirements[0]["sources"][0]["production_plan"], "PP-001")
self.assertEqual(requirements[0]["sources"][0]["work_order"], "WO-SA")
```

Add a missing-child case asserting no Material Request row for `SA` and a `production_task_missing` blocker.

- [ ] **Step 2: Run leaf requirement tests and verify RED**

Run the Task 1 command. Expected: failure because purchase requirement generation is absent.

- [ ] **Step 3: Implement leaf requirement generation**

For every unfinished Work Order direct item, emit `required_qty - transferred_qty` only when the item has no active BOM and is not an in-house Production Plan sub-assembly. Preserve sources with `production_plan`, `work_order`, `sales_order`, `sales_order_item`, `finished_item`, `planned_date`, and `required_qty`.

- [ ] **Step 4: Write failing plan-based shortage endpoint tests**

Replace BOM-demand fixtures in `test_aggregated_shortage.py` with normalized plan requirements. Assert earlier Production Plan dates consume shared current stock and PO/MR rows before later plans, and `check_shortage` for a later selected plan sees earlier plans as prior demand.

- [ ] **Step 5: Run shortage tests and verify RED**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_aggregated_shortage
```

Expected: existing endpoints still request exploded Sales Order BOM demands.

- [ ] **Step 6: Extract normalized requirement coverage and switch endpoints**

Move the stock/MR/PO/status portion of `calculate_material_coverage` behind `calculate_requirement_coverage`; keep the existing BOM wrapper for Quick Sales Order preflight. Switch `check_shortage` and `check_all_shortages` to Production Plan leaf requirements ordered by plan date. Return “请先安排生产” when selected order rows have no active Production Plan.

- [ ] **Step 7: Run Task 4 tests and verify GREEN**

Run the Task 1 and Step 5 commands. Expected: both modules pass.

- [ ] **Step 8: Commit Task 4**

```bash
git add custom_apps/process_simplification/process_simplification/api/production_readiness.py \
  custom_apps/process_simplification/process_simplification/api/shortage.py \
  custom_apps/process_simplification/process_simplification/tests/test_production_readiness.py \
  custom_apps/process_simplification/process_simplification/tests/test_aggregated_shortage.py
git commit -m "feat(process-simplification): purchase only plan leaf shortages"
```

### Task 5: Revalidate one-click purchasing before write

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/api/shortage.py`
- Test: `custom_apps/process_simplification/process_simplification/tests/test_aggregated_shortage.py`

**Interfaces:**
- `create_material_request(shortage_rows, company=None, schedule_date=None)` keeps its public signature.
- Every accepted row must include plan sources and must not exceed the freshly recalculated `(item_code, warehouse)` shortage for those plans.

- [ ] **Step 1: Write failing stale-shortage tests**

Add one test where the page submits quantity 10 but fresh plan shortage is 4; assert no Material Request is inserted and the API raises the Chinese stale-result error. Add one passing test where requested quantity is 4 and assert the Material Request description contains Production Plan and Work Order source text but leaves native `sales_order_item` empty.

- [ ] **Step 2: Run shortage tests and verify RED**

Run the Task 4 shortage command. Expected: the stale quantity is currently accepted.

- [ ] **Step 3: Implement server-side revalidation**

Extract plan names from `row.sources`, recalculate current plan shortages once for the union of plans, index them by `(item_code, warehouse)`, and reject missing or excessive rows before `frappe.new_doc("Material Request")`. Preserve the existing explicit over-purchase override only for manually authorized quantities after a fresh shortage still exists; never allow a manufactured item.

- [ ] **Step 4: Run shortage tests and verify GREEN**

Run the Task 4 shortage command. Expected: all plan shortage and write-safety tests pass.

- [ ] **Step 5: Commit Task 5**

```bash
git add custom_apps/process_simplification/process_simplification/api/shortage.py \
  custom_apps/process_simplification/process_simplification/tests/test_aggregated_shortage.py
git commit -m "fix(process-simplification): revalidate plan shortages before purchase"
```

### Task 6: Render Production Plans and Work Order readiness

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/production_workbench.js`
- Modify: `custom_apps/process_simplification/process_simplification/process_simplification/page/order_workbench/order_workbench.js`
- Modify: `custom_apps/process_simplification/process_simplification/process_simplification/page/shortage_purchase_planning/shortage_purchase_planning.js`
- Modify: `custom_apps/process_simplification/process_simplification/public/css/process_simplification.css`
- Test: `custom_apps/process_simplification/process_simplification/tests/js/production_workbench.test.js`
- Test: `custom_apps/process_simplification/process_simplification/tests/js/order_fulfillment_overview.test.js`

**Interfaces:**
- Production Workbench renders `demand.production_plans[].work_orders[]`.
- Order Workbench renders `row.production_plans[]` links.
- Shortage page renders plan/work-order sources without changing the selected-row route contract.

- [ ] **Step 1: Write failing frontend rendering tests**

Assert literal HTML behaviors:

- `PP-001` links to `/app/production-plan/PP-001`.
- `WO-LEAF` shows “当前可开工”.
- `WO-FG` shows “等待半成品” and its child item.
- A plan card says “可先开工 1 张工单”, not that the top-level Work Order is ready.
- Order rows show plan progress `1 / 3` and a Production Plan link.
- Shortage sources show `PP-001 / WO-SA`.

- [ ] **Step 2: Run frontend tests and verify RED**

```bash
node --test \
  custom_apps/process_simplification/process_simplification/tests/js/production_workbench.test.js \
  custom_apps/process_simplification/process_simplification/tests/js/order_fulfillment_overview.test.js
```

Expected: plan/work-order markup is absent.

- [ ] **Step 3: Implement minimal safe rendering**

Add status metadata for all Task 2 states, render the deepest Work Orders first with indentation/parent information, and retain HTML escaping and standard Desk routes. Change “只看缺料” to leaf `purchase_shortage` semantics and add a “当前可开工” filter for `ready_now` Work Orders. Keep current pagination and route refresh behavior.

- [ ] **Step 4: Run frontend tests and verify GREEN**

Run the Step 2 command. Expected: all focused JS tests pass.

- [ ] **Step 5: Commit Task 6**

```bash
git add custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/production_workbench.js \
  custom_apps/process_simplification/process_simplification/process_simplification/page/order_workbench/order_workbench.js \
  custom_apps/process_simplification/process_simplification/process_simplification/page/shortage_purchase_planning/shortage_purchase_planning.js \
  custom_apps/process_simplification/process_simplification/public/css/process_simplification.css \
  custom_apps/process_simplification/process_simplification/tests/js/production_workbench.test.js \
  custom_apps/process_simplification/process_simplification/tests/js/order_fulfillment_overview.test.js
git commit -m "feat(process-simplification): show plan work order readiness"
```

### Task 7: Real multi-level regression and full verification

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/tests/test_production_plan_subassembly.py`
- Verify all files changed in Tasks 1–6.

**Interfaces:**
- Proves the real Production Plan adapter, submitted Work Orders, stock ledger, readiness API, and leaf purchase requirements agree.

- [ ] **Step 1: Extend the real two-level integration fixture**

Start with no sub-assembly stock and enough leaf raw material. Assert the child Work Order is `ready_now`, the finished-good Work Order is `waiting_subassembly`, and purchase requirements never contain the sub-assembly even if its Item is changed to `is_purchase_item = 1`.

- [ ] **Step 2: Run the integration module and verify RED**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_production_plan_subassembly
```

Expected: readiness assertions fail before the integration wiring is complete.

- [ ] **Step 3: Complete the minimum integration wiring**

Make only changes required by the failing real-document test. Do not weaken ERPNext submission, stock, or warehouse validations.

- [ ] **Step 4: Run focused backend and frontend regressions**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_production_readiness
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_production_workbench
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_aggregated_shortage
node --test custom_apps/process_simplification/process_simplification/tests/js/*.test.js
```

Expected: zero failures.

- [ ] **Step 5: Run full custom-app verification**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification
python -m compileall -q custom_apps/process_simplification/process_simplification
git diff --check rc/develop...HEAD
git status --short --branch
```

Expected: backend suite, syntax compilation, and diff check exit 0; only intentional branch commits remain.

- [ ] **Step 6: Run the live read-only regression**

Call `get_production_demand` for `SAL-ORD-2026-00012 / uq14rb5f72`. Confirm `MFG-WO-2026-00036` is the first startable Work Order and `MFG-WO-2026-00031` waits on “焊线线圈”; do not modify or submit production documents during this check.

- [ ] **Step 7: Commit Task 7**

```bash
git add custom_apps/process_simplification/process_simplification/tests/test_production_plan_subassembly.py
git commit -m "test(process-simplification): verify multilevel plan readiness"
```
