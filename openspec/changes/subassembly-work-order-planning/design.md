## Context

`create_work_order` (`api/actions.py`) built one Work Order for the finished good with `use_multi_level_bom=True`. ERPNext explodes raw materials into that single Work Order, but does not create Work Orders for self-made semi-finished goods. For multi-level BOMs, each in-house sub-assembly level needs its own Work Order.

ERPNext's Production Plan (`erpnext/manufacturing/doctype/production_plan/`) already does this: `SubAssemblyService.get_sub_assembly_items` recursively explodes the BOM into sub-assembly rows, and `WorkOrderCreationService.make_work_order` creates a Work Order for the finished good and one per in-house sub-assembly level. The engine methods operate on a Production Plan document instance and its child tables (`po_items`, `sub_assembly_items`).

## Goals

1. One action creates finished-good + all in-house sub-assembly Work Orders.
2. Reuse the Production Plan engine; do not re-implement BOM recursion.
3. Preserve the custom delivery-priority allocation as the source of the net production quantity.
4. Keep Sales Order back-references on every created Work Order.

## Decisions

### Reuse the engine via a persisted, single-demand Production Plan

The adapter (`api/production_plan_adapter.py::create_work_orders_via_production_plan`) builds `frappe.new_doc("Production Plan")` with one `po_items` row for the finished good:

- `item_code`, `bom_no`, `planned_qty` (= delivery-priority net qty from the caller), `planned_start_date`, `warehouse` (finished-goods), `sales_order`, `sales_order_item`, `include_exploded_items=1`.

Doc-level flags:

- `skip_available_sub_assembly_item = 1` with `sub_assembly_warehouse = <resolved source warehouse>` — a sub-assembly level already covered by stock in that warehouse is not re-built.
- `combine_sub_items = 0` — combining aggregates sub-assembly rows and drops `sales_order` / `sales_order_item`; leaving it off preserves the Sales Order link on each sub-assembly Work Order (`services/sub_assembly.py` sets `sales_order` only when not combining).

Flow: `insert()` (persist so `make_work_order` can stamp `production_plan` back-references) → `get_sub_assembly_items()` → `make_work_order()`. Created Work Orders are then read back by `production_plan` filter.

**Why persist:** the engine stamps `production_plan = self.doc.name` on each Work Order. An unsaved plan leaves that link null and loses traceability, so the plan is inserted before driving the engine.

### Keep the delivery-priority allocation authoritative

`create_work_order` still runs its full pre-write rechecks unchanged: `_row_from_workbench`, then `get_allocated_production_row` to re-derive `unplanned_production_qty` after cross-order finished-stock allocation, plus BOM and warehouse validation. Only the final "build one Work Order" section is replaced by the adapter call, passing `planned_qty = <allocated net qty>`. The engine never re-derives demand.

### Mute engine messages

The Production Plan engine emits English `frappe.msgprint` ("N created", "No Work Orders were created", sufficiency warnings) and forces them through with `mute_messages = False`. The adapter wraps `get_sub_assembly_items()` / `make_work_order()` in a context manager that temporarily replaces `frappe.msgprint` with a no-op. The workbench then shows a Chinese summary using the returned counts.

## Risks and mitigations

- **Lost Sales Order link on sub-assembly Work Orders** → `combine_sub_items = 0`; integration test asserts both finished-good and sub-assembly Work Orders carry `sales_order` / `sales_order_item`.
- **`Production Plan.validate_sales_orders` throws** when a linked `sales_orders` table has no producible items → do not populate the `sales_orders` table; only set `sales_order` on the manually-appended `po_items` row, which bypasses that validation. (The `po_items.sales_order` link still requires a real submitted Sales Order.)
- **Engine English messages leak to simplified UI** → muted in the adapter.
- **Persisted Production Plan records accumulate** → kept for traceability this change; cleanup deferred.

## Verification

- Unit tests (mock `frappe.new_doc`): adapter builds the correct `po_items` row and sub-assembly flags, mutes msgprint, reports created Work Orders and sub-assembly count.
- Integration test (fresh company, real multi-level BOM finished good → self-made sub-assembly → raw material, real submitted Sales Order): one call creates ≥2 Work Orders; both finished-good and sub-assembly Work Orders link back to the Sales Order; `sub_assembly_count == 1`.
- Regression: `test_simplified_flow` create-work-order recheck tests stay green (allocation layer intact); the updated `test_quick_order_v2` BOM-snapshot test asserts the adapter receives the Sales Order Item BOM over `get_default_bom`.
- Manual: on a finished good with a multi-level BOM containing a self-made sub-assembly, click "创建生产任务" in the production workbench and confirm finished-good + sub-assembly Work Orders are created and linked to the order; a level with pre-existing sub-assembly stock is not rebuilt.
