## 1. App Structure And Setup

- [x] 1.1 Create the "流程简化" custom app/module structure using the repository's Frappe app conventions.
- [x] 1.2 Add the simplified workspace, page routes, role/profile assumptions, and navigation entries for phase-one pages.
- [x] 1.3 Add setup validation for required ERPNext settings and defaults, including stock reservation, company, finished goods warehouse, source warehouse, WIP warehouse, and default BOM availability.
- [x] 1.4 Define shared server-side DTOs/utilities for Sales Order Item workbench rows, quantity summaries, action eligibility, and Chinese validation errors.

## 2. Quick Sales Order

- [x] 2.1 Implement the quick Sales Order server API that accepts customer, item rows, quantities, prices, delivery dates, partial-delivery flag, warehouses, and remarks.
- [x] 2.2 Add duplicate finished-good validation for quick orders and return a Chinese blocking message when the same finished good appears more than once.
- [x] 2.3 Create and submit standard ERPNext Sales Orders after explicit confirmation while preserving ERPNext validation failures.
- [x] 2.4 Build the quick order UI with minimal fields, confirmation flow, validation display, and redirect to the order workbench.
- [x] 2.5 Add tests for successful quick order creation, submitted Sales Order output, duplicate item rejection, and ERPNext validation passthrough.

## 3. Order Workbench Calculations

- [x] 3.1 Implement Sales Order Item based workbench loading for submitted Sales Orders.
- [x] 3.2 Implement pending delivery quantity calculation from Sales Order Item ordered and delivered quantities.
- [x] 3.3 Implement effective Stock Reservation Entry calculation as remaining reserved quantity for the Sales Order Item.
- [x] 3.4 Implement active Work Order coverage calculation using Sales Order Item linked submitted Work Orders and excluding cancelled, closed, stopped, and completed coverage.
- [x] 3.5 Implement completed production and completed-but-unreserved detection from linked Work Orders and Manufacture Stock Entries.
- [x] 3.6 Implement row status and next-action calculation for waiting reservation, waiting production, shortage, waiting start, in production, waiting completion entry, completed waiting reservation, deliverable, and completed states.
- [x] 3.7 Add unsupported-row detection for ambiguous standard ERPNext Sales Orders such as duplicate finished-good rows.
- [x] 3.8 Add tests for reservation, cancellation, delivery, active production, completed production, and unsupported duplicate-row status recalculation.

## 4. Workbench Actions

- [x] 4.1 Implement "预留库存" action that re-reads availability, creates, and submits Stock Reservation Entry records for no more than pending delivery and unreserved stock.
- [x] 4.2 Implement "创建生产任务" action that re-reads coverage, validates uncovered quantity, creates, and submits Sales Order Item linked Work Orders.
- [x] 4.3 Implement "查看生产任务" API/view data for linked Work Orders, required materials, transfer entries, manufacture entries, and current production status.
- [x] 4.4 Implement "预留完工成品" action for completed-but-unreserved finished goods with Sales Order Item precise Stock Reservation Entry creation.
- [x] 4.5 Implement "创建发货单" action using ERPNext reserved-stock delivery mapping and enforce draft-only Delivery Note output.
- [x] 4.6 Add concurrency/race-condition tests that verify actions re-read current reservation and Work Order coverage before submitting.
- [x] 4.7 Add tests for partial reservation, over-reservation rejection, duplicate Work Order prevention, completed-stock reservation, and Delivery Note draft creation.

## 5. Shortage And Purchase Planning

- [x] 5.1 Implement selected-order-line shortage input collection from workbench rows.
- [x] 5.2 Implement BOM expansion for uncovered or active production material demand using ERPNext BOM utilities.
- [x] 5.3 Implement raw material aggregation and deduction of current available stock, unfinished Purchase Material Requests, and unfinished Purchase Orders.
- [x] 5.4 Implement shortage result explanation data showing contributing Sales Orders, Sales Order Items, finished goods, and quantities.
- [x] 5.5 Implement user-adjustable shortage selection and quantity validation.
- [x] 5.6 Implement Purchase Material Request creation and submission from selected shortage rows without creating Purchase Orders.
- [x] 5.7 Include source context in Material Request item descriptions or available note fields without creating a custom purchasing ledger.
- [x] 5.8 Add tests for shortage calculation, no-shortage handling, existing MR/PO deduction, adjusted quantity validation, and submitted Purchase Material Request creation.

## 6. Frontend Workbench Experience

- [x] 6.1 Build the order workbench page with row-level quantities, material status, current status, and next-action buttons.
- [x] 6.2 Build confirmation dialogs for reservation, Work Order creation, completed-stock reservation, Delivery Note draft creation, and Material Request creation.
- [x] 6.3 Build production task viewing UI for linked Work Orders and related material/stock entries.
- [x] 6.4 Build batch shortage checking UI with default selected rows, aggregated material results, source explanation, editable purchase quantities, and Material Request creation.
- [x] 6.5 Ensure all simplified user-facing errors and action labels are Chinese and do not expose raw English ERPNext exceptions without context.
- [x] 6.6 Verify responsive layout for small desktop and tablet-sized factory devices.

## 7. Integration Verification

- [x] 7.1 Verify scenario one: 1000-piece order with 200 available finished goods reserves 200 and creates an 800-piece Work Order.
- [x] 7.2 Verify scenario two: 800-piece Work Order completion results in effective reservation reaching 1000 and the order becoming deliverable.
- [x] 7.3 Verify scenario three: another order cannot reserve stock already reserved for order A and instead creates its own production task.
- [x] 7.4 Verify scenario four: cancelling reservation in standard ERPNext reduces workbench reservation and frees stock for other orders.
- [x] 7.5 Verify scenario five: an order already covered by 1000 pieces of active Work Orders cannot create another Work Order and returns a Chinese message.
- [x] 7.6 Run relevant ERPNext/Frappe unit tests and the new custom app tests.
- [x] 7.7 Manually verify that standard ERPNext pages can continue, cancel, and submit the documents created by the simplified app.
