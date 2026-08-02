# Quick Order Material Risk Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only, BOM-organized material-risk section to Quick Sales Order so an owner can understand finished-goods coverage, production demand, raw-material commitments, incoming supply, and suggested new purchase-request quantities before confirming the order.

**Architecture:** Extend the existing shared shortage engine into a material-coverage service that returns every BOM material row, while preserving the current shortage-only adapter used by the purchasing page. Quick-order preflight will attach product-grouped material explanations plus an order-level aggregated procurement summary; the Frappe page will render those results below the product grid and invalidate them through the existing stale-state machine. No ERPNext core code or purchasing document creation is added to Quick Sales Order.

**Tech Stack:** ERPNext/Frappe 17, Python, Frappe Query Builder, standard `Bin`/BOM/Material Request/Purchase Order data, JavaScript/jQuery Frappe Page API, CSS, Frappe `UnitTestCase` and integration tests.

## Global Constraints

- Work only on `rc/develop`; do not commit feature work to `develop`.
- Do not modify ERPNext core files.
- Standard ERPNext documents remain the only source of truth.
- Quick Sales Order remains read-only with respect to stock reservation, production, Material Request, and Purchase Order creation.
- Raw-material shortage is a warning; an unresolved warehouse, required production without a usable BOM, or a failed BOM explosion is a blocker.
- Lightweight preview must not explode BOMs; material detail is calculated only by explicit deep check, confirmation preflight, and final submit recheck.
- Different stock UOM quantities must never be summed into one total quantity.
- Existing quick-order review-token, reconfirmation, and idempotent submission guarantees must remain intact.
- UI copy is Chinese and the layout must remain usable on narrow screens.

---

## File Structure

- Modify `custom_apps/process_simplification/process_simplification/api/shortage.py`: own raw-material stock/supply snapshots, all-material coverage calculation, status classification, and the backward-compatible shortage adapter.
- Modify `custom_apps/process_simplification/process_simplification/api/quick_order.py`: attach grouped material risk to authoritative preflight and include material outcomes in the review fingerprint.
- Modify `custom_apps/process_simplification/process_simplification/process_simplification/page/quick_sales_order/quick_sales_order.js`: render the lower material-risk area, connect it to stale/checking/current states, and enrich confirmation copy.
- Modify `custom_apps/process_simplification/process_simplification/public/css/process_simplification.css`: style product/BOM cards, horizontally scrollable material tables, risk badges, stale overlay, and responsive layout.
- Modify `custom_apps/process_simplification/process_simplification/tests/test_quick_order_v2.py`: unit coverage for stock commitments, all-material results, shared-material aggregation, date-qualified supply, grouped quick-order output, and fingerprint changes.
- Modify `custom_apps/process_simplification/process_simplification/tests/test_quick_order_integration.py`: integration coverage for a production-required order returning BOM material detail without creating operational documents.
- Modify `custom_apps/process_simplification/README.md`: document the new read-only risk detail and its calculation timestamp.
- Modify `openspec/changes/refactor-quick-sales-order/tasks.md`: record the added verification after the implementation passes.

---

### Task 1: Build the reservation-aware material coverage service

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/api/shortage.py:35-128`
- Test: `custom_apps/process_simplification/process_simplification/tests/test_quick_order_v2.py`

**Interfaces:**
- Consumes: `demands: list[dict]`, where each demand contains `bom_no`, `qty`, and a source mapping with `row`, `finished_item`, and production quantity.
- Produces: `get_material_stock_snapshot(item_code: str, warehouse: str | None) -> frappe._dict`.
- Produces: `calculate_material_coverage(demands, company: str, need_by_date: str | None = None, defaults=None) -> frappe._dict` with keys `materials` and `shortages`.
- Preserves: `calculate_material_shortages(demands, company: str, defaults=None, need_by_date: str | None = None) -> list[dict]` as a wrapper returning only `coverage.shortages`.

- [ ] **Step 1: Write failing stock-snapshot tests**

Add tests that patch `frappe.db.get_value("Bin", ...)` and prove commitments use ERPNext Bin buckets rather than raw balance alone:

```python
@patch("process_simplification.api.shortage.frappe.db.get_value")
def test_material_snapshot_deducts_erpnext_commitments(self, get_value):
	from process_simplification.api.shortage import get_material_stock_snapshot

	get_value.return_value = frappe._dict(
		actual_qty=100,
		reserved_qty=10,
		reserved_qty_for_production=20,
		reserved_qty_for_sub_contract=5,
		reserved_qty_for_production_plan=3,
	)

	result = get_material_stock_snapshot("RM-001", "Stores - TC")

	self.assertEqual(result.actual_qty, 100)
	self.assertEqual(result.committed_qty, 38)
	self.assertEqual(result.available_qty, 62)
```

Also prove missing warehouse returns `can_calculate=False`, zero quantities, and no cross-warehouse pooling.

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests \
  --app process_simplification \
  --module process_simplification.tests.test_quick_order_v2
```

Expected: failure because `get_material_stock_snapshot` does not exist.

- [ ] **Step 3: Implement stock snapshot and date-qualified supply helpers**

Read `Bin` by exact item and warehouse and derive:

```python
committed_qty = sum(
	max(normalize_qty(bin_row.get(field)), 0)
	for field in (
		"reserved_qty",
		"reserved_qty_for_production",
		"reserved_qty_for_sub_contract",
		"reserved_qty_for_production_plan",
	)
)
available_qty = max(normalize_qty(bin_row.actual_qty) - committed_qty, 0)
```

Replace `_mr_outstanding` and `_po_outstanding` with signatures that require `item_code`, `warehouse`, `company`, and `need_by_date`. Filter submitted records by matching company and item warehouse. For Material Requests, sum `max(stock_qty - ordered_qty, 0)` only for Purchase requests whose item schedule date is no later than the need date. For Purchase Orders, sum `max(stock_qty - received_qty, 0)` only for item rows due no later than the need date. This keeps the unconverted Material Request balance and Purchase Order balance mutually exclusive.

- [ ] **Step 4: Write failing all-material coverage tests**

Patch BOM explosion, stock snapshots, Material Request balance, and Purchase Order balance. Require the service to return both sufficient and short materials:

```python
result = calculate_material_coverage(
	[{"bom_no": "BOM-FG-001", "qty": 10, "source": {"row": 1, "finished_item": "FG-001"}}],
	"_Test Company",
	need_by_date="2099-01-10",
)

self.assertEqual([row["item_code"] for row in result.materials], ["RM-ENOUGH", "RM-SHORT"])
self.assertEqual([row["item_code"] for row in result.shortages], ["RM-SHORT"])
self.assertEqual(result.materials[0]["status"], "ready_now")
self.assertEqual(result.materials[1]["status"], "new_purchase_required")
```

Add cases for:

- available stock covers current production;
- on-time Purchase Order covers the gap and status is `awaiting_purchase_receipt`;
- unconverted Material Request prevents a duplicate request but status is `purchase_request_pending`, never `ready_now`;
- late Purchase Order does not reduce the suggested new request quantity;
- two finished products sharing one raw material aggregate inventory and supply once while preserving two source contributions with their own `required_qty`;
- a missing source warehouse produces `status="cannot_calculate"` and a blocker indicator in the returned material row.

- [ ] **Step 5: Implement `calculate_material_coverage` and the compatibility wrapper**

For every exploded BOM contribution, append a copied source with the contribution quantity:

```python
source = dict(demand.get("source") or {})
source["required_qty"] = normalize_qty(bom_item.get("qty"))
material["sources"].append(source)
```

Return each material with these stable fields:

```python
{
	"item_code": str,
	"item_name": str | None,
	"stock_uom": str | None,
	"warehouse": str | None,
	"required_qty": float,
	"actual_qty": float,
	"committed_qty": float,
	"available_qty": float,
	"open_material_request_qty": float,
	"open_purchase_order_qty": float,
	"current_gap_qty": float,
	"shortage_qty": float,
	"status": "ready_now" | "awaiting_purchase_receipt" | "purchase_request_pending" | "new_purchase_required" | "cannot_calculate",
	"sources": list[dict],
}
```

Sort by warehouse, item code for deterministic review tokens and tests. Implement `calculate_material_shortages` by delegating to the new service and returning `result["shortages"]` so the existing shortage-purchase page keeps its response contract.

- [ ] **Step 6: Run Task 1 tests and commit**

Run the focused test module. Expected: all tests pass.

```bash
git add custom_apps/process_simplification/process_simplification/api/shortage.py \
  custom_apps/process_simplification/process_simplification/tests/test_quick_order_v2.py
git commit -m "feat: explain reservation-aware material coverage"
```

---

### Task 2: Attach BOM-organized risk details to quick-order preflight

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/api/quick_order.py:490-590`
- Test: `custom_apps/process_simplification/process_simplification/tests/test_quick_order_v2.py`

**Interfaces:**
- Consumes: `calculate_material_coverage(...)` from Task 1.
- Produces in preflight result: `material_groups: list[dict]`, `material_coverage: list[dict]`, and existing `shortages: list[dict]`.
- Extends: `quick_order_review_fingerprint(result)` to bind material quantities and statuses to the review token.

- [ ] **Step 1: Write failing grouped-result and fingerprint tests**

Patch preview rows and the coverage service, then assert the evaluated order maps material sources back to the correct finished-product row:

```python
self.assertEqual(result["material_groups"][0]["item_code"], "FG-001")
self.assertEqual(result["material_groups"][0]["bom_no"], "BOM-FG-001")
self.assertEqual(result["material_groups"][0]["materials"][0]["required_qty"], 20)
self.assertEqual(result["material_coverage"][0]["shortage_qty"], 5)
```

Create two otherwise-identical results whose material `available_qty` differs while shortage item count remains the same; assert `quick_order_review_fingerprint` changes.

- [ ] **Step 2: Run the tests and verify they fail**

Run the same focused test command as Task 1. Expected: missing `material_groups`/`material_coverage` assertions fail.

- [ ] **Step 3: Build demands with stable source identity**

Change quick-order demand sources to include `row`, `finished_item`, `production_qty`, and BOM number. Call:

```python
coverage = calculate_material_coverage(
	demands,
	company,
	need_by_date=data.delivery_date,
	defaults=defaults,
)
```

Map every coverage source back to its preview row. A product group contains product name/code, order quantity, finished-goods warehouse, available-to-reserve quantity, production-required quantity, BOM number, and its BOM material contributions. Preserve `material_coverage` as the order-level aggregate so shared material stock is presented once in the procurement summary.

- [ ] **Step 4: Extend warnings, blockers, and review fingerprint**

Use `coverage.shortages` for `RAW_MATERIAL_SHORTAGE`. If any material has `status="cannot_calculate"`, add a line-scoped `RAW_MATERIAL_WAREHOUSE_MISSING` blocker instead of claiming zero shortage.

Include deterministic material fields in the review fingerprint:

```python
"material_coverage": [
	{
		"item_code": row.get("item_code"),
		"warehouse": row.get("warehouse"),
		"required_qty": normalize_qty(row.get("required_qty")),
		"available_qty": normalize_qty(row.get("available_qty")),
		"open_material_request_qty": normalize_qty(row.get("open_material_request_qty")),
		"open_purchase_order_qty": normalize_qty(row.get("open_purchase_order_qty")),
		"shortage_qty": normalize_qty(row.get("shortage_qty")),
		"status": row.get("status"),
	}
	for row in result.get("material_coverage") or []
],
```

- [ ] **Step 5: Run Task 2 tests and commit**

Run the focused module. Expected: all tests pass.

```bash
git add custom_apps/process_simplification/process_simplification/api/quick_order.py \
  custom_apps/process_simplification/process_simplification/tests/test_quick_order_v2.py
git commit -m "feat: add BOM risk details to quick-order preflight"
```

---

### Task 3: Render the lower BOM material-risk area

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/process_simplification/page/quick_sales_order/quick_sales_order.js:1-605`
- Modify: `custom_apps/process_simplification/process_simplification/public/css/process_simplification.css:1-332`

**Interfaces:**
- Consumes: `result.material_groups`, `result.material_coverage`, `result.shortages`, and `result.checked_at` from Task 2.
- Produces: DOM rendering functions `renderMaterialRisk(result, options)`, `renderMaterialGroup(group)`, `renderMaterialSummary(materials)`, and `setMaterialRiskStale()`.

- [ ] **Step 1: Add the material-risk container and empty state**

Insert the section between the product grid and the existing standard-order guidance/footer:

```html
<section class="quick-material-risk" aria-labelledby="quick-material-risk-title">
	<div class="quick-material-risk-heading">
		<div>
			<h3 id="quick-material-risk-title">生产与物料风险</h3>
			<p>完成库存与缺料检查后，将按产品 BOM 显示本单用料。</p>
		</div>
		<span class="quick-material-risk-time">尚未检查</span>
	</div>
	<div class="quick-material-risk-body quick-material-risk-empty">尚无可用的物料检查结果</div>
</section>
```

The empty state must not trigger BOM queries; it only explains when details appear.

- [ ] **Step 2: Implement escaped, deterministic rendering helpers**

Use `frappe.utils.escape_html` for every server-provided label. Render one product/BOM card per `material_groups` entry. Each card header shows order quantity, currently reservable finished goods, production demand, finished-goods warehouse, and BOM number.

Render its material table with these columns:

```text
物料 | BOM 单耗/本次需求 | 来源仓库 | 账面 | 已占用 | 本单可用 |
采购申请 | 按时在途 | 当前生产缺口 | 建议新增申请 | 结论
```

Map statuses to Chinese without deciding business severity in the browser:

```javascript
const materialStatusCopy = {
	ready_now: { label: __("当前可生产"), indicator: "green" },
	awaiting_purchase_receipt: { label: __("待采购到货"), indicator: "blue" },
	purchase_request_pending: { label: __("已提采购申请"), indicator: "orange" },
	new_purchase_required: { label: __("需新增采购"), indicator: "red" },
	cannot_calculate: { label: __("无法判断"), indicator: "gray" },
};
```

For a shared material, add `本单汇总库存` supporting text so duplicated product cards do not imply that each product independently owns the full available quantity.

- [ ] **Step 3: Render the order-level procurement summary**

Below product cards, render one aggregated row per material/warehouse from `material_coverage`. Default product cards with shortage to expanded and sufficient cards to collapsed. Add a `查看完整用料` toggle for sufficient products.

The summary header states `预计需新增采购 N 项`; do not sum quantities across UOMs. Clicking the footer shortage metric scrolls to and focuses this section.

- [ ] **Step 4: Connect rendering to the existing state machine**

- On `deep_checking`, keep the previous result visible and add `正在重新检查`.
- On successful `runPreflight`, call `renderMaterialRisk(result, { stale: false })`, update the check time, and remove every stale label.
- On `markStale`, preserve the previous table, add an `is-stale` class and `订单已修改，以下结果仅供参考，请重新检查` message, clear the review token, and prevent confirmation.
- On blocked preflight with returned material data, render available explanation and blockers rather than blanking the section.
- On no production demand, render `当前成品库存可覆盖，本单无需展开生产物料`.

- [ ] **Step 5: Enrich confirmation detail**

In `confirmationHtml`, show up to five shortage materials with item, warehouse, current production gap, existing procurement coverage, and suggested new request quantity. If there are more than five, show `另有 N 项，请查看页面下方明细`.

Keep the existing statement that submitting the Sales Order does not create reservations, production tasks, or purchase requests.

- [ ] **Step 6: Add responsive and accessible styling**

Add CSS for:

- product/BOM cards and risk badge colors;
- `.quick-material-table-wrap { overflow-x: auto; }`;
- a minimum table width that preserves readable numeric columns;
- right-aligned tabular numbers;
- sticky first material column on wide tables;
- stale overlay/banner that does not hide the old data;
- focus-visible styles for toggles and the footer shortage link;
- narrow-screen card header wrapping and touch-sized controls.

Do not hide required columns on narrow screens; use horizontal scrolling.

- [ ] **Step 7: Format, inspect, and commit**

Run the repository formatter/checker against the two modified frontend files. Then open Quick Sales Order at desktop and narrow width and verify escaped names, expandable cards, horizontal scrolling, stale state, zero-production state, and the confirmation summary.

```bash
git add custom_apps/process_simplification/process_simplification/process_simplification/page/quick_sales_order/quick_sales_order.js \
  custom_apps/process_simplification/process_simplification/public/css/process_simplification.css
git commit -m "feat: show BOM material risk on quick orders"
```

---

### Task 4: Add integration coverage and complete verification

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/tests/test_quick_order_integration.py`
- Modify: `custom_apps/process_simplification/README.md`
- Modify: `openspec/changes/refactor-quick-sales-order/tasks.md`

**Interfaces:**
- Consumes: final preflight response and standard ERPNext database state.
- Produces: regression proof that the page receives complete material risk without creating operational documents.

- [ ] **Step 1: Write the failing production-required integration test**

Create a stock item with a submitted default BOM, put only part of the finished-good demand in the configured finished-goods warehouse, and set raw-material stock below BOM demand. Call preflight and assert:

```python
self.assertGreater(result["production_required"], 0)
self.assertEqual(result["material_groups"][0]["bom_no"], bom.name)
self.assertTrue(result["material_groups"][0]["materials"])
self.assertGreater(result["material_coverage"][0]["current_gap_qty"], 0)
self.assertGreater(result["material_coverage"][0]["shortage_qty"], 0)
self.assertTrue(result["can_submit"])
```

Count Work Orders, Material Requests, Purchase Orders, and Stock Reservation Entries before and after preflight; assert every count is unchanged.

- [ ] **Step 2: Run the integration test and verify it fails before fixture completion**

Run:

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests \
  --app process_simplification \
  --module process_simplification.tests.test_quick_order_integration
```

Expected: the new material detail assertions fail until the final contract is wired through.

- [ ] **Step 3: Complete fixtures and make the integration test pass**

Use unique test item/BOM names and submit only standard ERPNext fixtures. Ensure teardown cancels/deletes created test documents in dependency order so repeated test runs are stable.

- [ ] **Step 4: Update user-facing documentation**

Document that:

- detailed BOM material risk appears only after deep check;
- values are a timestamped snapshot and do not reserve stock;
- `当前生产缺口`, `已提采购申请`, `按时在途`, and `建议新增采购申请` are distinct;
- actual procurement is handled after Sales Order creation.

- [ ] **Step 5: Run all verification gates**

Run:

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification
openspec validate refactor-quick-sales-order --strict
git diff --check
```

Expected: 0 test failures, strict OpenSpec validation success, and no whitespace errors.

Use the browser to verify with the existing test product:

- finished-good demand exceeds reservable stock;
- material cards follow the product/BOM grouping;
- raw-material rows explain both sufficient and short materials;
- stale state appears after quantity changes;
- a fresh check removes stale state;
- confirmation uses the same shortage numbers;
- no Work Order, Material Request, Purchase Order, or reservation is created by checking.

- [ ] **Step 6: Mark verification and commit**

Check only the OpenSpec tasks actually proven by the commands and browser walkthrough. Then commit:

```bash
git add custom_apps/process_simplification/process_simplification/tests/test_quick_order_integration.py \
  custom_apps/process_simplification/README.md \
  openspec/changes/refactor-quick-sales-order/tasks.md
git commit -m "test: verify quick-order material risk details"
```

---

## Final Review Checklist

- Every design acceptance point has a corresponding unit, integration, or browser verification step.
- Backend returns all BOM material rows, not only shortage rows.
- Shared materials preserve product contributions while inventory and procurement are aggregated once.
- Material Request and Purchase Order quantities are mutually exclusive and constrained by company, warehouse, and need date.
- Review fingerprint changes when material coverage changes even if shortage item count does not.
- The page never creates procurement or production documents.
- The existing lightweight preview remains lightweight.
- The current stale-result bug is eliminated by rendering from authoritative state rather than prepending an orphan label.
- No placeholder requirements remain in this plan.
