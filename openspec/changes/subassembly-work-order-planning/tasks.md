## 1. Production Plan Adapter

- [x] 1.1 Write failing unit tests for a `create_work_orders_via_production_plan` adapter: builds one finished-good `po_items` row with delivery-priority net qty and Sales Order link, sets `skip_available_sub_assembly_item`, `sub_assembly_warehouse`, and `combine_sub_items=0`, and drives the engine only after persisting the plan.
- [x] 1.2 Implement `api/production_plan_adapter.py`: build and persist the Production Plan, call `get_sub_assembly_items()` then `make_work_order()`, mute engine `msgprint`, and return `{production_plan, work_orders, sub_assembly_count}`.
- [x] 1.3 Verify the adapter mutes the engine's English messages and reports created Work Orders with the sub-assembly count.

## 2. Work Order Action Integration

- [x] 2.1 Change `api/actions.py::create_work_order` to keep all pre-write rechecks (`_row_from_workbench`, `get_allocated_production_row` net-qty recalculation, BOM and warehouse validation) and replace only the single-Work-Order build with the adapter call.
- [x] 2.2 Feed the adapter the resolved source warehouse as the sub-assembly stock-check warehouse and the delivery-priority net quantity as `planned_qty`; extend the return payload with `work_orders`, `sub_assembly_count`, and `production_plan` while keeping the `work_order` field.
- [x] 2.3 Remove now-unused imports (`make_work_order`, `now_datetime`) and confirm no circular import between `actions` and `production_plan_adapter`.

## 3. Frontend

- [x] 3.1 Update `page/production_workbench/production_workbench.js` success message to report how many Work Orders were created and how many are sub-assemblies; keep the whitelisted method path unchanged.

## 4. Tests And Regression

- [x] 4.1 Add an integration test (fresh company, real multi-level BOM and submitted Sales Order) asserting finished-good + sub-assembly Work Orders are created and both link back to the Sales Order, with `sub_assembly_count == 1`.
- [x] 4.2 Keep the `test_simplified_flow` create-work-order recheck tests green (delivery-priority allocation intact).
- [x] 4.3 Update the `test_quick_order_v2` BOM-snapshot test to assert the adapter receives the Sales Order Item BOM and the resolved sub-assembly warehouse.
- [x] 4.4 Run the affected unit and integration modules; confirm the only remaining `test_quick_order_v2` failures are the pre-existing local warehouse-fixture issues unrelated to this change.
