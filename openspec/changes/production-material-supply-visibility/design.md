## Context

`calculate_material_coverage` (`api/shortage.py`) reports, per raw material, how much open Material Request (`open_material_request_qty`) and open Purchase Order (`open_purchase_order_qty`) quantity exists, plus a `status` code (`ready_now` / `awaiting_purchase_receipt` / `purchase_request_pending` / `new_purchase_required`). The quantities came from `_mr_outstanding` / `_po_outstanding`, which ran a query and returned only the summed outstanding quantity. The production workbench (`production_workbench.js`) rendered those numbers but had no way to show which documents they came from or their state.

## Goals

1. Show the specific Material Request / Purchase Order documents behind each material's open-supply totals, with status and outstanding quantity, linkable to the standard form.
2. Do not change the outstanding totals, the material status, purchase consolidation, or shortage calculation.

## Decisions

### Detail functions as the single source, sums derived from them

Add `_mr_documents` / `_po_documents` that reuse the exact `where` conditions of the previous outstanding queries but select document-level columns (`name`, `status`, `stock_qty`/`ordered_qty` or `received_qty`/`conversion_factor`, `schedule_date`) and return one row per outstanding document:

```
{ doctype, name, status, outstanding_qty, schedule_date }
```

`_mr_outstanding` / `_po_outstanding` become thin sums over these detail rows. This guarantees the totals are byte-for-byte the same computation as before (same filter, same per-line outstanding formula), so no existing quantity assertion changes.

`calculate_material_coverage` calls the detail functions once per material, sets the two summary quantities from the detail sums, and attaches `supply_documents = sorted(mr_docs + po_docs, key=schedule_date, doctype, name)`.

### Frontend: an expandable document row per material

Each material row that has `supply_documents` renders an extra full-width row listing each document as a link to `/app/<slug>/<name>` with a type label (采购申请 / 采购单), status pill, outstanding quantity, and schedule date. A material with no documents shows a "尚未发起采购" hint next to its status.

## Risks and mitigations

- **Outstanding totals drift** → totals are now sums of the same detail the queries already computed; an integration test asserts `_mr_outstanding == sum(mr_docs.outstanding_qty)` and the same for PO, and the existing `test_material_coverage_integration` quantity assertions must stay green.
- **Frontend references to `frappe.router` / `frappe.datetime`** → the JS test stubs them; the no-document path avoids these calls entirely.
- **Local `_Test Warehouse` fixture gaps** → the new integration test builds its own company, warehouse, supplier, item, Material Request, and partially-received Purchase Order so it runs without the site-level fixtures.

## Verification

- Unit test (mocked detail functions): `calculate_material_coverage` attaches `supply_documents` and derives `open_material_request_qty` / `open_purchase_order_qty` from them.
- Integration test (fresh company; real submitted Material Request + partially-received Purchase Order): `_mr_documents` / `_po_documents` return the correct documents and outstanding quantities, and the summed outstanding equals the detail totals.
- JS test: material rows render links to the Material Request / Purchase Order forms with status; a material with no documents shows "尚未发起采购".
- Manual: expand a shortage demand in the production workbench and confirm each material lists its Material Request / Purchase Order documents with status and progress, each opening the standard form.
