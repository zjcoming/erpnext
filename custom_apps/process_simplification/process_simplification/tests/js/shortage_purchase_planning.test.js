const test = require("node:test");
const assert = require("node:assert/strict");

const {
	shortageRowsHtml,
	shortageSummary,
	filterShortageRows,
	canCreateMaterialRequest,
	shortagePageHtml,
	normalizedPurchaseQuantity,
	preparePurchaseRows,
} = require("../../process_simplification/page/shortage_purchase_planning/shortage_purchase_planning.js");

const escapeHtml = (value) =>
	String(value ?? "")
		.replaceAll("&", "&amp;")
		.replaceAll("<", "&lt;")
		.replaceAll(">", "&gt;")
		.replaceAll('"', "&quot;")
		.replaceAll("'", "&#39;");

test("purchase input removes arithmetic residue and never renders an exponent", () => {
	const rows = preparePurchaseRows([
		{ item_code: "NO-GAP", shortage_qty: 7.247535904753022e-13, quantity_precision: 2 },
		{ item_code: "BUY", shortage_qty: 4.949999999999996, quantity_precision: 2 },
	]);
	assert.equal(rows.length, 1);
	assert.equal(rows[0].purchase_qty, 4.95);
	const html = shortageRowsHtml(rows, { escapeHtml, formatQty: String });
	assert.match(html, /value="4\.95"/);
	assert.match(html, /step="0\.01"/);
	assert.doesNotMatch(html, /value="[^"]*e[-+]\d/i);
});

test("purchase input respects the supplied precision and preserves explicit zero", () => {
	const row = { shortage_qty: 0.000001, quantity_precision: 6 };
	assert.equal(normalizedPurchaseQuantity(row.shortage_qty, row), 0.000001);
	assert.equal(preparePurchaseRows([{ ...row, purchase_qty: 0 }])[0].purchase_qty, 0);
	const html = shortageRowsHtml([row], { escapeHtml, formatQty: (value, precision) => Number(value || 0).toFixed(precision) });
	assert.match(html, /value="0\.000001"/);
	assert.match(html, /step="0\.000001"/);
	assert.match(html, /<strong>0\.000001<\/strong>/);
});

test("shortage rows use required quantity and escape every source label", () => {
	const html = shortageRowsHtml(
		[
			{
				item_code: "RM-1",
				item_name: "原料<img src=x onerror=alert(1)>",
				warehouse: "Stores<script>alert(1)</script>",
				required_qty: 8,
				available_qty: 1,
				open_material_request_qty: 2,
				open_purchase_order_qty: 3,
				shortage_qty: 2,
				sources: [
					{
						sales_order: "SO<script>alert(1)</script>",
						sales_order_item: "SOI-1",
						finished_item: "FG<img src=x onerror=alert(1)>",
						production_plan: "PP-1",
						work_order: "WO-1",
						required_qty: 6,
					},
				],
			},
		],
		{
			escapeHtml,
			formatQty: (value) => Number(value || 0).toFixed(2),
		},
	);

	assert.doesNotMatch(html, /SOI-1/);
	assert.match(html, /生产计划/);
	assert.match(html, /工单/);
	assert.match(html, /PP-1/);
	assert.match(html, /WO-1/);
	assert.match(html, /6\.00/);
	assert.match(html, /Stores&lt;script&gt;/);
	assert.doesNotMatch(html, /source\.qty/);
	assert.doesNotMatch(html, /<script>/);
	assert.doesNotMatch(html, /<img src=x/);
});

test("shortage rows show a readable material name before the labeled code", () => {
	const html = shortageRowsHtml(
		[{ item_code: "204001004", item_name: "PA6德尔隆", shortage_qty: 2 }],
		{
			escapeHtml,
			formatQty: (value) => Number(value || 0).toFixed(2),
			translate: (message) => message,
		}
	);

	assert.ok(html.indexOf("PA6德尔隆") < html.indexOf("物料编码：204001004"));
	assert.match(html, /href="\/app\/item\/204001004"/);
});

test("material request action follows the current role's create permission", () => {
	assert.equal(canCreateMaterialRequest({ can_create: (doctype) => doctype === "Material Request" }), true);
	assert.equal(canCreateMaterialRequest({ can_create: () => false }), false);
	assert.equal(canCreateMaterialRequest(null), false);
});

test("page opens as an all-shortage workbench without an order-read step", () => {
	const html = shortagePageHtml({
		escapeHtml,
		translate: (message) => message,
	});

	assert.match(html, /已扣除库存和在途采购/);
	assert.match(html, /搜索物料、订单、工单或仓库/);
	assert.match(html, /data-action="refresh"/);
	assert.doesNotMatch(html, /data-field="sales_order"/);
	assert.doesNotMatch(html, /读取订单/);
	assert.doesNotMatch(html, /检查缺料/);
});

test("summary counts unique sales orders and source warehouses", () => {
	const summary = shortageSummary([
		{
			warehouse: "Stores - A",
			sources: [{ sales_order: "SO-1" }, { sales_order: "SO-2" }],
		},
		{
			warehouse: "Stores - A",
			sources: [{ sales_order: "SO-1" }],
		},
		{
			warehouse: "Stores - B",
			sources: [],
		},
	]);

	assert.deepEqual(summary, { itemCount: 3, salesOrderCount: 2, warehouseCount: 2 });
});

test("search covers material, warehouse, sales order, and production documents", () => {
	const rows = [
		{
			item_code: "RM-1",
			item_name: "尼龙原料",
			warehouse: "原材料仓 - A",
			sources: [{ sales_order: "SO-001", work_order: "WO-001" }],
		},
		{
			item_code: "RM-2",
			item_name: "纸箱",
			warehouse: "包装仓 - A",
			sources: [{ production_plan: "PP-002" }],
		},
	];

	assert.deepEqual(filterShortageRows(rows, "尼龙"), [rows[0]]);
	assert.deepEqual(filterShortageRows(rows, "so-001"), [rows[0]]);
	assert.deepEqual(filterShortageRows(rows, "PP-002"), [rows[1]]);
	assert.deepEqual(filterShortageRows(rows, "包装仓"), [rows[1]]);
	assert.deepEqual(filterShortageRows(rows, ""), rows);
});
