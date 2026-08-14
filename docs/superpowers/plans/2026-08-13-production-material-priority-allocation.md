# Production Material Priority Allocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the production workbench allocate shared raw-material stock and open supply once, in delivery priority order, so each Sales Order Item shows whether it can start now.

**Architecture:** Keep `calculate_material_coverage` as the existing BOM/stock/supply fact collector and add a production-only sequential orchestrator in `api/production.py`. The orchestrator evaluates one demand at a time, carries earlier BOM demand into the stock calculation, carries remaining Purchase Order/Material Request quantities between demands, and then attaches per-demand material rows. `check_all_shortages` remains unchanged as the cross-order purchasing pool.

**Tech Stack:** Python/Frappe backend, ERPNext BOM and stock queries, `frappe.tests.UnitTestCase`, Node built-in test runner for the existing workbench renderer.

## Global Constraints

- Modify only `custom_apps/process_simplification` application code plus Superpowers documentation.
- Do not modify ERPNext or Frappe core.
- Do not create a persistent allocation ledger or any stock/purchase document.
- Priority is delivery date, then Sales Order creation time, Sales Order name, and Sales Order Item name; missing delivery dates sort after dated demands.
- Purchase shortage aggregation remains a company-wide pool.
- Follow strict TDD: each production behavior must first fail for the expected reason.

---

### Task 1: Sequential material allocation engine

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/api/production.py`
- Test: `custom_apps/process_simplification/process_simplification/tests/test_production_workbench.py`

**Interfaces:**
- Consumes: `calculate_material_coverage(demands, company, need_by_date=None, defaults=None, prior_demands=None)` and `_material_demands(demands)`.
- Produces: `material_priority_sort_key(demand) -> tuple` and `attach_priority_material_coverage(demands, company) -> list[dict]`.

- [ ] **Step 1: Write failing stock-priority tests**

Add tests with two real demand dictionaries and controlled BOM/stock query boundaries. Assert literal results:

```python
result = production.attach_priority_material_coverage([late, early], "_Test Company")
by_key = {row["demand_key"]: row for row in result}
self.assertEqual(by_key["EARLY"]["materials"][0]["current_gap_qty"], 0)
self.assertEqual(by_key["LATE"]["materials"][0]["current_gap_qty"], 10)
```

Add a same-delivery-date case whose input is reversed; the older `creation` row must consume the available stock first. Add a missing-delivery-date case proving the dated demand consumes stock before the undated data-risk row.

- [ ] **Step 2: Run tests and verify RED**

Run inside `erpnext-development-frappe-1`:

```bash
bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_production_workbench
```

Expected: failures because `attach_priority_material_coverage` and `material_priority_sort_key` do not exist.

- [ ] **Step 3: Implement minimal stock-priority orchestration**

Add a deterministic key:

```python
def material_priority_sort_key(demand):
    return (
        not bool(demand.get("delivery_date")),
        demand.get("delivery_date") or "9999-12-31",
        str(demand.get("creation") or ""),
        str(demand.get("sales_order") or ""),
        str(demand.get("sales_order_item") or demand.get("demand_key") or ""),
    )
```

Sort copied demands, call `calculate_material_coverage` for each demand with all earlier `_material_demands` passed as `prior_demands`, and retain each per-demand coverage for attachment. Build `(item_code, warehouse)` totals and source counts across the per-demand results so `total_required_qty`, `source_count`, and `is_shared` remain meaningful.

- [ ] **Step 4: Run tests and verify GREEN**

Run the Task 1 command. Expected: all `test_production_workbench` tests pass.

- [ ] **Step 5: Commit**

```bash
git add custom_apps/process_simplification/process_simplification/api/production.py \
  custom_apps/process_simplification/process_simplification/tests/test_production_workbench.py
git commit -m "fix(process-simplification): allocate production materials by delivery priority"
```

### Task 2: Allocate open supply once and derive start readiness

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/api/production.py`
- Test: `custom_apps/process_simplification/process_simplification/tests/test_production_workbench.py`
- Modify: `custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/production_workbench.js`
- Test: `custom_apps/process_simplification/process_simplification/tests/js/production_workbench.test.js`

**Interfaces:**
- Consumes: per-demand coverage rows containing `current_gap_qty` and `supply_documents`.
- Produces: each supply document gains `allocated_qty`; material `open_purchase_order_qty` and `open_material_request_qty` become the quantity allocated to this demand.

- [ ] **Step 1: Write failing shared-supply tests**

Add a backend case with zero stock, two demands of 10, and one on-time Purchase Order of 10. Assert the earlier demand receives `open_purchase_order_qty == 10` with `status == "awaiting_purchase_receipt"`; the later demand receives zero and `shortage_qty == 10`.

Add a deadline case with a Purchase Order scheduled between two delivery dates. Assert it is late and unallocated for the early demand, but allocated to the later demand.

Add a status case with an unstarted Work Order: current stock coverage keeps `ready_to_start`; Purchase Order-only coverage changes the main status to `material_shortage` while the material summary is `awaiting_supply`.

- [ ] **Step 2: Run tests and verify RED**

Run the Task 1 backend command. Expected: both demands currently reuse the same open supply, and an unstarted Work Order can still remain `ready_to_start` when only inbound supply covers it.

- [ ] **Step 3: Implement remaining-supply pools**

Within `attach_priority_material_coverage`, keep remaining quantities by `(item_code, warehouse, doctype, document_name)`. For each material row, allocate non-late Purchase Order documents first, then Material Request documents, never below zero. Recalculate `shortage_qty` and status from this demand's allocations. Add `allocated_qty` to the copied document rows.

Update material attachment so any `current_gap_qty > 0` prevents `ready_to_start`, while `handle_shortage` remains limited to `new_purchase_required`. Do not downgrade `unplanned`, `in_production`, or `partially_completed` workflow states.

- [ ] **Step 4: Expose document allocation in the renderer**

Render `allocated_qty` beside document outstanding quantity only when the field is present. Add a Node assertion that the generated HTML contains the per-order allocation and does not replace the document's total outstanding quantity.

- [ ] **Step 5: Run backend and frontend tests and verify GREEN**

```bash
bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_production_workbench
node --test custom_apps/process_simplification/process_simplification/tests/js/production_workbench.test.js
```

Expected: both commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add custom_apps/process_simplification/process_simplification/api/production.py \
  custom_apps/process_simplification/process_simplification/tests/test_production_workbench.py \
  custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/production_workbench.js \
  custom_apps/process_simplification/process_simplification/tests/js/production_workbench.test.js
git commit -m "fix(process-simplification): prevent duplicate supply allocation"
```

### Task 3: Connect the production overview and verify regression behavior

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/api/production.py`
- Test: `custom_apps/process_simplification/process_simplification/tests/test_production_workbench.py`

**Interfaces:**
- Consumes: `attach_priority_material_coverage(company_demands, company)`.
- Produces: `get_production_overview()` returns per-demand material coverage; `check_all_shortages()` remains aggregate.

- [ ] **Step 1: Write failing overview integration test**

Patch only external ERPNext query boundaries needed to build two workbench demands, then call `get_production_overview`. Assert the earlier order is material-ready and the later order is short. Also call `calculate_material_shortages` with both demands and assert it still returns the combined purchasing requirement.

- [ ] **Step 2: Run the test and verify RED**

Expected: the overview still calls one company-wide `calculate_material_coverage` and projects the same shortage onto both demands.

- [ ] **Step 3: Replace aggregate projection in the overview**

For each company, call `attach_priority_material_coverage` instead of one aggregate coverage call. Preserve the existing BOM expansion error handling, final production display sort, summary shape, and `other_work_orders` query.

- [ ] **Step 4: Run focused and full related regression tests**

```bash
bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_production_workbench
bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_shared_material_allocation
bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_material_coverage_integration
node --test custom_apps/process_simplification/process_simplification/tests/js/*.test.js
```

Expected: all commands exit 0 with no failures.

- [ ] **Step 5: Verify live workbench data**

Run:

```bash
bench --site development.localhost execute \
  process_simplification.api.production.get_production_overview
```

Confirm `SAL-ORD-2026-00003` no longer inherits the 33,807-unit company-wide material shortage, later demands consume only residual stock/supply, and all demand rows retain their source totals and linked purchase documents.

- [ ] **Step 6: Commit**

```bash
git add custom_apps/process_simplification/process_simplification/api/production.py \
  custom_apps/process_simplification/process_simplification/tests/test_production_workbench.py \
  docs/superpowers/plans/2026-08-13-production-material-priority-allocation.md
git commit -m "fix(process-simplification): show per-order material readiness"
```
