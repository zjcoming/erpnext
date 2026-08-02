## 1. Characterization and Shared Contracts

- [x] 1.1 Add characterization tests for the current quick-order APIs, standard Sales Order submission, duplicate-product handling, ERPNext customer-PO validation, and workbench BOM selection before changing behavior.
- [x] 1.2 Define normalized quick-order payload/result schemas and stable Chinese issue codes for preview, blockers, warnings, stale results, reconfirmation, and successful submission.
- [x] 1.3 Extract or adapt the shortage calculation so quick-order deep checks and the existing shortage-purchase page use the same BOM explosion, available-material, and net-shortage rules.

## 2. Server-Side Defaults and Lightweight Preview

- [x] 2.1 Refactor quick-order context resolution to derive permitted default company, currency, price list, default delivery date, and configured finished-goods warehouse without exposing editable advanced fields.
- [x] 2.2 Update product search/default lookup to allow sales-enabled non-variant products without requiring a BOM and to return UOM, pricing suggestion, resolved warehouse, and BOM readiness.
- [x] 2.3 Replace raw stock balance in quick-order fulfillment with ERPNext available-to-reserve quantity and add a batched lightweight preview endpoint that returns coverage and production demand.
- [x] 2.4 Add tests for reserved stock, missing warehouse, disabled/inaccessible products, no-BOM stock-covered items, pricing defaults, UOM conversion, and multiple lines in one preview request.

## 3. Deep Check and Authoritative Preflight

- [x] 3.1 Implement the whole-order deep check using production-required quantities, resolved BOMs, and the shared material-shortage engine; return line and aggregate results with `checked_at`.
- [x] 3.2 Implement strict payload normalization and unsupported-case rejection for duplicate products, line-specific dates/warehouses/remarks, advanced UOM/currency/tax data, bundles, subcontracting, serial/batch selection, customer-supplied materials, and multi-company data.
- [x] 3.3 Implement `preflight_quick_sales_order` with permission/master checks, quantity/rate/date rules, ERPNext customer-PO and credit behavior, deterministic defaults, BOM rules, warning/blocker classification, and safe standard Sales Order validation.
- [x] 3.4 Bind a short-lived tamper-resistant review token to user, normalized order intent, commercial totals, fulfillment/BOM/shortage result, issue classification, and expiry.
- [x] 3.5 Add preflight tests for every blocker/warning boundary, including stock shortage warning, raw-material shortage warning, production without BOM blocker, duplicate PO settings, past delivery date, and unauthorized record access.

## 4. Idempotent Submission and BOM Stability

- [x] 4.1 Add an access-controlled, uniquely indexed technical idempotency record (or equivalent durable mechanism) for key, user, intent digest, state, result Sales Order, timestamps, and retention cleanup.
- [x] 4.2 Implement guarded submission that repeats all mutable validations, compares the current material result with the reviewed token, and returns a refreshed summary without insertion when reconfirmation is required.
- [x] 4.3 Create and submit only standard Sales Order/Sales Order Item records, mapping optional PO number and order remark to standard fields and never encoding the removed partial-delivery control in terms.
- [x] 4.4 Snapshot the resolved BOM into `Sales Order Item.bom_no` when production is required, and update guided Work Order creation to prefer that snapshot with legacy fallback.
- [x] 4.5 Add concurrency and retry tests proving double-clicks, timeouts, same-key concurrent calls, and same-key/different-intent calls cannot create duplicate orders.
- [x] 4.6 Add transaction tests proving validation or submission failure leaves no submitted Sales Order and no falsely completed idempotency result.

## 5. Quick-Order Page Refactor

- [x] 5.1 Rebuild the page header with required customer and order-level delivery date plus optional customer PO number and a compact expandable order remark.
- [x] 5.2 Rebuild the product grid with product, quantity, transaction unit price, calculated amount, fulfillment status, add/delete behavior, and no visible warehouse, per-line date, line remark, or partial-delivery control.
- [x] 5.3 Add 500–800 ms debounced batched previews, request cancellation/sequence guards, loading/error states, and stale-state invalidation after any material or commercial edit.
- [x] 5.4 Add the sticky order summary with total, reservable coverage, production quantity, shortage item count, last-check time, `检查库存与缺料`, `确认下单`, and low-emphasis standard Sales Order route.
- [x] 5.5 Implement the final confirmation dialog, blocker/warning presentation, submit locking, reconfirmation refresh, successful workbench routing, and safe recovery from network errors.
- [x] 5.6 Implement responsive layout, keyboard traversal, focus-visible states, accessible labels/status announcements, and Chinese empty/error/help copy matching the Open Design reference.

## 6. Verification and Rollout

- [ ] 6.1 Add end-to-end tests for routine stock-covered orders, production-required orders, warning-only shortages, stale-result reconfirmation, validation blockers, advanced-case redirection, and optional PO/remark persistence.
- [x] 6.2 Verify created Sales Orders against the standard ERPNext form for company, customer, currency, dates, pricing, UOM, warehouse, BOM snapshot, totals, PO number, remark, docstatus, and permissions.
- [x] 6.3 Add a site-level rollout switch, enable it for a pilot site, and document activation, rollback, idempotency retention, and master-data prerequisites.
- [ ] 6.4 Run the custom app test suite, relevant ERPNext Sales Order/reservation tests, frontend lint/format checks, and `openspec validate refactor-quick-sales-order --strict` before implementation handoff.
- [ ] 6.5 Review the implemented page against `open-design/quick-sales-order/index.html` for default, loading, stale, blocker, warning, confirmation, narrow-screen, and keyboard states. Automated rendering checks pass, but live browser verification is still pending.
- [x] 6.6 Add a production-required integration test proving real submitted BOM material detail, raw-material shortage quantities, warning-only submission eligibility, and zero Work Order, Material Request, Purchase Order, or Stock Reservation Entry creation during preflight.
