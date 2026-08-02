const test = require("node:test");
const assert = require("node:assert/strict");

global.frappe = { pages: { "quick-sales-order": {} } };
global.__ = (message) => message;

const {
	materialStatusMeta,
	buildMaterialRiskView,
	materialRiskHtml,
	confirmationHtml,
	clearRowStaleLabels,
} = require("../../process_simplification/page/quick_sales_order/quick_sales_order.js");

const escapeHtml = (value) =>
	String(value ?? "")
		.replaceAll("&", "&amp;")
		.replaceAll("<", "&lt;")
		.replaceAll(">", "&gt;")
		.replaceAll('"', "&quot;")
		.replaceAll("'", "&#39;");

const helpers = {
	translate: (message, replacements = []) =>
		replacements.reduce(
			(rendered, replacement, index) => rendered.replace(`{${index}}`, replacement),
			message
		),
	escapeHtml,
	formatNumber: (value) => Number(value || 0).toFixed(2),
	formatDate: (value) => value,
	formatCurrency: (value, currency) => `${currency || ""} ${Number(value || 0).toFixed(2)}`.trim(),
};

function coverage(overrides = {}) {
	return {
		item_code: "RM-SHARED",
		item_name: "Shared Material",
		stock_uom: "Kg",
		warehouse: "Stores - TC",
		required_qty: 30,
		actual_qty: 18,
		committed_qty: 3,
		available_qty: 15,
		open_material_request_qty: 4,
		open_purchase_order_qty: 5,
		current_gap_qty: 15,
		shortage_qty: 6,
		status: "new_purchase_required",
		sources: [
			{ row: 1, finished_item: "FG-001", required_qty: 20, bom_qty_per_unit: 2 },
			{ row: 2, finished_item: "FG-002", required_qty: 10, bom_qty_per_unit: 2 },
		],
		...overrides,
	};
}

function group(row, overrides = {}) {
	const total = coverage();
	const contribution = total.sources[row - 1];
	return {
		row,
		item_code: `FG-00${row}`,
		item_name: `Finished Good ${row}`,
		qty: row === 1 ? 12 : 7,
		warehouse: "Finished Goods - TC",
		available_to_reserve: 2,
		production_required: row === 1 ? 10 : 5,
		bom_no: `BOM-FG-00${row}-001`,
		materials: [
			{
				...total,
				required_qty: contribution.required_qty,
				bom_qty_per_unit: contribution.bom_qty_per_unit,
				sources: [contribution],
			},
		],
		...overrides,
	};
}

const fixtureWithSharedMaterial = {
	checked_at: "2026-08-02 10:30:00",
	production_required: 15,
	shortage_item_count: 1,
	material_groups: [group(1), group(2)],
	material_coverage: [coverage()],
	shortages: [coverage()],
};

function fulfillmentRoot(labels) {
	const root = { children: [] };
	root.children = labels.map(({ className, text }) => ({
		className,
		text,
		remove() {
			root.children = root.children.filter((child) => child !== this);
		},
	}));
	root.find = (selector) => {
		assert.equal(selector, ".quick-stale-label");
		return {
			get: () => root.children.filter((child) => child.className === "quick-stale-label"),
		};
	};
	return root;
}

test("fresh preflight result removes every row stale label without removing current fulfillment", () => {
	const root = fulfillmentRoot([
		{ className: "quick-stale-label", text: "待重新检查" },
		{ className: "quick-fulfillment", text: "当前可生产" },
		{ className: "quick-stale-label", text: "待重新检查" },
	]);

	clearRowStaleLabels(root);

	assert.deepEqual(
		root.children.map((child) => child.text),
		["当前可生产"]
	);
});

test("builds product BOM cards and one aggregated shared-material summary", () => {
	const view = buildMaterialRiskView(fixtureWithSharedMaterial);

	assert.equal(view.groups.length, 2);
	assert.equal(view.groups[0].materials[0].bom_qty_per_unit, 2);
	assert.equal(view.summary.length, 1);
	assert.equal(view.summary[0].required_qty, 30);
});

test("maps every backend material status without deriving severity", () => {
	const cases = [
		["ready_now", "当前可生产", "green"],
		["awaiting_purchase_receipt", "待采购到货", "blue"],
		["purchase_request_pending", "已提采购申请", "orange"],
		["new_purchase_required", "需新增采购", "red"],
		["cannot_calculate", "无法判断", "gray"],
	];

	for (const [status, label, indicator] of cases) {
		assert.deepEqual(materialStatusMeta(status, helpers.translate), { label, indicator });
	}
});

test("escapes every server-provided label in rendered risk HTML", () => {
	const unsafe = '<img src=x onerror="alert(1)">';
	const unsafeMaterial = coverage({
		item_code: unsafe,
		item_name: unsafe,
		warehouse: unsafe,
	});
	const unsafeGroup = group(1, {
		item_code: unsafe,
		item_name: unsafe,
		warehouse: unsafe,
		bom_no: unsafe,
		materials: [{ ...unsafeMaterial, required_qty: 20, bom_qty_per_unit: 2 }],
	});
	const html = materialRiskHtml(
		buildMaterialRiskView({
			...fixtureWithSharedMaterial,
			material_groups: [unsafeGroup],
			material_coverage: [unsafeMaterial],
			shortages: [unsafeMaterial],
		}),
		helpers
	);

	assert.doesNotMatch(html, /<img/);
	assert.match(html, /&lt;img/);
});

test("explains shared inventory once at order level and expands only shortage products", () => {
	const enoughGroup = group(2, {
		materials: [
			{
				...coverage({ status: "ready_now", shortage_qty: 0, current_gap_qty: 0 }),
				required_qty: 10,
				bom_qty_per_unit: 2,
			},
		],
	});
	const html = materialRiskHtml(
		buildMaterialRiskView({
			...fixtureWithSharedMaterial,
			material_groups: [group(1), enoughGroup],
		}),
		helpers
	);

	assert.match(html, /本单汇总库存/);
	assert.match(html, /预计需新增采购 1 项/);
	assert.match(html, /<details[^>]*data-material-group="1"[^>]* open>/);
	assert.match(html, /<details[^>]*data-material-group="2"(?![^>]* open)>/);
	assert.match(html, /查看完整用料/);
});

test("renders explicit zero-production copy instead of an empty material table", () => {
	const view = buildMaterialRiskView({
		checked_at: "2026-08-02 11:00:00",
		production_required: 0,
		material_groups: [group(1, { production_required: 0, materials: [] })],
		material_coverage: [],
		shortages: [],
	});
	const html = materialRiskHtml(view, helpers);

	assert.match(html, /当前成品库存可覆盖，本单无需展开生产物料/);
	assert.doesNotMatch(html, /quick-material-table/);
});

test("preserves prior material data under checking and stale banners", () => {
	const view = buildMaterialRiskView(fixtureWithSharedMaterial);
	const checkingHtml = materialRiskHtml({ ...view, checking: true }, helpers);
	const staleHtml = materialRiskHtml({ ...view, stale: true }, helpers);

	assert.match(checkingHtml, /正在重新检查/);
	assert.match(checkingHtml, /RM-SHARED/);
	assert.match(staleHtml, /订单已修改，以下结果仅供参考，请重新检查/);
	assert.match(staleHtml, /RM-SHARED/);
});

test("keeps returned material detail visible beside escaped blockers", () => {
	const unsafeBlocker = '<img src=x onerror="alert(1)">';
	const html = materialRiskHtml(
		buildMaterialRiskView({
			...fixtureWithSharedMaterial,
			can_submit: false,
			blockers: [{ severity: "blocker", message: unsafeBlocker }],
		}),
		helpers
	);

	assert.match(html, /当前存在阻止下单的问题/);
	assert.match(html, /&lt;img/);
	assert.doesNotMatch(html, /<img/);
	assert.match(html, /RM-SHARED/);
});

test("confirmation shows at most five shortage rows and points to the lower detail", () => {
	const shortages = Array.from({ length: 7 }, (_, index) =>
		coverage({
			item_code: `RM-${index + 1}`,
			item_name: `Material ${index + 1}`,
			current_gap_qty: index + 2,
			open_material_request_qty: 1,
			open_purchase_order_qty: 2,
			shortage_qty: index + 1,
		})
	);
	const html = confirmationHtml(
		{
			customer: "Customer",
			delivery_date: "2026-08-08",
			grand_total: 100,
			currency: "CNY",
			available_to_reserve: 2,
			production_required: 10,
			shortage_item_count: 7,
			shortages,
			warnings: [],
		},
		helpers
	);

	assert.match(html, /RM-5/);
	assert.doesNotMatch(html, /RM-6/);
	assert.match(html, /另有 2 项，请查看页面下方明细/);
	assert.match(html, /当前生产缺口/);
	assert.match(html, /现有采购覆盖/);
	assert.match(html, /建议新增申请/);
	assert.match(html, /不会自动预留库存、创建生产任务或采购申请/);
});
