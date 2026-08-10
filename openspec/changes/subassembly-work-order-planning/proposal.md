## Why

The production workbench's "创建生产任务" action only created a single Work Order for the finished good, relying on `use_multi_level_bom=True` to explode raw materials into that one Work Order. Factories with self-made semi-finished goods (multi-level BOMs) also need a separate Work Order for each in-house sub-assembly level. Owners had to create those sub-assembly Work Orders manually, which is easy to miss.

ERPNext's standard Production Plan already resolves multi-level BOMs into sub-assembly items and creates a Work Order per in-house level. This change reuses that engine for Work Order creation while keeping the simplified production workbench UI and the custom delivery-priority finished-stock allocation that decides the net production quantity.

## What Changes

- Route `create_work_order` through ERPNext's Production Plan sub-assembly engine so a single action creates the finished-good Work Order plus one Work Order per remaining in-house sub-assembly level.
- Add a thin adapter that builds a persisted Production Plan for one finished-good demand, drives `get_sub_assembly_items()` and `make_work_order()`, and returns the created Work Orders.
- Keep the delivery-priority net production quantity (`get_allocated_production_row`) authoritative; the Production Plan `planned_qty` is fed from it, not recomputed by the engine.
- Skip sub-assembly levels already covered by sub-assembly stock (`skip_available_sub_assembly_item`), checking availability in the resolved production source warehouse.
- Preserve the Sales Order / Sales Order Item back-reference on every created Work Order (`combine_sub_items` left off) so the workbench keeps classifying order-driven demand correctly.
- Persist the Production Plan so created Work Orders keep a `production_plan` back-reference for traceability.
- Suppress the Production Plan engine's English `msgprint` output so it never surfaces in the simplified UI; show a Chinese summary of how many Work Orders (including sub-assemblies) were created.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `order-fulfillment-workbench`: The production Work Order creation action now creates finished-good and multi-level in-house sub-assembly Work Orders in one step via the Production Plan engine, instead of a single finished-good Work Order.

## Impact

- Backend: adds `custom_apps/process_simplification/process_simplification/api/production_plan_adapter.py`; changes `api/actions.py::create_work_order` to call the adapter while preserving all pre-write rechecks and the delivery-priority net-quantity recalculation.
- Frontend: `page/production_workbench/production_workbench.js` success message reports the number of Work Orders created and how many are sub-assemblies.
- ERPNext reuse: `erpnext/manufacturing/doctype/production_plan/services/{sub_assembly,work_order_planning}.py`, `Production Plan`, `Production Plan Item`, `Production Plan Sub Assembly Item`, `Work Order`, `BOM`.
- No ERPNext core changes. No custom production ledger. Persisted Production Plan cleanup is out of scope for this change.

## Non-goals

- Batch cross-order Material Request consolidation (the existing shortage flow already merges selected shortage rows into one Material Request).
- Capacity/scheduling, Job Card reporting, manufacture Stock Entry, material transfer, or subcontracting orchestration.
- A scheduler to clean up persisted Production Plan records (deferred).
