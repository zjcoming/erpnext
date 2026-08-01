## Context

See `proposal.md` for motivation and `specs/quick-sales-order/spec.md` for behavior. The current custom page collects per-line dates and warehouses, displays raw stock balance, writes the partial-delivery checkbox into Sales Order terms, and creates/submits immediately after a generic browser confirmation. The custom app already has quick-order, shortage, workbench, and guided-action APIs; standard ERPNext Sales Order and Sales Order Item remain the source of truth.

The design must work as a custom app extension, preserve user changes in ERPNext configuration, and avoid ERPNext core modifications. Availability is mutable, Sales Order submission does not itself guarantee reservation, and a browser response can be delayed or retried.

## Goals / Non-Goals

**Goals:**

- Make the routine owner workflow understandable on one screen with five required inputs.
- Give useful availability feedback without running a BOM explosion on every keystroke.
- Make the server authoritative at preflight and submit time, including protection against stale confirmation and duplicate requests.
- Reuse standard ERPNext master data, pricing, permissions, validation, documents, and reservation semantics.
- Keep shortage calculation consistent between quick order and the existing shortage/purchase workflow.

**Non-Goals:**

- Reserving finished goods or materials while merely viewing or submitting the quick order.
- Automatically creating Work Orders, Material Requests, Purchase Orders, Delivery Notes, or stock entries.
- Reproducing every Sales Order option on the quick page.
- Enforcing an all-at-once delivery policy in a custom field or custom fulfillment engine.
- Changing ERPNext core behavior in this change.

## Decisions

### 1. One order-level header and a deliberately small item grid

The header contains required `customer` and `delivery_date`, plus optional `po_no` and a collapsed one-line `remarks` field. The grid contains product, quantity, transaction unit price, calculated amount, fulfillment status, and delete. Company, transaction date, currency, price list, UOM/conversion, warehouse, BOM, item description, and totals are derived.

This reduces contradictory per-line state and makes the quick flow suitable for routine orders. The alternative—keeping advanced fields but hiding them behind expandable sections—still expands validation and support scope and makes it unclear when the standard Sales Order should be used.

### 2. Remove the partial-delivery control rather than model a new policy

The current checkbox is not an ERPNext constraint; it is only rendered into `terms`. The refactor removes it. Standard ERPNext continues to permit partial fulfillment, while an owner can put a customer promise in the ordinary remark or use standard Sales Order entry for structured exceptions.

Adding a custom all-at-once flag would require every downstream reservation, production, delivery, and cancellation action to honor it, creating a cross-flow invariant not justified for the first simplified version.

### 3. Resolve one deterministic context on the server

`get_quick_order_context` resolves the permitted default company, selling settings, currency, default delivery date, and configured finished-goods warehouse. Item lookup returns sales-enabled non-variant items; BOM presence is not a search prerequisite because fully stock-covered products do not need production.

For each item, the server resolves ERPNext item defaults and the app's configured fallback warehouse. If a warehouse cannot be determined, preflight blocks instead of asking the owner to choose one. Pricing should be obtained through ERPNext pricing rules where practical; an Item Price lookup is only a displayed default and never bypasses Sales Order pricing validation.

### 4. Use two check depths and explicit stale state

The lightweight preview is debounced 500–800 ms after product or quantity input stabilizes. A batched endpoint returns item identity/UOM, suggested rate, resolvable warehouse, `available_to_reserve`, `production_required`, and default BOM readiness. It uses ERPNext's stock-reservation availability calculation; raw `get_stock_balance` is not fulfillment coverage.

The deep endpoint accepts the whole normalized order, calculates finished-goods coverage, snapshots the BOM candidates, explodes only the production-required quantities, and invokes the same net-shortage calculation used by the shortage page. It returns line results, aggregate results, `checked_at`, issue codes/messages, and a server-generated `review_token`.

Any customer, delivery date, product, quantity, or price change invalidates the token locally and labels the previous result stale. Customer and price participate because credit, price and customer-status results are part of final confirmation even when they do not change material demand.

### 5. Preflight is authoritative and submission is a second guarded phase

Use two whitelisted commands:

- `preflight_quick_sales_order(payload)` normalizes the supported payload, checks permissions/masters/commercial rules/defaults, computes the deep fulfillment result, constructs an in-memory Sales Order, runs safe standard validations, and returns the confirmation model plus a signed or server-stored short-lived `review_token` bound to the normalized intent and material result.
- `submit_quick_sales_order(payload, review_token, idempotency_key)` locks the action, repeats permission/master/default/availability/BOM/credit/standard validations, and compares the normalized material result with the reviewed token. If it changed, it returns `reconfirmation_required` and a new confirmation model without inserting a Sales Order. If unchanged, it inserts and submits the standard document.

The token should include or reference user, company, customer, delivery date, normalized lines, commercial totals, coverage/BOM/shortage classification, and expiry. It is not a stock lock and must not be described as one. A short-lived server cache is preferable if signing and canonical serialization would be error-prone; either approach must be user-bound and tamper-resistant.

The alternative—create a draft first and confirm afterward—leaves abandoned drafts and still does not protect against changing stock. The alternative—trust the client snapshot—allows stale or manipulated data.

### 6. Idempotency is persisted independently of the browser

The page creates a UUID idempotency key for one intended order and reuses it for retries until the form changes after success/failure resolution. The server persists the key, requesting user, normalized intent digest, status, and resulting Sales Order in a small custom idempotency record or an equivalent uniquely indexed cache-backed record with durable-enough semantics for the deployment.

Concurrent requests for the same key serialize on the unique key. A completed match returns the existing result; reuse with different intent is rejected. In-progress requests return a retryable status. A database savepoint/rollback prevents a key from claiming success when Sales Order submission fails.

Client button disabling alone is retained as feedback but is not considered duplicate protection.

### 7. Warnings and blockers are machine-readable

Backend results use stable issue codes, severity (`warning` or `blocker`), scope (`order` or line identifier), Chinese message, and optional action target. Shortage of finished goods and raw materials is warning-only. Missing/disabled/inaccessible masters, invalid numbers/dates/UOM, duplicate rows, unresolved company/warehouse, production without a usable BOM, disallowed duplicate PO, credit failure, unsupported payload, permission failure, and standard document validation are blockers.

The UI renders messages but does not decide severity. This prevents a future frontend change from accidentally allowing a server blocker.

### 8. Snapshot the BOM on the standard order line

For any line with positive production demand, preflight resolves one active submitted default BOM and submit writes it to `Sales Order Item.bom_no`. The workbench's Work Order creation path prefers `sales_order_item.bom_no`; only legacy orders without a snapshot fall back to the then-current default BOM.

This avoids silent planning drift after a default BOM change without introducing a custom planning record. Fully stock-covered lines may have no BOM.

### 9. UI state is a small explicit state machine

The page uses these observable states: `editing`, `preview_loading`, `preview_current`, `preview_stale`, `deep_checking`, `ready_to_confirm`, `blocked`, `submitting`, and `reconfirmation_required`. Only `ready_to_confirm` can open final confirmation; only the confirmation dialog can submit. The primary button label is `确认下单`, not `创建并提交`, because clicking it begins preflight rather than immediate creation.

The footer is sticky and shows order total, reservable coverage, production quantity, shortage item count, and last check time. It exposes only secondary `检查库存与缺料` and primary `确认下单`. A low-emphasis `使用标准销售订单` link handles advanced cases.

The Open Design reference is `open-design/quick-sales-order/index.html`. It demonstrates the default, stale, checking, blocking, and confirmation interactions; it is a behavior reference, not production Frappe code.

### 10. Keep the custom-app boundary

Implement page composition, API orchestration, issue translation, result hashing/token handling, idempotency, and shared shortage calculation in `process_simplification`. Call standard ERPNext document APIs and validation methods. Do not patch ERPNext core or duplicate stock/credit/pricing calculations when a supported ERPNext API exists.

If implementation proves an essential invariant cannot be achieved through hooks or standard APIs, stop and propose the specific core change separately with upgrade impact and tests.

## Risks / Trade-offs

- [Availability can change immediately after submission because quick order does not reserve stock] → Label values as a check-time view; repeat reservation-aware checks in the fulfillment workbench before reservation or production actions.
- [Deep BOM checks may be slow on large BOMs] → Run only on explicit check/preflight, batch queries, reuse the shared shortage engine, and return line-level progress/loading feedback.
- [Running full Sales Order validation without insertion may have side effects or miss insert-time behavior] → Build the standard document using supported controller methods, keep final insert/submit authoritative, and add integration tests for validation parity.
- [Idempotency storage adds a small custom data object] → Keep it technical and non-authoritative, uniquely indexed, access-controlled, retention-limited, and linked to the standard Sales Order result.
- [A derived warehouse can be surprising in poorly configured sites] → Return the resolved warehouse as read-only supporting text and block when missing; configuration corrections happen outside the quick page.
- [Strict quick-flow boundaries reject some real orders] → Provide a visible route to the standard Sales Order and preserve the user's safe header context when feasible without silently converting advanced payloads.
- [Earlier quick orders encoded partial-delivery text in terms] → Do not migrate or reinterpret old terms; removal affects only newly submitted quick orders.

## Migration Plan

1. Add backend preview, preflight, token, idempotency, and BOM-snapshot behavior behind a site-level feature flag or System Settings field defaulting off.
2. Add automated tests and deploy the backend compatibly while the existing page still uses the old create endpoint.
3. Switch the page to the new contract, enable for a pilot site, and compare created Sales Orders and fulfillment results with standard ERPNext screens.
4. Remove the old partial-delivery and per-line field payload only after the new page is live; retain a temporary server-side rejection message for stale clients.
5. Roll back by disabling the new page/flag and restoring the previous route. Submitted Sales Orders require no data rollback because they remain standard ERPNext documents.

## Open Questions

- Retention duration for completed idempotency records can be set during implementation based on expected order volume; it does not change user-visible behavior.
