from __future__ import annotations

import hashlib
import json

import frappe
from frappe.desk.search import sanitize_searchfield
from frappe.utils import add_days, cint, escape_html, getdate, now_datetime, nowdate, parse_json

from erpnext import get_default_company
from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
	get_available_qty_to_reserve,
)

from process_simplification.api.setup import get_company_defaults, get_default_bom
from process_simplification.api.shortage import (
	MaterialCoverageBomExpansionError,
	calculate_material_coverage,
)
from process_simplification.api.utils import SimplifiedFlowError, normalize_qty, throw_chinese


REVIEW_TOKEN_TTL_SECONDS = 15 * 60
IDEMPOTENCY_RETENTION_DAYS = 30
QUICK_ORDER_SCHEMA_VERSION = 2
SUPPORTED_ORDER_FIELDS = {"customer", "delivery_date", "po_no", "remarks", "items"}
SUPPORTED_ITEM_FIELDS = {"item_code", "qty", "rate"}


def _as_dict(payload):
	if isinstance(payload, str):
		return frappe._dict(parse_json(payload))
	return frappe._dict(payload or {})


def _as_list(value):
	if isinstance(value, str):
		value = parse_json(value)
	return value or []


def _trim(value) -> str:
	return str(value or "").strip()


def _issue(code: str, severity: str, message: str, scope: str = "order", row: int | None = None):
	return {
		"code": code,
		"severity": severity,
		"message": message,
		"scope": scope,
		"row": row,
	}


def _canonical_hash(value) -> str:
	serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
	return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def quick_order_idempotency_name(user: str, idempotency_key: str) -> str:
	return hashlib.sha1(f"{user}:{idempotency_key}".encode("utf-8")).hexdigest()


def is_quick_order_v2_enabled() -> bool:
	return bool(cint(frappe.conf.get("process_simplification_quick_order_v2_enabled", 0)))


def _ensure_quick_order_v2_enabled():
	if not is_quick_order_v2_enabled():
		throw_chinese("新版快速开单当前未启用，请使用标准销售订单或联系管理员。")


def normalize_quick_order_payload(payload):
	data = _as_dict(payload)
	unsupported = sorted(key for key in data if key not in SUPPORTED_ORDER_FIELDS)
	if unsupported:
		throw_chinese("快速开单不支持字段 {0}，请使用标准销售订单。".format(", ".join(unsupported)))

	customer = _trim(data.get("customer"))
	if not customer:
		throw_chinese("客户不能为空。")

	delivery_date = _trim(data.get("delivery_date"))
	if not delivery_date:
		throw_chinese("交付日期不能为空。")
	try:
		parsed_delivery_date = getdate(delivery_date)
	except Exception:
		throw_chinese("交付日期格式不正确。")
	if parsed_delivery_date < getdate(nowdate()):
		throw_chinese("交付日期不能早于今天。")

	items = _as_list(data.get("items"))
	if not items:
		throw_chinese("请至少添加一个产品。")

	normalized_items = []
	for index, raw_row in enumerate(items, start=1):
		row = frappe._dict(raw_row or {})
		unsupported_row_fields = sorted(key for key in row if key not in SUPPORTED_ITEM_FIELDS)
		if unsupported_row_fields:
			throw_chinese(
				"第 {0} 行包含快速开单不支持的字段 {1}，请使用标准销售订单。".format(
					index, ", ".join(unsupported_row_fields)
				)
			)

		item_code = _trim(row.get("item_code"))
		qty = normalize_qty(row.get("qty"))
		rate = normalize_qty(row.get("rate"))
		if not item_code:
			throw_chinese("第 {0} 行产品不能为空。".format(index))
		if qty <= 0:
			throw_chinese("第 {0} 行数量必须大于 0。".format(index))
		if rate <= 0:
			throw_chinese("第 {0} 行成交单价必须大于 0。".format(index))
		normalized_items.append({"item_code": item_code, "qty": qty, "rate": rate})

	validate_no_duplicate_finished_goods(normalized_items)
	return frappe._dict(
		{
			"customer": customer,
			"delivery_date": str(parsed_delivery_date),
			"po_no": _trim(data.get("po_no")),
			"remarks": _trim(data.get("remarks")),
			"items": normalized_items,
		}
	)


def validate_no_duplicate_finished_goods(items):
	seen = set()
	duplicates = []
	for row in items:
		item_code = row.get("item_code")
		if item_code in seen and item_code not in duplicates:
			duplicates.append(item_code)
		seen.add(item_code)

	if duplicates:
		throw_chinese(
			"同一张快速开单中，同一个成品只能出现一次。请合并或改用标准销售订单：{0}".format(
				", ".join(duplicates)
			)
		)


def _selling_price_list() -> str | None:
	return frappe.db.get_single_value("Selling Settings", "selling_price_list")


def _item_default(item_code: str, company: str | None):
	if not company:
		return frappe._dict()
	return (
		frappe.db.get_value(
			"Item Default",
			{"parent": item_code, "company": company},
			["default_warehouse", "default_price_list"],
			as_dict=True,
		)
		or frappe._dict()
	)


def _item_price(item_code: str, price_list: str | None):
	filters = {"item_code": item_code, "selling": 1}
	if price_list:
		filters["price_list"] = price_list
	price = frappe.db.get_value(
		"Item Price", filters, ["price_list", "price_list_rate", "currency"], as_dict=True
	)
	if price or not price_list:
		return price
	return frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "selling": 1},
		["price_list", "price_list_rate", "currency"],
		as_dict=True,
	)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def search_quick_order_products(doctype, txt, searchfield, start, page_len, filters):
	_ensure_quick_order_v2_enabled()
	searchfield = searchfield or "name"
	sanitize_searchfield(searchfield)
	search_value = "%{0}%".format(txt or "")
	rows = frappe.db.sql(
		f"""
		select item.name, item.item_name
		from `tabItem` item
		where item.disabled = 0
			and item.is_sales_item = 1
			and item.has_variants = 0
			and item.has_serial_no = 0
			and item.has_batch_no = 0
			and not exists (
				select 1 from `tabProduct Bundle` bundle
				where bundle.new_item_code = item.name
					and bundle.docstatus = 1
					and bundle.is_active = 1
					and bundle.disabled = 0
			)
			and (
				item.`{searchfield}` like %(txt)s
				or item.item_name like %(txt)s
				or item.name like %(txt)s
			)
		order by case when item.name like %(prefix)s then 0 else 1 end, item.modified desc
		limit %(start)s, %(page_len)s
		""",
		{
			"txt": search_value,
			"prefix": "{0}%".format(txt or ""),
			"start": start,
			"page_len": page_len,
		},
	)
	return [row for row in rows if frappe.has_permission("Item", "read", doc=row[0])]


@frappe.whitelist()
def get_quick_order_context(company: str | None = None):
	_ensure_quick_order_v2_enabled()
	frappe.has_permission("Sales Order", "create", throw=True)
	defaults = get_company_defaults(company)
	company = defaults.company
	return {
		"enabled": True,
		"schema_version": QUICK_ORDER_SCHEMA_VERSION,
		"company": company,
		"currency": frappe.db.get_value("Company", company, "default_currency") if company else None,
		"fg_warehouse": defaults.fg_warehouse,
		"selling_price_list": _selling_price_list(),
		"default_delivery_date": add_days(nowdate(), 7),
	}


@frappe.whitelist()
def get_quick_order_item_defaults(item_code: str, company: str | None = None):
	_ensure_quick_order_v2_enabled()
	if not item_code:
		throw_chinese("产品不能为空。")
	frappe.has_permission("Item", "read", doc=item_code, throw=True)
	item = frappe.get_cached_value(
		"Item",
		item_code,
		[
			"item_code",
			"item_name",
			"stock_uom",
			"is_sales_item",
			"is_stock_item",
			"disabled",
			"has_variants",
			"has_serial_no",
			"has_batch_no",
		],
		as_dict=True,
	)
	if not item:
		throw_chinese("产品不存在：{0}".format(item_code))
	if item.disabled:
		throw_chinese("产品已停用：{0}".format(item_code))
	if not item.is_sales_item:
		throw_chinese("产品不是销售物料：{0}".format(item_code))
	if item.has_variants or item.has_serial_no or item.has_batch_no:
		throw_chinese("产品 {0} 需要标准销售订单处理规格、序列号或批次。".format(item_code))
	if frappe.db.exists(
		"Product Bundle",
		{"new_item_code": item_code, "docstatus": 1, "is_active": 1, "disabled": 0},
	):
		throw_chinese("产品 {0} 是产品组合，请使用标准销售订单。".format(item_code))

	company = company or get_default_company()
	defaults = get_company_defaults(company)
	item_default = _item_default(item_code, defaults.company)
	warehouse = item_default.default_warehouse or defaults.fg_warehouse
	price_list = item_default.default_price_list or _selling_price_list()
	price = _item_price(item_code, price_list) or frappe._dict()
	available_to_reserve = (
		get_available_qty_to_reserve(item_code, warehouse) if item.is_stock_item and warehouse else 0
	)
	bom_no = get_default_bom(item_code)
	return {
		"item_code": item.item_code,
		"item_name": item.item_name,
		"stock_uom": item.stock_uom,
		"warehouse": warehouse,
		"rate": normalize_qty(price.get("price_list_rate")),
		"currency": price.get("currency")
		or (frappe.db.get_value("Company", defaults.company, "default_currency") if defaults.company else None),
		"price_list": price.get("price_list") or price_list,
		"available_to_reserve": available_to_reserve,
		"available_qty": available_to_reserve,
		"bom_no": bom_no,
		"has_bom": bool(bom_no),
		"has_variants": item.has_variants,
	}


@frappe.whitelist()
def preview_quick_order_items(items, company: str | None = None):
	_ensure_quick_order_v2_enabled()
	rows = []
	for index, raw_row in enumerate(_as_list(items), start=1):
		row = frappe._dict(raw_row or {})
		item_code = _trim(row.get("item_code"))
		qty = normalize_qty(row.get("qty"))
		if not item_code or qty <= 0:
			continue
		try:
			detail = frappe._dict(get_quick_order_item_defaults(item_code, company))
		except (frappe.PermissionError, frappe.DoesNotExistError, SimplifiedFlowError):
			issues = [
				_issue(
					"ITEM_UNAVAILABLE",
					"blocker",
					"产品不存在、已停用、无权访问或不适用于快速开单。",
					"line",
					index,
				)
			]
			rows.append(
				{
					"item_code": item_code,
					"row": index,
					"qty": qty,
					"available_to_reserve": 0,
					"production_required": qty,
					"issues": issues,
					"blocked": True,
				}
			)
			continue
		coverage = min(max(normalize_qty(detail.get("available_to_reserve")), 0), qty)
		production_required = max(qty - coverage, 0)
		issues = []
		if not detail.get("warehouse"):
			issues.append(
				_issue("FG_WAREHOUSE_MISSING", "blocker", "无法确定成品仓库，请先完善公司或物料默认值。", "line", index)
			)
		if production_required > 0:
			issues.append(
				_issue(
					"FINISHED_GOODS_SHORTAGE",
					"warning",
					"成品库存不足，需要生产 {0}。".format(production_required),
					"line",
					index,
				)
			)
			if not detail.get("bom_no"):
				issues.insert(
					0,
					_issue(
						"PRODUCTION_BOM_MISSING",
						"blocker",
						"需要生产，但没有已提交、启用的默认 BOM。",
						"line",
						index,
					),
				)
		rows.append(
			{
				**detail,
				"row": index,
				"qty": qty,
				"available_to_reserve": coverage,
				"production_required": production_required,
				"issues": issues,
				"blocked": any(issue["severity"] == "blocker" for issue in issues),
			}
		)
	return {
		"schema_version": QUICK_ORDER_SCHEMA_VERSION,
		"rows": rows,
		"available_to_reserve": sum(row["available_to_reserve"] for row in rows),
		"production_required": sum(row["production_required"] for row in rows),
	}


def _validate_customer(customer: str):
	if not frappe.db.exists("Customer", customer):
		return [_issue("CUSTOMER_INVALID", "blocker", "客户不存在或无权访问。")]
	if not frappe.has_permission("Customer", "read", doc=customer):
		return [_issue("CUSTOMER_INVALID", "blocker", "客户不存在或无权访问。")]
	if frappe.db.get_value("Customer", customer, "disabled"):
		return [_issue("CUSTOMER_DISABLED", "blocker", "客户已停用。")]
	return []


def _customer_po_issue(customer: str, po_no: str):
	if not customer or not po_no:
		return None
	existing_order = frappe.db.get_value(
		"Sales Order",
		{
			"po_no": po_no,
			"customer": customer,
			"docstatus": ("<", 2),
		},
		"name",
	)
	if not existing_order:
		return None
	if cint(frappe.get_single_value("Selling Settings", "allow_against_multiple_purchase_orders")):
		return _issue(
			"DUPLICATE_CUSTOMER_PO_ALLOWED",
			"warning",
			"客户订单号已用于销售订单 {0}；当前设置允许重复，请确认。".format(existing_order),
		)
	return _issue(
		"DUPLICATE_CUSTOMER_PO",
		"blocker",
		"客户订单号已用于销售订单 {0}，不能重复下单。".format(existing_order),
	)


def _build_sales_order(data, preview_rows, company: str):
	rows_by_item = {row["item_code"]: row for row in preview_rows}
	so = frappe.new_doc("Sales Order")
	so.customer = data.customer
	so.company = company
	so.order_type = "Sales"
	so.transaction_date = nowdate()
	so.delivery_date = data.delivery_date
	so.po_no = data.po_no or None
	so.terms = escape_html(data.remarks) if data.remarks else None
	so.group_same_items = 0
	for row in data.get("items"):
		preview = rows_by_item[row["item_code"]]
		so.append(
			"items",
			{
				"item_code": row["item_code"],
				"qty": row["qty"],
				"rate": row["rate"],
				"delivery_date": data.delivery_date,
				"warehouse": preview.get("warehouse"),
				"bom_no": preview.get("bom_no") if preview.get("production_required") > 0 else None,
			},
		)
	return so


def _standard_validate_sales_order(so):
	so.set_missing_values(for_validate=True)
	so.run_method("before_validate")
	so.run_method("validate")


def _validate_commercial_rules(so):
	try:
		_standard_validate_sales_order(so)
	except Exception as exc:
		return [
			_issue(
				"ERP_NEXT_VALIDATION",
				"blocker",
				"ERPNext 校验未通过：{0}".format(escape_html(str(exc))),
			)
		]

	try:
		so.check_credit_limit()
	except Exception as exc:
		return [
			_issue(
				"CREDIT_LIMIT",
				"blocker",
				"客户信用额度校验未通过：{0}".format(escape_html(str(exc))),
			)
		]
	return []


def _quick_order_intent_digest(data) -> str:
	return _canonical_hash(
		{
			"customer": data.customer,
			"delivery_date": data.delivery_date,
			"po_no": data.po_no,
			"remarks": data.remarks,
			"items": data.get("items"),
		}
	)


def quick_order_review_fingerprint(result) -> str:
	stable = {
		"intent_digest": result.get("intent_digest"),
		"grand_total": normalize_qty(result.get("grand_total")),
		"available_to_reserve": normalize_qty(result.get("available_to_reserve")),
		"production_required": normalize_qty(result.get("production_required")),
		"shortage_item_count": int(result.get("shortage_item_count") or 0),
		"blockers": [
			(issue.get("code"), issue.get("scope"), issue.get("row")) for issue in result.get("blockers") or []
		],
		"warnings": [
			(issue.get("code"), issue.get("scope"), issue.get("row")) for issue in result.get("warnings") or []
		],
		"rows": [
			{
				"item_code": row.get("item_code"),
				"available_to_reserve": normalize_qty(row.get("available_to_reserve")),
				"production_required": normalize_qty(row.get("production_required")),
				"bom_no": row.get("bom_no"),
			}
			for row in result.get("rows") or []
		],
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
	}
	return _canonical_hash(stable)


def _evaluate_quick_order(payload):
	data = normalize_quick_order_payload(payload)
	frappe.has_permission("Sales Order", "create", throw=True)
	defaults = get_company_defaults()
	company = defaults.company or get_default_company()
	blockers = []
	warnings = []
	if not company:
		blockers.append(_issue("COMPANY_MISSING", "blocker", "默认公司缺失，请先设置公司。"))
	blockers.extend(_validate_customer(data.customer))
	po_issue = _customer_po_issue(data.customer, data.po_no)
	if po_issue:
		(blockers if po_issue["severity"] == "blocker" else warnings).append(po_issue)

	preview = preview_quick_order_items(data.get("items"), company)
	for row in preview["rows"]:
		for issue in row["issues"]:
			(blockers if issue["severity"] == "blocker" else warnings).append(issue)

	demands = [
		{
			"bom_no": row.get("bom_no"),
			"qty": row.get("production_required"),
			"source": {
				"row": row.get("row"),
				"sales_order": "快速开单预检",
				"sales_order_item": "第 {0} 行".format(row.get("row")),
				"finished_item": row.get("item_code"),
				"production_qty": row.get("production_required"),
				"bom_no": row.get("bom_no"),
			},
		}
		for row in preview["rows"]
		if row.get("production_required") > 0 and row.get("bom_no")
	]
	coverage = frappe._dict({"materials": [], "shortages": []})
	if company and demands:
		try:
			coverage = calculate_material_coverage(
				demands,
				company,
				need_by_date=data.delivery_date,
				defaults=defaults,
			)
		except MaterialCoverageBomExpansionError:
			for demand in demands:
				blockers.append(
					_issue(
						"BOM_EXPLOSION_FAILED",
						"blocker",
						"BOM 展开失败，无法评估原料风险，请检查 BOM 后重试。",
						"line",
						demand["source"]["row"],
					)
				)
	material_coverage = coverage.get("materials") or []
	shortages = coverage.get("shortages") or []
	material_groups = []
	material_groups_by_row = {}
	for preview_row in preview["rows"]:
		group = {
			"row": preview_row.get("row"),
			"item_code": preview_row.get("item_code"),
			"item_name": preview_row.get("item_name"),
			"qty": normalize_qty(preview_row.get("qty")),
			"warehouse": preview_row.get("warehouse"),
			"available_to_reserve": normalize_qty(preview_row.get("available_to_reserve")),
			"production_required": normalize_qty(preview_row.get("production_required")),
			"bom_no": preview_row.get("bom_no"),
			"materials": [],
		}
		material_groups.append(group)
		material_groups_by_row[group["row"]] = group

	for material in material_coverage:
		for source in material.get("sources") or []:
			group = material_groups_by_row.get(source.get("row"))
			if not group:
				continue
			contribution = dict(material)
			contribution["required_qty"] = normalize_qty(source.get("required_qty"))
			contribution["bom_qty_per_unit"] = normalize_qty(source.get("bom_qty_per_unit"))
			contribution["sources"] = [source]
			group["materials"].append(contribution)

		if material.get("status") == "cannot_calculate":
			for source in material.get("sources") or []:
				if source.get("row") is not None:
					blockers.append(
						_issue(
							"RAW_MATERIAL_WAREHOUSE_MISSING",
							"blocker",
							"无法确定原料仓库，请先完善 BOM、公司或物料默认值。",
							"line",
							source.get("row"),
						)
					)
	if shortages:
		warnings.append(
			_issue(
				"RAW_MATERIAL_SHORTAGE",
				"warning",
				"原料存在 {0} 项采购缺口，不阻止下单。".format(len(shortages)),
			)
		)

	so = None
	if company and not blockers:
		so = _build_sales_order(data, preview["rows"], company)
		blockers.extend(_validate_commercial_rules(so))

	grand_total = normalize_qty(so.grand_total) if so else sum(
		row["qty"] * row["rate"] for row in data.get("items")
	)
	result = {
		"schema_version": QUICK_ORDER_SCHEMA_VERSION,
		"status": "blocked" if blockers else "ready",
		"can_submit": not blockers,
		"company": company,
		"customer": data.customer,
		"delivery_date": data.delivery_date,
		"po_no": data.po_no,
		"remarks": data.remarks,
		"currency": so.currency if so else None,
		"grand_total": grand_total,
		"available_to_reserve": preview["available_to_reserve"],
		"production_required": preview["production_required"],
		"shortage_item_count": len(shortages),
		"shortages": shortages,
		"material_coverage": material_coverage,
		"material_groups": material_groups,
		"rows": preview["rows"],
		"blockers": blockers,
		"warnings": warnings,
		"checked_at": str(now_datetime()),
		"intent_digest": _quick_order_intent_digest(data),
	}
	result["review_fingerprint"] = quick_order_review_fingerprint(result)
	result["_normalized_payload"] = data
	result["_sales_order"] = so
	return result


def _public_result(result):
	return {key: value for key, value in result.items() if not key.startswith("_")}


def _issue_review_token(result):
	token = frappe.generate_hash(length=40)
	frappe.cache.set_value(
		"process_simplification:quick_order_review:{0}".format(token),
		{
			"user": frappe.session.user,
			"intent_digest": result["intent_digest"],
			"review_fingerprint": result["review_fingerprint"],
		},
		expires_in_sec=REVIEW_TOKEN_TTL_SECONDS,
		shared=True,
	)
	return token


def _get_review_token(token: str):
	if not token:
		throw_chinese("确认信息已失效，请重新检查订单。")
	stored = frappe.cache.get_value(
		"process_simplification:quick_order_review:{0}".format(token), shared=True
	)
	if not stored or stored.get("user") != frappe.session.user:
		throw_chinese("确认信息已过期或不属于当前用户，请重新检查订单。")
	return frappe._dict(stored)


@frappe.whitelist()
def preflight_quick_sales_order(payload=None, **kwargs):
	_ensure_quick_order_v2_enabled()
	result = _evaluate_quick_order(payload or kwargs)
	public = _public_result(result)
	if result["can_submit"]:
		public["review_token"] = _issue_review_token(result)
	return public


@frappe.whitelist()
def check_quick_order_shortage(payload=None, **kwargs):
	return preflight_quick_sales_order(payload, **kwargs)


def _existing_idempotency_result(record, intent_digest: str):
	if record.intent_digest != intent_digest:
		throw_chinese("该提交标识已用于另一张订单，请刷新页面后重试。")
	if record.status == "Completed" and record.sales_order:
		if frappe.db.exists("Sales Order", record.sales_order):
			return {
				"schema_version": QUICK_ORDER_SCHEMA_VERSION,
				"status": "submitted",
				"sales_order": record.sales_order,
				"docstatus": 1,
				"route": ["order-workbench", record.sales_order],
				"idempotent_replay": True,
			}
		throw_chinese("上次提交结果异常，请联系管理员检查订单记录。")
	throw_chinese("相同订单正在提交，请稍后重试。")


def _create_idempotency_record(name: str, key: str, intent_digest: str):
	record = frappe.get_doc(
		{
			"doctype": "Quick Order Idempotency",
			"name": name,
			"idempotency_key": key,
			"requesting_user": frappe.session.user,
			"intent_digest": intent_digest,
			"status": "In Progress",
		}
	)
	record.flags.ignore_permissions = True
	record.insert()
	return record


@frappe.whitelist()
def submit_quick_sales_order(payload=None, review_token: str | None = None, idempotency_key: str | None = None, **kwargs):
	_ensure_quick_order_v2_enabled()
	frappe.has_permission("Sales Order", "create", throw=True)
	data = normalize_quick_order_payload(payload or kwargs)
	intent_digest = _quick_order_intent_digest(data)
	stored_review = _get_review_token(review_token or "")
	if stored_review.intent_digest != intent_digest:
		throw_chinese("订单内容已修改，请重新检查后再确认。")

	idempotency_key = _trim(idempotency_key)
	if not idempotency_key or len(idempotency_key) > 140:
		throw_chinese("提交标识缺失或格式不正确，请刷新页面后重试。")
	record_name = quick_order_idempotency_name(frappe.session.user, idempotency_key)
	lock_name = "process_simplification:quick_order_submit:{0}".format(record_name)

	with frappe.cache.lock(lock_name, timeout=30, blocking_timeout=5):
		if frappe.db.exists("Quick Order Idempotency", record_name):
			record = frappe.get_doc("Quick Order Idempotency", record_name)
			return _existing_idempotency_result(record, intent_digest)

		current = _evaluate_quick_order(data)
		if not current["can_submit"]:
			return _public_result(current)
		if current["review_fingerprint"] != stored_review.review_fingerprint:
			public = _public_result(current)
			public["status"] = "reconfirmation_required"
			public["review_token"] = _issue_review_token(current)
			return public

		frappe.db.savepoint("quick_order_submit")
		record = _create_idempotency_record(record_name, idempotency_key, intent_digest)
		so = current["_sales_order"]
		try:
			so.insert()
			so.submit()
			record.status = "Completed"
			record.sales_order = so.name
			record.completed_at = now_datetime()
			record.save(ignore_permissions=True)
			# Keep the lock until both the order and durable key are visible to a retry.
			# Frappe's request-level auto-commit runs only after this method returns.
			frappe.db.commit()
		except Exception as exc:
			frappe.db.rollback(save_point="quick_order_submit")
			throw_chinese("创建销售订单失败：{0}".format(escape_html(str(exc))))

	return {
		"schema_version": QUICK_ORDER_SCHEMA_VERSION,
		"status": "submitted",
		"sales_order": so.name,
		"docstatus": so.docstatus,
		"route": ["order-workbench", so.name],
	}


@frappe.whitelist()
def create_quick_sales_order(payload=None, **kwargs):
	throw_chinese("快速开单已升级，请刷新页面并完成库存预检后再提交。")


@frappe.whitelist()
def cleanup_quick_order_idempotency():
	frappe.only_for("System Manager")
	return cleanup_expired_quick_order_idempotency()


def cleanup_expired_quick_order_idempotency():
	cutoff = add_days(nowdate(), -IDEMPOTENCY_RETENTION_DAYS)
	return frappe.db.delete("Quick Order Idempotency", {"status": "Completed", "completed_at": ("<", cutoff)})
