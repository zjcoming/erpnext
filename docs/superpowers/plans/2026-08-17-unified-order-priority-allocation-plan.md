# Unified Order Priority Allocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the order workbench and production plan center share one delivery-date-priority allocation for finished goods, raw materials, and inbound supply so common Sales Order Items show identical quantities and ordering.

**Architecture:** Move the finished-goods allocator and stable Sales Order Item priority key into the fulfillment workbench layer, allocate all open rows before building order summaries, and let the production center flatten those already allocated rows without recalculating. Enrich Production Plan Work Order readiness with linked Sales Order Item priority metadata and allocate work-order materials globally by the same priority key while preserving deepest-subassembly-first execution within one order demand.

**Tech Stack:** Python 3, Frappe/ERPNext document APIs, `frappe._dict`, Frappe `UnitTestCase`, JavaScript CommonJS helpers, Node.js built-in test runner.

## Global Constraints

- Modify only `custom_apps/process_simplification` plus its design, plan, and test documentation.
- Do not modify ERPNext/Frappe official source or core DocTypes.
- Do not create persistent allocation tables, automatic Stock Reservation Entries, or business documents during page reads.
- Keep Sales Order, Stock Reservation Entry, Production Plan, Work Order, Stock Entry, Material Request, and Purchase Order as facts of record.
- Customer-order allocation priority is Sales Order Item delivery date, then Sales Order creation, Sales Order name, and Sales Order Item index/name.
- Missing delivery dates sort after every dated order and must not claim stock or materials ahead of committed dates.
- Risk, status, Production Plan dates, and Work Order dates do not affect cross-order allocation priority.
- Search, filtering, and pagination run only after full allocation.
- Preserve the unrelated `.codex/config.toml` modification.

---

### Task 1: Shared Sales Order Item Priority and Finished-Goods Allocation

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/api/workbench.py`
- Modify: `custom_apps/process_simplification/process_simplification/tests/test_simplified_flow.py`

**Interfaces:**
- Produces: `order_item_priority_key(row) -> tuple`
- Produces: `allocate_finished_stock(rows) -> list[frappe._dict]`
- Produces: globally allocated rows inside `get_fulfillment_overview()` before `build_fulfillment_order()`.
- Rows carry `company`, `order_creation`, and `sales_order_item_idx` metadata in addition to existing public quantity fields.

- [ ] **Step 1: Add failing unit tests for the stable priority key**

Add tests using literal expected ordering:

```python
def test_order_item_priority_puts_missing_delivery_after_dated_rows(self):
	from process_simplification.api.workbench import order_item_priority_key

	dated = frappe._dict(delivery_date="2026-08-20", order_creation="2026-08-01", sales_order="SO-2", sales_order_item_idx=1, sales_order_item="SOI-2")
	missing = frappe._dict(delivery_date=None, order_creation="2026-07-01", sales_order="SO-1", sales_order_item_idx=1, sales_order_item="SOI-1")
	self.assertEqual(sorted([missing, dated], key=order_item_priority_key), [dated, missing])

def test_order_item_priority_breaks_equal_dates_by_creation_order_and_item(self):
	from process_simplification.api.workbench import order_item_priority_key

	rows = [
		frappe._dict(delivery_date="2026-08-20", order_creation="2026-08-02", sales_order="SO-2", sales_order_item_idx=1, sales_order_item="SOI-2"),
		frappe._dict(delivery_date="2026-08-20", order_creation="2026-08-01", sales_order="SO-1", sales_order_item_idx=2, sales_order_item="SOI-1-B"),
		frappe._dict(delivery_date="2026-08-20", order_creation="2026-08-01", sales_order="SO-1", sales_order_item_idx=1, sales_order_item="SOI-1-A"),
	]
	self.assertEqual([row.sales_order_item for row in sorted(rows, key=order_item_priority_key)], ["SOI-1-A", "SOI-1-B", "SOI-2"])
```

- [ ] **Step 2: Run the priority tests and verify RED**

Run:

```bash
docker compose exec -T --workdir /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_simplified_flow \
  --test TestSimplifiedFlow.test_order_item_priority_puts_missing_delivery_after_dated_rows
```

Expected: FAIL because `order_item_priority_key` does not exist.

- [ ] **Step 3: Implement the priority key and expose row metadata**

Add the pure key:

```python
def order_item_priority_key(row):
	row = frappe._dict(row or {})
	return (
		not bool(row.get("delivery_date")),
		str(row.get("delivery_date") or "9999-12-31"),
		str(row.get("order_creation") or row.get("creation") or ""),
		str(row.get("sales_order") or ""),
		cint(row.get("sales_order_item_idx") or 0),
		str(row.get("sales_order_item") or row.get("name") or ""),
	)
```

When serializing each `WorkbenchRow`, add `company=so.company`, `order_creation=str(so.creation or "")`, and `sales_order_item_idx=cint(item.idx)` to the dictionary returned by `get_order_workbench()`.

- [ ] **Step 4: Run the priority tests and verify GREEN**

Run both new test methods. Expected: PASS.

- [ ] **Step 5: Add failing tests for one-time finished-stock allocation**

Cover these literal behaviors:

```python
def test_finished_stock_allocation_respects_reservations_and_allocates_free_stock_once(self):
	from process_simplification.api.workbench import allocate_finished_stock

	rows = [
		frappe._dict(company="C", item_code="FG", warehouse="FG-C", delivery_date="2026-08-10", order_creation="2026-08-01", sales_order="SO-EARLY", sales_order_item_idx=1, sales_order_item="SOI-EARLY", pending_qty=40, reserved_qty=5, available_to_reserve=50, active_work_order_qty=0),
		frappe._dict(company="C", item_code="FG", warehouse="FG-C", delivery_date="2026-08-20", order_creation="2026-08-02", sales_order="SO-LATE", sales_order_item_idx=1, sales_order_item="SOI-LATE", pending_qty=40, reserved_qty=10, available_to_reserve=50, active_work_order_qty=0),
	]
	allocated = {row.sales_order_item: row for row in allocate_finished_stock(reversed(rows))}
	self.assertEqual(allocated["SOI-EARLY"].available_to_reserve, 35)
	self.assertEqual(allocated["SOI-EARLY"].finished_stock_coverage_qty, 40)
	self.assertEqual(allocated["SOI-LATE"].available_to_reserve, 15)
	self.assertEqual(allocated["SOI-LATE"].finished_stock_coverage_qty, 25)
	self.assertEqual(allocated["SOI-LATE"].production_required_qty, 15)

def test_finished_stock_pools_are_isolated_by_company_item_and_warehouse(self):
	from process_simplification.api.workbench import allocate_finished_stock

	rows = [
		frappe._dict(company="C1", item_code="FG1", warehouse="W1", delivery_date="2026-08-10", order_creation="2026-08-01", sales_order="SO-1", sales_order_item_idx=1, sales_order_item="SOI-1", pending_qty=10, reserved_qty=0, available_to_reserve=10, active_work_order_qty=0),
		frappe._dict(company="C2", item_code="FG1", warehouse="W1", delivery_date="2026-08-10", order_creation="2026-08-01", sales_order="SO-2", sales_order_item_idx=1, sales_order_item="SOI-2", pending_qty=10, reserved_qty=0, available_to_reserve=10, active_work_order_qty=0),
		frappe._dict(company="C1", item_code="FG2", warehouse="W1", delivery_date="2026-08-10", order_creation="2026-08-01", sales_order="SO-3", sales_order_item_idx=1, sales_order_item="SOI-3", pending_qty=10, reserved_qty=0, available_to_reserve=10, active_work_order_qty=0),
		frappe._dict(company="C1", item_code="FG1", warehouse="W2", delivery_date="2026-08-10", order_creation="2026-08-01", sales_order="SO-4", sales_order_item_idx=1, sales_order_item="SOI-4", pending_qty=10, reserved_qty=0, available_to_reserve=10, active_work_order_qty=0),
	]
	allocated = allocate_finished_stock(rows)
	self.assertEqual([row.finished_stock_coverage_qty for row in allocated], [10, 10, 10, 10])
```

- [ ] **Step 6: Run allocation tests and verify RED**

Expected: FAIL because the workbench module does not yet expose the allocator.

- [ ] **Step 7: Move the allocator into `workbench.py`**

Implement `allocate_finished_stock()` with these rules:

```python
ordered = [frappe._dict(deepcopy(dict(row))) for row in rows or []]
ordered.sort(key=order_item_priority_key)
pool_key = (row.get("company"), row.get("item_code"), row.get("warehouse"))
```

Initialize each duplicated snapshot with `max(existing_pool, available_to_reserve)`, never a sum. For every ordered row, call the existing `calculate_production_quantities()`, subtract only the allocated `available_to_reserve`, and update all derived quantity fields.

- [ ] **Step 8: Allocate before order aggregation**

Refactor `get_fulfillment_overview()` to:

1. Load every open Sales Order and its raw rows.
2. Allocate all rows once with `allocate_finished_stock()`.
3. Group allocated rows by `sales_order`.
4. Call `build_fulfillment_order()` with allocated rows.
5. Sort, attach Production Plan summaries, filter, and paginate exactly once afterward.

- [ ] **Step 9: Run `test_simplified_flow` and verify GREEN**

Run the complete module. Expected: all tests pass.

- [ ] **Step 10: Commit Task 1**

```bash
git add custom_apps/process_simplification/process_simplification/api/workbench.py \
  custom_apps/process_simplification/process_simplification/tests/test_simplified_flow.py
git commit -m "fix(process-simplification): allocate finished stock once in order workbench"
```

---

### Task 2: Make Production Demands Reuse Fulfillment Allocation

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/api/production.py`
- Modify: `custom_apps/process_simplification/process_simplification/tests/test_production_workbench.py`
- Modify: `custom_apps/process_simplification/process_simplification/tests/test_simplified_flow.py`

**Interfaces:**
- Consumes: `workbench.allocate_finished_stock()` only through `get_fulfillment_overview()`.
- Produces: `_allocated_rows_from_fulfillment(fulfillment)` as a flatten-only helper.
- Preserves: `get_allocated_production_row(sales_order, sales_order_item)` for write-action revalidation.

- [ ] **Step 1: Add a failing test proving production does not allocate twice**

```python
def test_production_flattens_fulfillment_rows_without_reallocating(self):
	from process_simplification.api.production import _allocated_rows_from_fulfillment

	fulfillment = {"orders": [{"name": "SO-1", "rows": [{"sales_order": "SO-1", "sales_order_item": "SOI-1", "available_to_reserve": 7, "finished_stock_coverage_qty": 7, "production_required_qty": 3}]}]}
	rows = _allocated_rows_from_fulfillment(fulfillment)
	self.assertEqual(len(rows), 1)
	self.assertEqual(rows[0].available_to_reserve, 7)
	self.assertEqual(rows[0].production_required_qty, 3)
```

The production change that this test catches is reintroducing a second allocator that overwrites the fulfillment result.

- [ ] **Step 2: Run the test and verify RED**

Expected: FAIL or error because the current helper invokes `allocate_finished_stock()` and the minimal fixture lacks raw snapshot fields.

- [ ] **Step 3: Replace the production allocator with flattening**

Remove the local `allocate_finished_stock()` implementation and make `_allocated_rows_from_fulfillment()` return copied rows that only fill missing `sales_order` and `delivery_date` from their parent order. Do not recalculate quantity fields.

- [ ] **Step 4: Update allocator ownership tests**

Move the existing finished-stock allocation behavior assertions from `test_production_workbench.py` to `test_simplified_flow.py`, importing from `workbench`. Keep production tests focused on demand construction and reuse.

- [ ] **Step 5: Run both Python modules and verify GREEN**

Run `test_simplified_flow` and `test_production_workbench`. Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add custom_apps/process_simplification/process_simplification/api/production.py \
  custom_apps/process_simplification/process_simplification/tests/test_production_workbench.py \
  custom_apps/process_simplification/process_simplification/tests/test_simplified_flow.py
git commit -m "refactor(process-simplification): reuse fulfillment allocation in production center"
```

---

### Task 3: Allocate Work Order Materials by Sales Order Delivery Priority

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/api/production_readiness.py`
- Modify: `custom_apps/process_simplification/process_simplification/tests/test_production_readiness.py`

**Interfaces:**
- Consumes: linked `Sales Order Item.name`, `delivery_date`, `idx`, and parent Sales Order `creation`.
- Produces: Work Order fields `order_delivery_date`, `order_creation`, and `sales_order_item_idx`.
- Produces: `work_order_priority_key(plan, work_order) -> tuple`, falling back to the existing plan key only for non-order work.
- Produces: serialized plan field `material_priority_date`, derived from linked order delivery dates for display only.

- [ ] **Step 1: Add failing tests for cross-plan delivery priority**

Create two plan graphs whose Production Plan dates conflict with their Sales Order delivery dates:

```python
early_delivery_late_plan = self._graph(
	plan_name="PP-LATE",
	planned_date="2026-09-01",
	creation="2026-08-02",
	work_orders=[frappe._dict(name="WO-EARLY", production_item="FG", status="Not Started", creation="2026-08-02", required_items=[])],
	required_items=[frappe._dict(parent="WO-EARLY", item_code="RM", source_warehouse="Stores", required_qty=1, transferred_qty=0)],
)
late_delivery_early_plan = self._graph(
	plan_name="PP-EARLY",
	planned_date="2026-08-01",
	creation="2026-08-01",
	work_orders=[frappe._dict(name="WO-LATE", production_item="FG", status="Not Started", creation="2026-08-01", required_items=[])],
	required_items=[frappe._dict(parent="WO-LATE", item_code="RM", source_warehouse="Stores", required_qty=1, transferred_qty=0)],
)
early_delivery_late_plan.work_orders_by_name["WO-EARLY"].update(order_delivery_date="2026-08-10", order_creation="2026-08-02", sales_order="SO-EARLY", sales_order_item="SOI-EARLY", sales_order_item_idx=1)
late_delivery_early_plan.work_orders_by_name["WO-LATE"].update(order_delivery_date="2026-08-20", order_creation="2026-08-01", sales_order="SO-LATE", sales_order_item="SOI-LATE", sales_order_item_idx=1)
```

With one stock unit, assert `WO-EARLY` receives it even though its Production Plan date is later. Add separate tests proving a missing delivery date loses to a dated Work Order and equal dates use stable order metadata.

- [ ] **Step 2: Run the new readiness tests and verify RED**

Expected: FAIL because allocation still sorts plans by `planned_date`.

- [ ] **Step 3: Implement Work Order priority metadata and global execution ordering**

Query all linked Sales Order Items in one batch for `name`, `parent`, `delivery_date`, and `idx`, then all parent Sales Orders in one batch for `name` and `creation`. Enrich every linked Work Order before graph construction.

Build a global list of `(plan, work_order)` execution units and sort it by:

```python
(
	work_order_priority_key(plan, work_order),
	-int(work_order.get("bom_level") or 0),
	str(work_order.get("creation") or ""),
	str(work_order.get("name") or ""),
)
```

This keeps every earlier-delivery order ahead of later orders and processes the deepest subassembly first inside the same order demand.

- [ ] **Step 4: Compare supply dates to order delivery date**

Replace the late-supply comparison against `plan.planned_date` with the current Work Order's `order_delivery_date`. A supply without a delivery-date target remains visible but does not create a false on-time promise.

- [ ] **Step 5: Preserve plan summaries and expose display metadata**

After global allocation, summarize every graph as before. Serialize `material_priority_date` as the earliest non-empty linked `order_delivery_date`; keep `planned_date` as the Production Plan start date.

- [ ] **Step 6: Run all readiness tests and verify GREEN**

Run `test_production_readiness`. Expected: PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add custom_apps/process_simplification/process_simplification/api/production_readiness.py \
  custom_apps/process_simplification/process_simplification/tests/test_production_readiness.py
git commit -m "fix(process-simplification): prioritize materials by order delivery"
```

---

### Task 4: Clarify Allocation Semantics in Both Pages

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/process_simplification/page/order_workbench/order_workbench.js`
- Modify: `custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/production_workbench.js`
- Modify: `custom_apps/process_simplification/process_simplification/tests/js/order_fulfillment_overview.test.js`
- Modify: `custom_apps/process_simplification/process_simplification/tests/js/production_workbench.test.js`

**Interfaces:**
- Consumes: allocated `available_to_reserve`, total `finished_stock_coverage_qty`, Production Plan `planned_date`, and `material_priority_date`.
- Produces: user-visible labels “优先获配成品”, “计划开始”, and “物料优先依据：订单交付日期”.

- [ ] **Step 1: Add failing frontend assertions**

Update fixtures with `material_priority_date: "2026-08-10"` and assert both pages contain:

```javascript
assert.match(html, /优先获配成品/);
assert.match(html, /计划开始/);
assert.match(html, /物料优先依据/);
assert.match(html, /订单交付日期/);
assert.doesNotMatch(html, /Production Plan 的计划日期优先分配/);
```

Also change late-supply assertions from “晚于计划日期” to “晚于订单交期”.

- [ ] **Step 2: Run both Node test files and verify RED**

```bash
node --test \
  custom_apps/process_simplification/process_simplification/tests/js/order_fulfillment_overview.test.js \
  custom_apps/process_simplification/process_simplification/tests/js/production_workbench.test.js
```

Expected: FAIL on the new wording.

- [ ] **Step 3: Update order workbench labels**

Rename row-level “可用成品” to “优先获配成品”. Change Production Plan text from “计划优先日期” to “计划开始”, and add `物料优先依据：订单交付日期 <material_priority_date>`.

- [ ] **Step 4: Update production center labels and notes**

Rename quantity explanation “可用成品” to “优先获配成品”. Show both “计划开始” and “物料优先依据：订单交付日期”. Replace page and section notes so they state that customer-order stock and material competition use Sales Order Item delivery dates. Change supply warning to “晚于订单交期”.

- [ ] **Step 5: Run both Node test files and verify GREEN**

Expected: PASS with no warnings.

- [ ] **Step 6: Commit Task 4**

```bash
git add custom_apps/process_simplification/process_simplification/process_simplification/page/order_workbench/order_workbench.js \
  custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/production_workbench.js \
  custom_apps/process_simplification/process_simplification/tests/js/order_fulfillment_overview.test.js \
  custom_apps/process_simplification/process_simplification/tests/js/production_workbench.test.js
git commit -m "fix(process-simplification): explain delivery-priority allocation"
```

---

### Task 5: Regression and Live Reconciliation

**Files:**
- Modify only if a failing regression test exposes an in-scope defect in the files listed above.

**Interfaces:**
- Verifies: Python behavior, frontend rendering, current site responses, and unchanged official ERPNext source.

- [ ] **Step 1: Run targeted Python regression modules**

```bash
docker compose exec -T --workdir /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_simplified_flow
docker compose exec -T --workdir /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_production_workbench
docker compose exec -T --workdir /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_production_readiness
```

Expected: all targeted modules pass.

- [ ] **Step 2: Run the full custom-app Python suite**

```bash
docker compose exec -T --workdir /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification
```

Expected: PASS. If unrelated pre-existing failures occur, record the exact module and error separately rather than changing unrelated code.

- [ ] **Step 3: Run the full frontend suite and build**

```bash
node --test custom_apps/process_simplification/process_simplification/tests/js/*.test.js
docker compose exec -T --workdir /workspace/erpnext/development/frappe-bench frappe bench build --app process_simplification
```

Expected: all Node tests pass and assets build successfully.

- [ ] **Step 4: Reconcile current live orders through read-only APIs**

Call `get_fulfillment_overview()` and `get_production_overview()` within two seconds, then compare common Sales Order Items. Assert identical values for:

```text
reserved_qty
available_to_reserve
finished_stock_coverage_qty
production_required_qty
active_work_order_qty
unplanned_production_qty
```

Verify the current shared finished-goods example allocates 511 to `SAL-ORD-2026-00004`, the remaining 1,489 to `SAL-ORD-2026-00006`, preserves `SAL-ORD-2026-00008`'s actual reservation, and assigns no duplicate free stock to later orders.

- [ ] **Step 5: Verify repository scope and diff hygiene**

```bash
git diff --check
git status --short
git diff --name-only HEAD~4..HEAD
```

Expected: only plan, tests, and `custom_apps/process_simplification` files changed; `.codex/config.toml` remains an unrelated uncommitted user modification.

- [ ] **Step 6: Record final verification commit if test-only corrections were needed**

```bash
git add custom_apps/process_simplification
git commit -m "test(process-simplification): verify unified order allocation"
```

Skip this commit when no additional files changed after Tasks 1–4.
