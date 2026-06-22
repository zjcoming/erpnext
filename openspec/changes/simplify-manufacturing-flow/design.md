## Context

ERPNext 17 already provides the authoritative documents needed for small factory order execution: Sales Order, Stock Reservation Entry, Work Order, Material Request, Purchase Order, Purchase Receipt, Stock Entry, and Delivery Note. The target users need fewer steps and clearer next actions, not a replacement ERP or a parallel accounting/inventory model.

The custom app will sit above ERPNext as a simplified workflow surface. It will create and read standard documents, expose Chinese-facing guided actions, and recalculate status from standard ERPNext records whenever users return from standard ERPNext pages.

The most important modeling constraint is the planning grain: all order coverage must be calculated at `Sales Order Item.name` level. ERPNext supports this through `Stock Reservation Entry.voucher_detail_no` and `Work Order.sales_order_item`, but some standard ERPNext flows aggregate by Sales Order and item code. Phase one will therefore avoid duplicate finished-good rows in a single quick Sales Order and will add validation/repair actions where standard automation is not precise enough.

## Goals / Non-Goals

**Goals:**

- Provide a simplified Custom App named "流程简化" for small manufacturing teams.
- Keep ERPNext standard documents as the only source of truth for order, stock, production, purchasing, and delivery.
- Let users quickly create a Sales Order, reserve available finished goods, create Sales Order linked Work Orders for uncovered demand, check shortages, create Purchase Material Requests, inspect production tasks, reserve completed finished goods, and create Delivery Note drafts.
- Calculate order status in real time from standard documents instead of storing duplicate operational state.
- Use Chinese validation messages for simplified workflow failures while preserving ERPNext validations as the final guardrail.

**Non-Goals:**

- Do not create custom inventory, production, purchasing, or delivery ledgers.
- Do not create a custom order DocType for phase one.
- Do not auto-submit real-world stock movements or procurement documents: Purchase Order, Purchase Receipt, Material Transfer for Manufacture, Manufacture Stock Entry, and Delivery Note remain draft/manual-submit flows.
- Do not support product bundles, subcontracting, duplicate finished-good rows in one quick Sales Order, mobile job reporting, piece-rate payroll, or complex semi-finished goods allocation in phase one.
- Do not hide or replace ERPNext standard pages; supervisors and advanced users can continue standard ERPNext operations.

## Decisions

### Use ERPNext standard documents as the persistence model

The app will only persist business effects through standard ERPNext DocTypes. Quick actions may submit planning/resource-allocation documents such as Sales Order, Stock Reservation Entry, Work Order, and Material Request after explicit confirmation. Physical movement and purchasing execution documents remain drafts for human confirmation.

Alternative considered: create custom demand, reservation, or production tables. This was rejected because it would require reconciliation with ERPNext ledgers and would break the requirement that users can freely switch between simplified and standard ERPNext pages.

### Use Sales Order Item as the coverage grain

Workbench rows represent Sales Order Item rows, not just product totals. Reservation queries must filter `voucher_type = "Sales Order"`, `voucher_no = sales_order`, and `voucher_detail_no = sales_order_item`. Work Order queries must filter both `sales_order` and `sales_order_item`.

Alternative considered: aggregate by Sales Order and item code. This is simpler but ambiguous when the same item appears multiple times on one order or when ERPNext package/product bundle behavior is involved.

### Calculate effective reservation as remaining reserved quantity

The app will calculate effective finished-goods reservation as:

```text
sum(reserved_qty - delivered_qty - transferred_qty - consumed_qty)
where docstatus = 1
and voucher_type = "Sales Order"
and voucher_detail_no = Sales Order Item.name
```

This avoids treating already delivered or otherwise consumed reservation as still available.

Alternative considered: count all submitted Stock Reservation Entries. This is insufficient because ERPNext keeps delivered reservations as submitted documents with updated quantity fields/status.

### Calculate production coverage from active Work Orders

The app will calculate active production coverage from submitted, not-cancelled, not-closed, not-stopped, not-completed Work Orders linked to the Sales Order Item. Remaining production coverage should use `qty - produced_qty - process_loss_qty`, with non-negative flooring.

Alternative considered: use `Work Order.qty - produced_qty` only. This misses process loss and closed/stopped Work Orders.

### Prefer standard mappers/services, then constrain the result

Where possible, implementation should call ERPNext helpers such as Sales Order to Delivery Note mapping with reserved-stock behavior and Work Order/Stock Reservation services. The simplified app should then trim, validate, or reject results according to the phase-one workflow.

Alternative considered: manually construct all downstream documents. This risks bypassing ERPNext validations, defaulting, serial/batch handling, taxes, warehouses, and permissions.

### Treat completed production reservation as verified, not assumed

ERPNext can reserve finished goods from Manufacture Stock Entries against Sales Orders, but not every path is guaranteed to be strict at Sales Order Item grain. The workbench will detect completed-but-unreserved finished goods and show "完工待预留" with an explicit "预留完工成品" action when needed.

Alternative considered: assume ERPNext auto-reservation is always sufficient. This risks another order seeing completed stock as available if the automatic reservation does not point to the intended Sales Order Item.

### Keep shortage purchasing aggregated but explainable

Shortage checking will aggregate raw material demand for purchasing efficiency. The result must still be traceable to selected Sales Order Items in the simplified UI and, where practical, in Material Request item notes or references.

Alternative considered: create one Material Request row per order-line/material pair. This improves traceability but creates noisy purchasing documents for small factories.

## Risks / Trade-offs

- Duplicate item rows in a Sales Order can make coverage ambiguous -> phase one quick entry rejects duplicate finished-good rows; standard ERPNext-created orders with duplicates should be flagged as unsupported or advanced.
- ERPNext settings may disable stock reservation -> install/setup validation must require stock reservation settings before users can use reservation actions.
- Batch/serial items can require explicit selection -> rely on ERPNext reservation and delivery-note behavior where possible; show standard ERPNext pages for advanced correction.
- Standard ERPNext pages can cancel or modify documents behind the simplified app -> never cache operational status as truth; recalculate from standard documents on load and after every action.
- Material shortage aggregation can hide per-order allocation -> keep an explanation model in API responses and include source order context in Material Request descriptions/notes when creating requests.
- Overproduction or race conditions can occur if two users act on the same order -> re-read Sales Order Item, reservation, and Work Order coverage immediately before submitting Stock Reservation Entry or Work Order.

## Migration Plan

- Add the Custom App and enable it per site after ERPNext 17 is installed.
- Validate required ERPNext settings and defaults: stock reservation enabled, company, finished goods warehouse, WIP/source warehouses, default BOMs, and user roles.
- Roll out phase-one pages to a limited role while keeping ERPNext standard pages available to supervisors.
- No data migration is required because the app writes standard ERPNext documents only.
- Rollback consists of removing users' access to the simplified workspace/API; created ERPNext standard documents remain valid and manageable from ERPNext.

## Open Questions

- Should phase one store a non-ledger shortage calculation snapshot for auditability, or is runtime explanation in the UI and Material Request notes sufficient?
- Which warehouse defaults should be site-wide settings versus per-item/per-company ERPNext defaults?
- Should Delivery Note drafts be created for all currently reserved rows at once or allow the user to choose a partial reserved quantity per Sales Order Item?
- How should unsupported standard ERPNext orders with duplicate finished-good rows be presented: blocked entirely, read-only, or advanced mode?
