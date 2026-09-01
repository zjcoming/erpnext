const test = require("node:test");
const assert = require("node:assert/strict");

const {
	shortageRowsHtml,
	shortageSummary,
	filterShortageRows,
	canCreateMaterialRequest,
	shortagePageHtml,
} = require("../../process_simplification/page/shortage_purchase_planning/shortage_purchase_planning.js");

const escapeHtml = (value) =>
	String(value ?? "")
		.replaceAll("&", "&amp;")
		.replaceAll("<", "&lt;")
		.replaceAll(">", "&gt;")
		.replaceAll('"', "&quot;")
		.replaceAll("'", "&#39;");

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

	assert.match(html, /打开即汇总全部未完成生产需求（含未排产订单）/);
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
