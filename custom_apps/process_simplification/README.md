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

Before using the page, configure:

1. A permitted default Company with currency and a leaf default finished-goods Warehouse.
2. Sales-enabled Items with Stock UOM and, preferably, an Item Default warehouse and selling Item Price.
3. A submitted, active, default BOM for every item that may need production. Stock-covered items may omit it.
4. Customer masters, Selling Settings and credit limits appropriate for standard Sales Order submission.
5. ERPNext stock reservation if reservable coverage is expected in the preview.

## Availability

Quick Sales Order is available by default whenever the app is installed. It does not require a site-level feature
flag or container setting. Existing orders need no migration because the page creates only standard submitted Sales
Orders.

Completed `Quick Order Idempotency` records are retained for 30 days and cleaned by a daily scheduler. They contain
only the requesting user, request key/digest, status, result Sales Order and timestamps; they are not business
documents. Review tokens expire after 15 minutes.

## 订单工作台与生产工作台

`订单工作台` is the owner-facing customer-fulfilment view. It includes every submitted Sales Order that is not closed,
completed, or fully delivered, including stock-only orders that need no production. The collapsed order shows the
earliest delivery date, delivery progress, finished-stock coverage, planned production, unplanned production and the
highest risk. The expanded Sales Order Item rows can reserve current finished stock, reserve available completed output,
create a draft Delivery Note, open the standard Sales Order, or hand a production requirement to `生产工作台`.

`生产工作台` is the phase-one owner production decision view. Its primary row is a Sales Order Item production demand,
not a Work Order, so demand remains visible before any Work Order exists. Finished stock is allocated once across open
orders in delivery-date order before production demand is calculated. Each row explains pending delivery, effective
reservation, currently allocatable finished stock, required production, active Work Order coverage, unplanned quantity,
completed output and output awaiting order reservation. Linked Work Orders remain standard ERPNext documents.

The production workbench expands the effective BOM with ERPNext's standard multi-level BOM rules and shows raw-material
stock, committed quantity, open unconverted Material Requests, outstanding Purchase Orders, current gap and new purchase
gap. Shared raw materials show both the selected demand's contribution and the total current production requirement;
the page does not claim that shared stock belongs exclusively to one order. `处理缺料` opens the separate shortage
purchase page, which recalculates the selected demand and creates a standard Material Request. It never automatically
chooses a supplier or creates a Purchase Order.

Both workbenches are derived views, not business ledgers. Loading, filtering and expanding are read-only. Reserving
stock, creating a Work Order, preparing a Delivery Note or creating a Material Request remains an explicit action with
normal ERPNext permissions and a fresh server-side validation. The production workbench may create or supplement a
standard Work Order, inspect material risk and reserve completed stock back to its source order; it does not perform
material transfer, start work, Job Card reporting, manufacture Stock Entry, capacity scheduling or worker piece/time
payroll. Those shop-floor operations stay in standard ERPNext and a later dedicated worker reporting page.
