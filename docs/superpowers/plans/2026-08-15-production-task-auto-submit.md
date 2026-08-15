# Production Task Auto-Submit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the production workbench create and atomically submit the Production Plan, finished-good Work Order, and every in-house sub-assembly Work Order.

**Architecture:** Keep ERPNext's Production Plan engine as the sole multi-level Work Order generator. Wrap the complete lifecycle in one database savepoint, persist and submit the expanded Production Plan before generating Work Orders, then submit every generated Work Order; the whitelisted action checks all required permissions before entering the adapter.

**Tech Stack:** Python 3, Frappe document lifecycle and database savepoints, ERPNext Production Plan/Work Order, Frappe `UnitTestCase` and `IntegrationTestCase`.

## Global Constraints

- Do not use OpenSpec.
- Do not modify Frappe or ERPNext core code; implement only in `process_simplification`.
- Do not bypass create or submit permissions with `ignore_permissions`.
- Do not bypass ERPNext sales-order overproduction validation.
- Any failure must roll back the complete Production Plan and Work Order batch.
- Only submitted Work Orders may count toward production-workbench coverage.

---

## File Structure

- `custom_apps/process_simplification/process_simplification/api/production_plan_adapter.py`: owns the atomic Production Plan expansion, submission, Work Order generation, and Work Order submission lifecycle.
- `custom_apps/process_simplification/process_simplification/api/actions.py`: owns the workbench API permission boundary and response shape.
- `custom_apps/process_simplification/process_simplification/tests/test_production_plan_subassembly.py`: verifies adapter lifecycle, rollback, and real multi-level submitted documents.
- `custom_apps/process_simplification/process_simplification/tests/test_quick_order_v2.py`: verifies permission checks and the existing workbench action contract.

### Task 1: Atomic Production Plan and Work Order lifecycle

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/api/production_plan_adapter.py`
- Test: `custom_apps/process_simplification/process_simplification/tests/test_production_plan_subassembly.py`

**Interfaces:**
- Consumes: `create_work_orders_via_production_plan(*, sales_order, sales_order_item, company, item_code, bom_no, planned_qty, fg_warehouse, sub_assembly_warehouse, delivery_date=None)`.
- Produces: `{production_plan: str, work_orders: list[str], sub_assembly_count: int}`, with the stronger guarantee that the Production Plan and all returned Work Orders have `docstatus == 1`.

- [ ] **Step 1: Write a failing lifecycle unit test**

Add `test_adapter_submits_expanded_plan_and_every_generated_work_order`. Use a `FakePP` that records `insert`, `get_sub_assembly_items`, `save`, `submit`, and `make_work_order`, plus two `FakeWO` documents whose `submit` changes `docstatus` from 0 to 1. Invoke the adapter with these exact values:

```python
result = adapter.create_work_orders_via_production_plan(
	sales_order="SO-001",
	sales_order_item="SOI-001",
	company="_Test Company",
	item_code="FG-001",
	bom_no="BOM-FG-001",
	planned_qty=10,
	fg_warehouse="FG - TC",
	sub_assembly_warehouse="Stores - TC",
	delivery_date=None,
)
```

Patch `_work_orders_for_plan` to return `['WO-FG', 'WO-SA']` and `frappe.get_doc` to return the matching fake Work Order. Assert the plan and both Work Orders have `docstatus == 1`, the result contains both names, and the recorded `plan-submit` event occurs before `work-orders-generate`.

- [ ] **Step 2: Run the lifecycle test and verify RED**

```bash
docker exec -w /workspace/erpnext/development/frappe-bench erpnext-development-frappe-1 \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_production_plan_subassembly \
  --case TestProductionPlanSubassemblyAdapter
```

Expected: FAIL because the current adapter neither saves/submits the expanded Production Plan nor submits generated Work Orders.

- [ ] **Step 3: Write a failing rollback unit test**

Add `test_adapter_rolls_back_batch_when_any_work_order_submit_fails`. Make the second fake Work Order raise `frappe.ValidationError("sub assembly invalid")`, patch `frappe.db.savepoint` and `frappe.db.rollback`, assert the same exception reaches the caller, and assert:

```python
rollback.assert_called_once_with(save_point="production_task_auto_submit")
```

- [ ] **Step 4: Run the rollback test and verify RED**

Run the Task 1 command again. Expected: FAIL because no savepoint rollback exists.

- [ ] **Step 5: Implement the minimal atomic lifecycle**

Add to `production_plan_adapter.py`:

```python
_AUTO_SUBMIT_SAVEPOINT = "production_task_auto_submit"


def _submit_work_orders(work_orders: list[str]) -> None:
	for name in work_orders:
		frappe.get_doc("Work Order", name).submit()
```

Replace the adapter write sequence with:

```python
frappe.db.savepoint(_AUTO_SUBMIT_SAVEPOINT)
try:
	plan.insert()
	with _muted_messages():
		plan.get_sub_assembly_items()
		plan.save()
		plan.submit()
		plan.make_work_order()

	work_orders = _work_orders_for_plan(plan.name)
	_submit_work_orders(work_orders)
except Exception:
	frappe.db.rollback(save_point=_AUTO_SUBMIT_SAVEPOINT)
	raise
```

Remove `plan.flags.ignore_permissions = True` and `insert(ignore_permissions=True)`. Keep input fields, warehouse behavior, message muting, Work Order ordering, and result keys unchanged.

- [ ] **Step 6: Run adapter unit tests and verify GREEN**

Run the Task 1 command. Expected: all `TestProductionPlanSubassemblyAdapter` tests pass.

- [ ] **Step 7: Commit Task 1**

```bash
git add custom_apps/process_simplification/process_simplification/api/production_plan_adapter.py \
  custom_apps/process_simplification/process_simplification/tests/test_production_plan_subassembly.py
git commit -m "feat(process-simplification): atomically submit production tasks"
```

### Task 2: Workbench permission boundary

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/api/actions.py`
- Test: `custom_apps/process_simplification/process_simplification/tests/test_quick_order_v2.py`

**Interfaces:**
- Consumes: `create_work_order(sales_order: str, sales_order_item: str, qty: float | None = None)`.
- Produces: the same response dictionary, but refuses the request before demand lookup or writes unless the user may create and submit both Production Plans and Work Orders.

- [ ] **Step 1: Write a failing permission test**

Add `test_create_work_order_checks_all_create_and_submit_permissions_before_writing`. Patch `frappe.has_permission` with a function that raises `frappe.PermissionError` for `('Production Plan', 'submit')` and returns `True` otherwise. Assert `create_work_order('SO-001', 'SOI-001', 1)` raises, `_row_from_workbench` is not called, and `create_work_orders_via_production_plan` is not called.

- [ ] **Step 2: Run the permission test and verify RED**

```bash
docker exec -w /workspace/erpnext/development/frappe-bench erpnext-development-frappe-1 \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_quick_order_v2 \
  --case TestQuickOrderV2
```

Expected: FAIL because the action currently checks only Work Order create permission.

- [ ] **Step 3: Add the minimal permission gate**

At the beginning of `create_work_order`, before `_row_from_workbench`, add:

```python
for doctype in ("Production Plan", "Work Order"):
	for permission_type in ("create", "submit"):
		frappe.has_permission(doctype, permission_type, throw=True)
```

- [ ] **Step 4: Run focused action tests and verify GREEN**

Run the Task 2 command. Expected: all `TestQuickOrderV2` tests pass; existing tests whose permission mock returns `True` remain compatible.

- [ ] **Step 5: Commit Task 2**

```bash
git add custom_apps/process_simplification/process_simplification/api/actions.py \
  custom_apps/process_simplification/process_simplification/tests/test_quick_order_v2.py
git commit -m "fix(process-simplification): require production submit access"
```

### Task 3: Real multi-level submission regression

**Files:**
- Test: `custom_apps/process_simplification/process_simplification/tests/test_production_plan_subassembly.py`

**Interfaces:**
- Consumes: the strengthened adapter result from Task 1.
- Produces: integration evidence that real ERPNext Production Plan and Work Order documents are submitted and retain order-level traceability.

- [ ] **Step 1: Strengthen the integration assertions**

In `test_creates_work_orders_for_finished_good_and_sub_assembly`, assert:

```python
self.assertEqual(
	frappe.db.get_value("Production Plan", result["production_plan"], "docstatus"),
	1,
)
```

Add `docstatus` to the Work Order query fields and assert every returned row has `docstatus == 1`. Keep the existing assertions for finished-good and sub-assembly items plus sales-order links.

- [ ] **Step 2: Run the real integration module**

```bash
docker exec -w /workspace/erpnext/development/frappe-bench erpnext-development-frappe-1 \
  bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_production_plan_subassembly
```

Expected: PASS with a submitted Production Plan, submitted finished-good Work Order, and submitted sub-assembly Work Order. If ERPNext exposes an additional validation, return to root-cause analysis instead of weakening the assertions.

- [ ] **Step 3: Commit Task 3**

```bash
git add custom_apps/process_simplification/process_simplification/tests/test_production_plan_subassembly.py
git commit -m "test(process-simplification): verify submitted multilevel tasks"
```

### Task 4: Full regression and handoff

**Files:**
- Verify only; do not add production files in this task.

**Interfaces:**
- Consumes: all Task 1–3 commits.
- Produces: fresh backend, frontend, syntax, and diff-hygiene evidence.

- [ ] **Step 1: Run all custom-app backend tests**

```bash
docker exec -w /workspace/erpnext/development/frappe-bench erpnext-development-frappe-1 \
  bench --site development.localhost run-tests --app process_simplification
```

Expected: zero failures.

- [ ] **Step 2: Run frontend tests**

```bash
node --test custom_apps/process_simplification/process_simplification/tests/frontend/*.test.js
```

Expected: zero failures.

- [ ] **Step 3: Run syntax and diff checks**

```bash
python -m compileall -q \
  custom_apps/process_simplification/process_simplification/api/production_plan_adapter.py \
  custom_apps/process_simplification/process_simplification/api/actions.py
git diff --check HEAD~3..HEAD
git status --short --branch
```

Expected: compilation and diff check exit 0. The pre-existing untracked `custom_apps/process_simplification/docs/遗留问题清单.md` remains outside implementation commits unless separately requested.

- [ ] **Step 4: Check acceptance criteria**

Confirm every design acceptance criterion is supported by a passing unit/integration assertion or a retained ERPNext validation. Report that PS-002 replacement-production overage remains unresolved and is not masked by this change.
