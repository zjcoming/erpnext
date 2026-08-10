## 1. Supply Document Detail (Backend)

- [x] 1.1 Write failing tests: `_mr_documents` / `_po_documents` return document rows (type, name, status, outstanding qty, schedule date); summed outstanding equals detail totals; `calculate_material_coverage` attaches `supply_documents`.
- [x] 1.2 Add `_mr_documents` / `_po_documents` in `api/shortage.py` reusing the existing outstanding query filters and returning per-document rows.
- [x] 1.3 Reduce `_mr_outstanding` / `_po_outstanding` to sums over the detail rows so totals are unchanged.
- [x] 1.4 Attach `supply_documents` (MR then PO, ordered by schedule date) to each material in `calculate_material_coverage` and derive the open-supply totals from the detail.

## 2. Production Workbench (Frontend)

- [x] 2.1 Render an expandable document list under each material row: links to the Material Request / Purchase Order form, type label, status pill, outstanding quantity, schedule date.
- [x] 2.2 Show a "尚未发起采购" hint for a material with no purchase documents.
- [x] 2.3 Add CSS for the supply-document list and bump the app CSS cache version in `hooks.py`.

## 3. Tests And Regression

- [x] 3.1 Add an integration test (fresh company, real submitted Material Request and partially-received Purchase Order) asserting the returned documents and outstanding quantities, and that summed outstanding equals the detail totals.
- [x] 3.2 Add a JS test asserting material rows render Material Request / Purchase Order links with status, and the no-purchase hint appears when there are no documents.
- [x] 3.3 Keep the existing `test_material_coverage_integration` outstanding-quantity assertions green (totals unchanged); confirm the only failing tests are the pre-existing local warehouse-fixture cases unrelated to this change.
