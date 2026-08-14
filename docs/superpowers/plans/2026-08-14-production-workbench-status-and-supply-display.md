# Production Workbench Status and Supply Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make production readiness visually distinct and prevent irrelevant, unallocated purchase documents from making a material-ready order look late or dependent on inbound supply.

**Architecture:** Keep backend priority allocation and purchasing calculations unchanged. Add a pure frontend production-status color mapping and filter each material row's existing `supply_documents` at render time: material-ready rows show only documents allocated to the current order, while rows with a current gap retain all documents as shortage evidence.

**Tech Stack:** Frappe Desk page JavaScript, Node built-in test runner, existing ERPNext indicator-pill CSS.

## Global Constraints

- Modify only the production workbench renderer, its frontend tests, and Superpowers documentation.
- Do not change backend material allocation, purchasing aggregation, or database documents.
- Keep delivery-risk color independent from production-status color.
- Follow strict TDD: every new behavior must fail for the expected reason before implementation.

---

### Task 1: Distinguish production state and show only relevant supply documents

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/production_workbench.js`
- Modify: `custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/production_workbench.json`
- Test: `custom_apps/process_simplification/process_simplification/tests/js/production_workbench.test.js`

**Interfaces:**
- Produces: `productionStatusMeta(status) -> { indicator }` with green for `ready_to_start`, blue for active production states, orange for `unplanned`, red for shortage/data blockers, and gray for handoff/overplanned/unknown states.
- Consumes: each material row's `current_gap_qty`, `shortage_qty`, and `supply_documents[].allocated_qty`.
- Renders: only allocated supply documents when `current_gap_qty == 0`; all supply documents when `current_gap_qty > 0`.
- Invalidates: the standard Page's browser cache by advancing its `modified` metadata when the page script changes.

- [ ] **Step 1: Write failing status-color tests**

Export `productionStatusMeta` and assert the agreed mapping. Render an overdue `ready_to_start` demand and assert the HTML contains both a red delivery-risk pill and a green production-status pill.

- [ ] **Step 2: Write failing supply-document relevance tests**

Add cases proving:

- A material-ready row (`current_gap_qty == 0`) hides a Purchase Order whose `allocated_qty == 0`, hides its late label, and does not show `尚未发起采购`.
- A row with a current gap keeps a zero-allocated Purchase Order and labels it `未分配给本单` and `晚于本单交期`.
- An allocated document retains total `未完成` quantity and labels its allocation as `已分配给本单 N`.
- A real purchase shortage with no documents shows `尚未发起采购`, while a no-shortage row without documents does not.

- [ ] **Step 3: Run the focused test and verify RED**

```bash
node --test custom_apps/process_simplification/process_simplification/tests/js/production_workbench.test.js
```

Expected: failures because the main status is always gray, all supply documents render unconditionally, and the old allocation/late labels are still used.

- [ ] **Step 4: Implement the minimal renderer change**

Add the pure status mapping and use it for the main production-status pill without changing the risk pill. Before rendering document links, filter the document list with:

```javascript
const hasCurrentGap = Number(row.current_gap_qty || 0) > 0;
const docs = (row.supply_documents || []).filter(
	(doc) => hasCurrentGap || Number(doc.allocated_qty || 0) > 0
);
```

Use `已分配给本单` when allocation is positive, `未分配给本单` otherwise, and rename the late marker to `晚于本单交期`. Show `尚未发起采购` only when `shortage_qty > 0` and the displayed document list is empty.

Advance `production_workbench.json`'s `modified` timestamp so Frappe's `page_info` synchronization removes existing `_page:production-workbench` browser caches after the Page metadata is reloaded.

- [ ] **Step 5: Run focused and full frontend tests**

```bash
node --test custom_apps/process_simplification/process_simplification/tests/js/production_workbench.test.js
node --test custom_apps/process_simplification/process_simplification/tests/js/*.test.js
```

Expected: all frontend tests exit 0.

- [ ] **Step 6: Run backend regression tests**

```bash
bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_production_workbench
bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_shared_material_allocation
bench --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_material_coverage_integration
```

Expected: backend material allocation and purchasing tests remain green because this change is presentation-only.

- [ ] **Step 7: Verify the live workbench scenario**

Open the production workbench demand for `SAL-ORD-2026-00003` and confirm:

- `可开工` is green while its overdue risk remains red.
- Purchase Orders allocated `0` to the ready material are not displayed.
- A genuinely short material still displays its inbound documents with clear per-order allocation and deadline wording.

Before browser verification, reload the Page metadata and clear the server cache:

```bash
bench --site development.localhost reload-doc process_simplification page production_workbench
bench --site development.localhost clear-cache
```

- [ ] **Step 8: Commit**

```bash
git add custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/production_workbench.js \
  custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/production_workbench.json \
  custom_apps/process_simplification/process_simplification/tests/js/production_workbench.test.js \
  docs/superpowers/plans/2026-08-14-production-workbench-status-and-supply-display.md
git commit -m "fix(process-simplification): clarify production readiness display"
```
