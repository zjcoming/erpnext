## Why

The current quick-order page exposes fields that are not meaningful to a small-factory owner, while giving too little confidence about whether finished goods can be fulfilled and whether production has a usable BOM and sufficient material. The refactor should make routine ordering faster without weakening ERPNext's validations or creating a second order and inventory system.

## What Changes

- Reduce quick-order input to five required values: customer, one order-level delivery date, product, quantity, and transaction unit price. Keep customer PO number and one order-level remark optional; derive company, currency, UOM, warehouse, pricing defaults, amounts, and BOM information from ERPNext.
- Remove the quick-page partial-delivery switch, visible warehouse selection, per-line delivery dates, and oversized remarks area. ERPNext's standard partial-delivery behavior remains available after the order is created; exceptional multi-date or all-at-once commitments use the standard Sales Order page.
- Show a compact fulfillment preview on each line using reservable finished-goods quantity, production demand, BOM readiness, and shortage state. Mark the preview stale whenever demand-changing input changes.
- Split checking into a lightweight automatic preview after product/quantity edits and an explicit deep material check. Re-run a complete server-side preflight before confirmation and once more before submitting the Sales Order.
- If the authoritative preflight differs materially from the result the user reviewed, refresh the summary and require confirmation again. Finished-goods or raw-material shortage is normally a warning; invalid masters, required production without an active BOM, invalid commercial data, permissions, credit rules, and standard ERPNext validation remain blockers.
- Create and submit only standard ERPNext Sales Order/Sales Order Item records. Snapshot the selected default BOM on each production-requiring order line and protect submission retries with an idempotency key.
- Route unsupported cases—multiple delivery dates, duplicate product rows, advanced UOM/currency/tax behavior, bundles, subcontracting, serial/batch selection, customer-supplied material, and multi-company ordering—to the standard Sales Order page.
- Add an Open Design prototype as the visual and interaction reference for implementation.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `quick-sales-order`: Replace the existing quick-order contract with a smaller order-level form, staged availability/shortage checks, authoritative preflight and reconfirmation, safe idempotent submission, and explicit routing of complex cases to standard ERPNext.

## Impact

- Frontend: `custom_apps/process_simplification/process_simplification/process_simplification/page/quick_sales_order/quick_sales_order.js` and its page styling/interaction states.
- Backend: `custom_apps/process_simplification/process_simplification/api/quick_order.py`, reusable shortage calculation in `api/shortage.py`, and related workbench/BOM selection behavior.
- Tests: quick-order API and UI behavior in `custom_apps/process_simplification/process_simplification/tests/`.
- ERPNext reuse: Customer, Item, Item Price/Pricing Rule, Sales Order, Sales Order Item, BOM, Stock Reservation Entry availability APIs, credit checks, permissions, and standard document validation.
- No ERPNext core changes and no custom order, stock, production, or purchasing ledger are planned. A core change is allowed only if a proven ERPNext extension point cannot preserve the required validation semantics, and must be proposed separately.
