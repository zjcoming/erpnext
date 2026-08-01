# Process Simplification

ERPNext custom app for a guided small-manufacturer workflow. It keeps Sales Order, stock reservation,
BOM, Work Order, Material Request and Delivery Note as standard ERPNext documents, while providing a
smaller owner-facing entry and fulfillment workbench.

## Quick Sales Order v2

The quick page is intentionally limited to routine orders:

- Required: customer, one order-level delivery date, product, quantity greater than zero and transaction
  unit price greater than zero.
- Optional: customer purchase-order number and one order remark.
- Derived by the server: company, currency, price list suggestion, UOM, warehouse, reservable finished-goods
  quantity, production demand and BOM snapshot.
- Not supported: per-line dates or warehouses, duplicate product rows, product bundles, variants, serial or
  batch selection, subcontracting, customer-supplied material, special currency/tax/UOM payloads and other
  advanced Sales Order options. Use the standard Sales Order form for these cases.

The lightweight preview is informative only. `确认下单` always runs a complete server preflight, and submit
repeats mutable checks. A change in stock, BOM, shortage or commercial validation requires confirmation again.
Submitting the Sales Order does not reserve stock and does not create Work Orders or purchase documents.

The removed “允许分批发货” checkbox is not replaced by a custom policy. Standard ERPNext fulfillment behavior
continues to apply; no partial-delivery text is written into terms.

## Master-data prerequisites

Before enabling the page, configure:

1. A permitted default Company with currency and a leaf default finished-goods Warehouse.
2. Sales-enabled Items with Stock UOM and, preferably, an Item Default warehouse and selling Item Price.
3. A submitted, active, default BOM for every item that may need production. Stock-covered items may omit it.
4. Customer masters, Selling Settings and credit limits appropriate for standard Sales Order submission.
5. ERPNext stock reservation if reservable coverage is expected in the preview.

## Rollout and rollback

The feature is off unless the site explicitly enables it:

```text
bench --site <site> set-config process_simplification_quick_order_v2_enabled true
bench --site <site> clear-cache
```

Enable it on a pilot site first and compare submitted orders with the standard Sales Order form and fulfillment
workbench. To roll back, set the flag to `false`, clear cache and direct users to standard Sales Order entry.
Existing orders need no migration or rollback because the quick page creates only standard submitted Sales Orders.

Completed `Quick Order Idempotency` records are retained for 30 days and cleaned by a daily scheduler. They contain
only the requesting user, request key/digest, status, result Sales Order and timestamps; they are not business
documents. Review tokens expire after 15 minutes.
