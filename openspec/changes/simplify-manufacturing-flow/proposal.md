## Why

Small manufacturing teams need ERPNext's inventory, manufacturing, purchasing, and delivery correctness without forcing operators through the full ERPNext workflow surface. This change introduces a simplified manufacturing flow app that keeps ERPNext standard documents as the source of truth while giving small factories fast, guided, low-error order execution.

## What Changes

- Add a simplified quick order entry flow that creates and submits standard Sales Orders from a minimal input surface.
- Add an order fulfillment workbench that shows Sales Order Item level coverage, reservation, production, shortage, completion, and delivery status calculated from ERPNext standard documents.
- Add guided actions to reserve finished goods, create Sales Order linked Work Orders, inspect production tasks, reserve completed finished goods, and create Delivery Note drafts for reserved stock.
- Add batch material shortage checking across selected order lines and create submitted Purchase Material Requests without directly creating Purchase Orders.
- Keep all stock, manufacturing, purchasing, and delivery records in ERPNext standard DocTypes; do not introduce custom inventory, production, purchasing, or delivery ledgers.
- Scope phase one to the first four user-facing areas: quick order entry, order workbench, shortage checking/material request creation, and production task viewing.
- Exclude advanced flows from phase one: product bundles, subcontracting, complex multi-UOM behavior, semi-finished goods allocation across orders, mobile job reporting, piece-rate payroll, and duplicate finished-good rows in the same quick Sales Order.

## Capabilities

### New Capabilities

- `quick-sales-order`: Simplified Sales Order creation and submission for small factory users.
- `order-fulfillment-workbench`: Sales Order Item level order-dedicated stock reservation, production coverage, completion, and delivery workflow.
- `shortage-purchase-planning`: Batch raw material shortage calculation and Purchase Material Request creation for selected order demand.

### Modified Capabilities

- None.

## Impact

- Adds a custom app surface for simplified manufacturing flow pages and server APIs.
- Reads and writes ERPNext standard DocTypes: Sales Order, Sales Order Item, Stock Reservation Entry, Work Order, Material Request, Purchase Order, Purchase Receipt, Stock Entry, and Delivery Note.
- Relies on ERPNext stock reservation being enabled and on Work Orders carrying `sales_order` and `sales_order_item` references.
- Uses Sales Order Item as the primary planning and coverage grain to avoid ambiguous order-level aggregation.
- Requires Chinese-facing validation and action messages for simplified users while preserving ERPNext standard validations as the final authority.
