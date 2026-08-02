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

Detailed BOM material risk appears only after `检查库存与缺料`, during confirmation preflight, or during the
final submit recheck. The result is a timestamped snapshot for decision support; viewing it does not reserve
finished goods or raw materials and does not create production or purchasing documents. Each product/BOM card
keeps these quantities separate:

- `当前生产缺口`: BOM raw-material demand left after available raw material in the resolved source warehouse is
  applied, before open purchase requests or on-time purchase orders are deducted.
- `已提采购申请`: on-time, unconverted Purchase Material Request balance, which avoids proposing a duplicate request.
- `按时在途`: outstanding Purchase Order quantity due by the order's delivery date.
- `建议新增采购申请`: raw-material gap left after available stock, open purchase requests and on-time purchase
  orders are applied.

The quick page explains the risk before the owner confirms the Sales Order. Actual procurement remains a later
action in the order workbench and shortage-purchase flow after the standard Sales Order has been created.

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

## 订单履约总览

`订单履约总览` is the owner-facing, sales-to-delivery risk view. It reads every submitted Sales Order that is not
closed, completed, or fully delivered, recalculates its remaining fulfilment from the standard order workbench data,
and includes direct-stock orders as well as orders with production work. The default order is the earliest outstanding
delivery date first, then the higher risk first; overdue, due-soon, stock/production state, and risk filters narrow the
same read model. Expanding an order shows its mixed item states and existing line-level workbench actions; the route
can focus the selected Sales Order.

Loading, refreshing, filtering, expanding, and exporting the overview are read-only. They never reserve stock, create
Work Orders, create purchase documents, or submit delivery documents. Row-level actions remain explicit in the
workbench and create only the corresponding standard ERPNext documents (for example Stock Reservation Entries, Work
Orders, Material Requests, and draft Delivery Notes); the overview is refreshed afterwards. Access is permission-aware:
users need Sales Order read access to discover the overview and keep the normal ERPNext permissions required by any
action or linked document. The table is horizontally scrollable at narrow desktop/tablet widths, and CSV export reflects
the currently visible orders.

This overview deliberately covers the entire sales-to-delivery risk picture, not just manufacturing. A future
production workbench may focus only on production execution, such as Work Order progress and related stock entries.
Production scheduling and automatic procurement remain outside the scope of both surfaces.
