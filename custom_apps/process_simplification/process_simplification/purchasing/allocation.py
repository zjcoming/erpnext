"""Allocate one submitted request across supplier POs without changing ERPNext ledgers."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict

import frappe
from frappe.utils import flt, getdate, nowdate

from erpnext.stock.doctype.material_request.material_request import get_default_supplier_for_item
from process_simplification.api.utils import get_quantity_precision, normalize_purchase_qty
from process_simplification.notifications import _user_matches_company
from process_simplification.purchasing.receipts import lock_material_requests
from process_simplification.purchasing.query import document_page, item_search_codes
from process_simplification.request_transaction import retry_request_transaction

BATCH = "Purchase Allocation Batch"
HEADER_FIELDS = ("supplier", "currency", "taxes_and_charges", "tc_name", "payment_terms_template")


def _request(name, create=False):
	doc = frappe.get_doc("Material Request", name)
	doc.check_permission("read")
	if not _user_matches_company(frappe.session.user, doc.company):
		frappe.throw("无权操作其他公司的采购申请。", frappe.PermissionError)
	if (
		doc.docstatus != 1
		or doc.material_request_type != "Purchase"
		or doc.status in {"Stopped", "Cancelled"}
	):
		frappe.throw("请选择已提交且未停止的采购申请。")
	if create:
		frappe.has_permission("Purchase Order", "create", throw=True)
	return doc


def coverage(request, exclude_po=None):
	"""Draft commitments + submitted outstanding + net accepted, without double counting."""
	orders = frappe.db.sql(
		"""select p.name, p.supplier, p.docstatus, p.status, i.name as row_name,
		i.material_request_item, i.stock_qty
		from `tabPurchase Order Item` i join `tabPurchase Order` p on p.name=i.parent
		where i.material_request=%s and p.docstatus<2 order by p.name, i.idx for update""",
		request,
		as_dict=True,
	)
	receipts = frappe.db.sql(
		"""select i.material_request_item, i.purchase_order_item, i.stock_qty
		from `tabPurchase Receipt Item` i join `tabPurchase Receipt` p on p.name=i.parent
		where i.material_request=%s and p.docstatus=1 for update""",
		request,
		as_dict=True,
	)
	result = defaultdict(
		lambda: frappe._dict(draft_qty=0.0, ordered_pending_qty=0.0, received_qty=0.0, occupied_qty=0.0)
	)
	po_received = defaultdict(float)
	for row in receipts:
		result[row.material_request_item].received_qty += flt(row.stock_qty)
		po_received[row.purchase_order_item] += flt(row.stock_qty)
	for row in orders:
		if row.name == exclude_po or row.status in {"Closed", "Cancelled"}:
			continue
		key = "draft_qty" if row.docstatus == 0 else "ordered_pending_qty"
		result[row.material_request_item][key] += max(flt(row.stock_qty) - po_received[row.row_name], 0)
	for values in result.values():
		for key in ("received_qty", "draft_qty", "ordered_pending_qty"):
			values[key] = normalize_purchase_qty(values[key])
		values.occupied_qty = normalize_purchase_qty(
			max(values.received_qty, 0) + values.draft_qty + values.ordered_pending_qty
		)
	return result, orders, po_received


def _managed_requests(doc):
	names = set(row.material_request for row in doc.items if row.material_request)
	return {
		name
		for name in names
		if frappe.db.sql(
			"select name from `tabPurchase Allocation Batch` where material_request=%s limit 1 for update",
			name,
		)
	}


def lock_order_sources(doc, method=None):
	# Native PO creation may race the first allocation batch. Lock before checking adoption.
	lock_material_requests(row.material_request for row in doc.items)


def validate_order_allocation(doc, method=None):
	"""Also guard native draft edits, submits and reopening of requests using allocations."""
	if doc.flags.in_delete or doc.docstatus == 2 or doc.status in {"Closed", "Cancelled"}:
		return
	for name in sorted(_managed_requests(doc)):
		lock_material_requests([name])
		used, _, received = coverage(name, exclude_po=doc.name)
		candidate = defaultdict(float)
		for row in doc.items:
			if row.material_request == name:
				candidate[row.material_request_item] += max(
					flt(row.qty) * flt(row.conversion_factor) - received[row.name], 0
				)
		for row_name, qty in candidate.items():
			demand = frappe.db.get_value(
				"Material Request Item",
				{"name": row_name, "parent": name},
				["stock_qty", "item_code"],
				as_dict=True,
			)
			if not demand or flt(used[row_name].occupied_qty + qty - flt(demand.stock_qty), 6) > 0:
				frappe.throw(
					f"采购申请 {name} 的明细已被其他草稿或正式采购占用，请刷新供应商分配页面后调整数量。"
				)


def _positive(value, label, allow_zero=False):
	try:
		number = float(value)
	except (ValueError, TypeError):
		frappe.throw(f"{label}必须是有效数字。")
	if not math.isfinite(number) or number < 0 or (not allow_zero and number == 0):
		frappe.throw(f"{label}必须{'大于或等于' if allow_zero else '大于'} 0。")
	if abs(number - round(number, 6)) > 1e-9:
		frappe.throw(f"{label}最多支持 6 位小数。")
	return number


def _uom_qty(item, uom, qty):
	conversion = (
		1.0
		if uom == item.stock_uom
		else frappe.db.get_value(
			"UOM Conversion Detail", {"parent": item.item_code, "uom": uom}, "conversion_factor"
		)
	)
	if not conversion or flt(conversion) <= 0:
		frappe.throw(f"物料 {item.item_code} 没有单位 {uom} 的有效换算，请先维护物料。")
	stock_qty = qty * flt(conversion)
	for value, unit in ((qty, uom), (stock_qty, item.stock_uom)):
		if frappe.db.get_value("UOM", unit, "must_be_whole_number") and abs(value - round(value)) > 1e-8:
			frappe.throw(f"单位 {unit} 必须使用整数数量。")
	precision = get_quantity_precision("Purchase Order Item", "qty")
	if abs(qty - flt(qty, precision)) > 1e-9:
		frappe.throw(f"采购数量最多支持 {precision} 位小数，请调整数量或采购单位。")
	stock_qty = normalize_purchase_qty(stock_qty)
	if flt(qty, precision) <= 0 or stock_qty <= 0:
		frappe.throw("采购数量按单据精度处理后必须大于 0，请调整数量或采购单位。")
	return flt(conversion), stock_qty


def _normalize(doc, data):
	data = frappe.parse_json(data) if isinstance(data, str) else data
	if not isinstance(data, dict) or not data.get("rows") or not isinstance(data.get("quantities"), dict):
		frappe.throw("请填写本次采购数量和供应商分配。")
	if data.get("company") and data["company"] != doc.company:
		frappe.throw("一次分配只能属于同一公司。")
	items = {row.name: row for row in doc.items}
	quantities = {name: _positive(qty, "本次采购数量") for name, qty in data["quantities"].items()}
	if not quantities.keys() <= items.keys():
		frappe.throw("本次采购包含不属于该申请的明细。")
	rows, totals = [], defaultdict(float)
	for raw in data["rows"]:
		row = frappe._dict(raw)
		item = items.get(row.material_request_item)
		if not item or item.name not in quantities:
			frappe.throw("分配明细必须属于本次选择的采购申请行。")
		if row.company and row.company != doc.company:
			frappe.throw("一次分配只能属于同一公司。")
		supplier = frappe.get_doc("Supplier", row.supplier)
		supplier.check_permission("read")
		if supplier.disabled:
			frappe.throw("不能向已停用供应商采购。")
		qty = _positive(row.qty, "供应商采购数量")
		uom = row.uom or item.uom
		conversion, stock_qty = _uom_qty(item, uom, qty)
		currency = (
			row.currency
			or supplier.default_currency
			or frappe.db.get_value("Company", doc.company, "default_currency")
		)
		if supplier.default_currency and currency != supplier.default_currency:
			frappe.throw(f"供应商 {supplier.name} 的单据币种必须为 {supplier.default_currency}。")
		if not frappe.db.exists("Currency", currency):
			frappe.throw("请选择有效币种。")
		warehouse = row.warehouse or item.warehouse
		if warehouse != item.warehouse:
			frappe.throw("收货仓库必须沿用申请明细；不同仓库请使用各自的申请行。")
		if not warehouse or frappe.db.get_value("Warehouse", warehouse, "company") != doc.company:
			frappe.throw("请选择本公司有效的目标仓库。")
		date = getdate(row.schedule_date or item.schedule_date)
		if date < getdate(nowdate()):
			frappe.throw("采购需要日期不能早于今天。")
		clean = {
			"material_request_item": item.name,
			"supplier": supplier.name,
			"qty": flt(qty, get_quantity_precision("Purchase Order Item", "qty")),
			"uom": uom,
			"conversion_factor": conversion,
			"stock_qty": stock_qty,
			"rate": _positive(row.rate, "采购单价", allow_zero=True),
			"currency": currency,
			"schedule_date": str(date),
			"warehouse": warehouse,
			"taxes_and_charges": row.taxes_and_charges or "",
			"tc_name": row.tc_name or "",
			"payment_terms_template": row.payment_terms_template or supplier.payment_terms or "",
		}
		for field, doctype in (
			("taxes_and_charges", "Purchase Taxes and Charges Template"),
			("tc_name", "Terms and Conditions"),
			("payment_terms_template", "Payment Terms Template"),
		):
			if clean[field]:
				template = frappe.get_doc(doctype, clean[field])
				template.check_permission("read")
				if template.get("company") and template.company != doc.company:
					frappe.throw("税费或条款模板不属于当前公司。")
		rows.append(clean)
		totals[item.name] += stock_qty
	for name, qty in quantities.items():
		_uom_qty(items[name], items[name].stock_uom, qty)
		if abs(flt(totals[name] - qty, 6)) > 0:
			frappe.throw(
				f"物料 {items[name].item_code} 的供应商合计必须等于本次采购数量 {qty:g} {items[name].stock_uom}。"
			)
	rows.sort(key=lambda row: json.dumps(row, sort_keys=True))
	return {"company": doc.company, "quantities": quantities, "rows": rows}


def _check_available(doc, data):
	used, _, _ = coverage(doc.name)
	for row in doc.items:
		if (
			row.name in data["quantities"]
			and flt(data["quantities"][row.name] + used[row.name].occupied_qty - flt(row.stock_qty), 6) > 0
		):
			frappe.throw(f"物料 {row.item_code} 的可分配数量已变化，请刷新后重新分配。")


def _groups(data):
	groups = defaultdict(list)
	for row in data["rows"]:
		groups[tuple(row[field] for field in HEADER_FIELDS)].append(row)
	return [
		{
			**dict(zip(HEADER_FIELDS, key, strict=True)),
			"rows": rows,
			"reason": "按供应商、币种、税费、条款及付款条件分单",
		}
		for key, rows in sorted(groups.items())
	]


@frappe.whitelist()
def get_allocation_context(material_request):
	doc = _request(material_request)
	lock_material_requests([doc.name])
	used, orders, _ = coverage(doc.name)
	items = []
	for row in doc.items:
		item = {
			key: row.get(key)
			for key in (
				"name",
				"item_code",
				"item_name",
				"qty",
				"uom",
				"stock_qty",
				"stock_uom",
				"conversion_factor",
				"warehouse",
				"sales_order",
				"schedule_date",
			)
		}
		item.update(used[row.name])
		item["available_qty"] = max(normalize_purchase_qty(row.stock_qty - used[row.name].occupied_qty), 0)
		item["default_supplier"] = get_default_supplier_for_item(row.item_code, doc.company)
		item["uoms"] = frappe.get_all(
			"UOM Conversion Detail", filters={"parent": row.item_code}, fields=["uom", "conversion_factor"]
		)
		items.append(item)
	unique_orders = {
		row.name: {key: row[key] for key in ("name", "supplier", "docstatus", "status")} for row in orders
	}
	readable_orders = []
	if frappe.has_permission("Purchase Order", "read"):
		for name in unique_orders:
			po = frappe.get_doc("Purchase Order", name)
			if frappe.has_permission("Purchase Order", "read", doc=po) and _user_matches_company(frappe.session.user, po.company):
				readable_orders.append(_order_followup(po, material_request=doc.name, pending_only=False))
	can_create = frappe.has_permission("Purchase Order", "create")
	references = {}
	if can_create:
		for doctype in (
			"Supplier",
			"Currency",
			"Terms and Conditions",
			"Purchase Taxes and Charges Template",
			"Payment Terms Template",
		):
			if frappe.has_permission(doctype, "read"):
				filters = {"disabled": 0} if doctype == "Supplier" else {}
				if doctype == "Purchase Taxes and Charges Template":
					filters["company"] = doc.company
				references[doctype] = frappe.get_list(
					doctype, filters=filters, pluck="name", limit=500, order_by="name asc"
				)
	return {
		"material_request": doc.name,
		"company": doc.company,
		"items": items,
		"orders": readable_orders,
		"currency": frappe.db.get_value("Company", doc.company, "default_currency"),
		"reference_options": references,
		"can_create": can_create,
		"can_submit": frappe.has_permission("Purchase Order", "submit"),
	}


@frappe.whitelist()
def get_request_page(start=0, page_length=20, search="", view="pending"):
	if view not in {"pending", "history", "all"}:
		frappe.throw("无效的采购申请范围。")
	search = str(search or "").strip().lower()
	item_codes = item_search_codes(search)
	finished = ["Received", "Transferred", "Issued"]
	filters = {"docstatus": 1, "material_request_type": "Purchase"}
	filters["status"] = ["in", finished] if view == "history" else ["not in", ["Stopped", "Cancelled"] + (finished if view == "pending" else [])]
	labels = {"Pending": "待下单", "Partially Ordered": "部分下单", "Ordered": "已下单", "Partially Received": "部分到货", "Received": "已到货"}
	def project(doc):
		if not any(flt(item.stock_qty) > 0 for item in doc.items):
			return None
		words = " ".join(str(doc.get(key) or "") for key in ("name", "company", "status")) + " " + labels.get(doc.status, "")
		if search and search not in words.lower() and not any(item.item_code in item_codes for item in doc.items):
			return None
		return {key: doc.get(key) for key in ("name", "company", "status", "transaction_date")}
	return document_page("Material Request", filters, project, start=start, page_length=page_length)


@frappe.whitelist()
def list_requests():
	"""Preserve the original list contract for existing clients without truncation."""
	rows, start = [], 0
	while start is not None:
		page = get_request_page(start=start, page_length=50, view="all")
		rows.extend(frappe._dict(row) for row in page["rows"])
		start = page["next_start"]
	return rows


def _order_followup(doc, *, material_request=None, pending_only=True, search="", item_codes=None, overdue_only=False):
	returned = {
		row.purchase_order_item: flt(row.returned_stock_qty)
		for row in frappe.db.sql(
			"""select i.purchase_order_item, -sum(i.stock_qty) as returned_stock_qty
			from `tabPurchase Receipt Item` i join `tabPurchase Receipt` p on p.name=i.parent
			where i.purchase_order=%s and p.docstatus=1 and p.is_return=1
			group by i.purchase_order_item""", doc.name, as_dict=True,
		)
	}
	items = []
	header_matches = not search or search in f"{doc.name} {doc.supplier} {doc.get('supplier_name') or ''}".lower()
	for item in doc.items:
		if material_request and item.material_request != material_request:
			continue
		pending_qty = max(normalize_purchase_qty(flt(item.qty) - flt(item.received_qty)), 0)
		if pending_only and pending_qty <= 0:
			continue
		active = doc.docstatus == 1 and doc.status not in {"Closed", "On Hold", "Completed", "Cancelled"}
		days = max((getdate(nowdate()) - getdate(item.schedule_date)).days, 0) if active and item.schedule_date and pending_qty > 0 else 0
		if overdue_only and not days:
			continue
		if not header_matches and item.item_code not in (item_codes or set()) and search not in str(item.material_request or "").lower():
			continue
		items.append({
			"item_code": item.item_code, "item_name": item.item_name,
			"qty": flt(item.qty), "received_qty": flt(item.received_qty), "pending_qty": pending_qty,
			"returned_qty": normalize_purchase_qty(returned.get(item.name, 0) / (flt(item.conversion_factor) or 1)),
			"uom": item.uom, "schedule_date": item.schedule_date, "overdue_days": days,
			"material_request": item.material_request,
		})
	from process_simplification.api.utils import apply_current_item_names
	apply_current_item_names(items)
	return {
		**{key: doc.get(key) for key in ("name", "supplier", "company", "status", "docstatus", "per_received")},
		"items": items, "can_submit": bool(doc.docstatus == 0 and frappe.has_permission("Purchase Order", "submit", doc=doc)),
		"can_receive": bool(doc.docstatus == 1 and doc.status not in {"Closed", "On Hold", "Completed", "Cancelled"} and any(item["pending_qty"] > 0 for item in items) and frappe.has_permission("Purchase Receipt", "create")),
	}


@frappe.whitelist()
def get_supplier_followup(start=0, page_length=20, search="", overdue_only=0):
	search = str(search or "").strip().lower()
	item_codes = item_search_codes(search)
	def project(doc):
		row = _order_followup(doc, search=search, item_codes=item_codes, overdue_only=frappe.utils.cint(overdue_only))
		return row if row["items"] else None
	return document_page(
		"Purchase Order", {"docstatus": 1, "per_received": ["<", 100], "status": ["not in", ["Closed", "On Hold", "Completed", "Cancelled"]]},
		project, start=start, page_length=page_length, order_by="schedule_date asc, creation asc, name asc",
	)


@frappe.whitelist(methods=["POST"])
def preview_allocation(material_request, data):
	doc = _request(material_request, create=True)
	lock_material_requests([doc.name])
	doc.reload()
	normalized = _normalize(doc, data)
	_check_available(doc, normalized)
	return {"groups": _groups(normalized), "quantities": normalized["quantities"]}


@frappe.whitelist(methods=["POST"])
@retry_request_transaction
def create_purchase_orders(material_request, request_key, data):
	doc = _request(material_request, create=True)
	if not request_key or len(str(request_key)) > 120:
		frappe.throw("缺少有效的本次生成标识，请刷新页面重试。")
	lock_material_requests([doc.name])
	doc.reload()
	normalized = _normalize(doc, data)
	digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
	name = (
		"PAL-" + hashlib.sha256(f"{doc.name}:{frappe.session.user}:{request_key}".encode()).hexdigest()[:32]
	)
	if frappe.db.exists(BATCH, name):
		batch = frappe.get_doc(BATCH, name)
		if batch.intent_digest != digest:
			frappe.throw("本次生成标识已用于其他分配内容，请刷新页面后重新操作。")
		return json.loads(batch.result)
	_check_available(doc, normalized)
	frappe.db.savepoint("purchase_allocation")
	try:
		batch = frappe.get_doc(
			{
				"doctype": BATCH,
				"name": name,
				"material_request": doc.name,
				"company": doc.company,
				"requesting_user": frappe.session.user,
				"request_key": request_key,
				"intent_digest": digest,
			}
		)
		batch.insert(ignore_permissions=True)
		items = {row.name: row for row in doc.items}
		orders = []
		for group in _groups(normalized):
			po = frappe.new_doc("Purchase Order")
			po.update({key: group[key] for key in HEADER_FIELDS})
			po.company, po.transaction_date = doc.company, nowdate()
			po.schedule_date = min(row["schedule_date"] for row in group["rows"])
			for row in group["rows"]:
				source = items[row["material_request_item"]]
				po.append(
					"items",
					{
						**{
							key: row[key]
							for key in (
								"qty",
								"uom",
								"conversion_factor",
								"rate",
								"warehouse",
								"schedule_date",
							)
						},
						"item_code": source.item_code,
						"stock_uom": source.stock_uom,
						"material_request": doc.name,
						"material_request_item": source.name,
						"sales_order": source.sales_order,
						"sales_order_item": source.sales_order_item,
						"project": source.project,
						"wip_composite_asset": source.wip_composite_asset,
					},
				)
			po.set_missing_values()
			# Explicit reviewed prices/units win over Item Price suggestions.
			for target, row in zip(po.items, group["rows"], strict=True):
				target.update(
					{
						key: row[key]
						for key in ("qty", "uom", "conversion_factor", "rate", "schedule_date", "warehouse")
					}
				)
			if group["tc_name"]:
				po.terms = frappe.db.get_value("Terms and Conditions", group["tc_name"], "terms")
			po.calculate_taxes_and_totals()
			po.insert()  # Standard create/user permissions and validation, never auto-submit.
			orders.append(
				{"name": po.name, "supplier": po.supplier, "currency": po.currency, "status": po.status}
			)
			for target, row in zip(po.items, group["rows"], strict=True):
				batch.append(
					"allocations", {**row, "purchase_order": po.name, "purchase_order_item": target.name}
				)
		result = {"batch": batch.name, "material_request": doc.name, "orders": orders}
		batch.result = json.dumps(result, ensure_ascii=False)
		batch.save(ignore_permissions=True)
		return result
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
		raise
	except Exception:
		frappe.db.rollback(save_point="purchase_allocation")
		raise
