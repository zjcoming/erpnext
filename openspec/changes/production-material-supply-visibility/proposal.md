## Why

The production workbench's material risk table showed only summed quantities for `采购申请` (open Material Request) and `在途采购` (open Purchase Order) plus a status code. An owner could not see whether a shortage had actually been ordered, which specific Material Request / Purchase Order documents covered it, what state those documents were in, or open the documents. The backend `_mr_outstanding` / `_po_outstanding` (`api/shortage.py`) summed the rows and discarded the per-document detail.

This change surfaces the underlying purchase documents and their status on each material row, without changing how purchases are consolidated or how shortages are calculated.

## What Changes

- Return per-document detail (document type, name, status, outstanding quantity, schedule date) for the outstanding Material Requests and Purchase Orders behind each material's `采购申请` / `在途采购` totals.
- Attach a `supply_documents` list (Material Requests then Purchase Orders, ordered by schedule date) to every material in the coverage result.
- Keep the summary quantities and material status derived from the same documents, so the existing outstanding totals are unchanged.
- Show the linked documents under each material row in the production workbench, each as a link to the standard Material Request / Purchase Order form with its status and outstanding quantity; show a "尚未发起采购" hint when a material has no purchase documents.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `order-fulfillment-workbench`: The production workbench material risk view now exposes the specific Material Request / Purchase Order documents and their status behind each material's open-supply totals, instead of only summed quantities.

## Impact

- Backend: `api/shortage.py` adds `_mr_documents` / `_po_documents`; `_mr_outstanding` / `_po_outstanding` become sums over those detail rows (identical totals); `calculate_material_coverage` attaches `supply_documents` per material.
- Frontend: `page/production_workbench/production_workbench.js` renders a linked document list under each material row; `public/css/process_simplification.css` styles it (cache version bumped in `hooks.py`).
- ERPNext reuse: `Material Request`, `Material Request Item`, `Purchase Order`, `Purchase Order Item` (same filters as the existing outstanding queries).
- No change to purchase consolidation (`create_material_request` still merges selected shortage rows into one Material Request) and no change to shortage calculation (raw materials still explode through sub-assemblies; delivery-priority prior consumption unchanged).

## Non-goals

- Changing how Material Requests are consolidated across orders.
- Treating self-made sub-assemblies as a separate material layer in shortage calculation.
- Creating or advancing Purchase Orders from the workbench.
