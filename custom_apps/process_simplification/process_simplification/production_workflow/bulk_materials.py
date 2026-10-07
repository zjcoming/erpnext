"""Central material hand-off; every write remains one guarded native document.

The browser owns batching, so each successful document has its own transaction.
Read previews never reserve stock. They are rechecked before creation/posting.
"""

from __future__ import annotations

import hashlib
import json
from math import isfinite

import frappe
from frappe import _
from frappe.utils import cint, flt

from process_simplification.management_access import (
	CAPABILITY_PRODUCTION_REVIEW, CAPABILITY_WAREHOUSE_WORKBENCH,
	user_company_scope, user_has_capability,
)
from process_simplification.production_reporting.dispatch_pool import is_enabled
from process_simplification.production_workflow import service

LIMIT = 20
PURPOSE = "Material Transfer for Manufacture"


def _throw(message, permission=False):
	frappe.throw(_(message), frappe.PermissionError if permission else frappe.ValidationError)


def _access(view=None, company=None):
	if not is_enabled():
		_throw("集中领料尚未启用。", True)
	capabilities = {
		"request": bool(user_has_capability(CAPABILITY_PRODUCTION_REVIEW)),
		"confirm": bool(user_has_capability(CAPABILITY_WAREHOUSE_WORKBENCH)),
	}
	view = view or ("request" if capabilities["request"] else "confirm")
	if view not in capabilities or not capabilities[view]:
		_throw("没有该领料操作的岗位权限。", True)
	scope = user_company_scope()
	if scope is not None and not scope:
		_throw("请先配置可访问的公司。", True)
	if company and scope is not None and company not in scope:
		_throw("没有该公司的领料权限。", True)
	return view, capabilities, scope


def _hash(value):
	return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _names(value):
	value = frappe.parse_json(value) if isinstance(value, str) else value
	if not isinstance(value, list) or not value or len(value) > LIMIT:
		_throw("每批请选择 1 至 20 张单据。")
	if any(not isinstance(name, str) or not name or len(name) > 140 for name in value):
		_throw("单据编号无效。")
	if len(value) != len(set(value)):
		_throw("同一张单据不能重复选择。")
	return value


def _check_scope(doc, scope):
	if scope is not None and doc.company not in scope:
		_throw("没有该公司单据的访问权限。", True)
	frappe.has_permission(doc.doctype, "read", doc=doc, throw=True)


def _work_order(name, scope, *, lock=False):
	if lock:
		frappe.db.get_value("Work Order", name, "name", for_update=True)
	doc = frappe.get_doc("Work Order", name, for_update=lock)
	_check_scope(doc, scope)
	if doc.docstatus != 1 or doc.status in service.TERMINAL_WORK_ORDER_STATUSES or not doc.production_plan:
		_throw("仅支持有效生产计划下尚未完成的正式工单。")
	plan = frappe.get_doc("Production Plan", doc.production_plan, for_update=lock)
	_check_scope(plan, scope)
	if plan.docstatus != 1 or plan.company != doc.company or plan.status in {"Closed", "Cancelled"}:
		_throw("生产计划已关闭或失效，请刷新后核对。")
	return doc


def _request_state(doc, stock_facts):
	groups = service._material_issue_groups(doc, stock_facts)
	return _hash({
		"work_order": doc.name, "company": doc.company, "production_plan": doc.production_plan,
		"qty": flt(doc.qty), "bom_no": doc.bom_no, "wip_warehouse": doc.wip_warehouse,
		"skip_transfer": doc.skip_transfer, "transfer_material_against": doc.transfer_material_against,
		"track_semi_finished_goods": doc.get("track_semi_finished_goods"),
		"materials": sorted((code or "", warehouse or "", flt(group.required),
			flt(group.net_transferred), flt(group.returned_qty)) for (code, warehouse), group in groups.items()),
	})


def _draft_state(doc):
	# Include every persisted field, not just the displayed quantities: changed
	# posting date, accounts or extra costs must also invalidate confirmation.
	return _hash(doc.as_dict())


def _existing_draft(doc, scope, *, lock=False):
	if lock:
		entry = frappe.qb.DocType("Stock Entry")
		rows = (frappe.qb.from_(entry).select(entry.name, entry.custom_process_workflow_action)
			.where((entry.work_order == doc.name) & (entry.purpose == PURPOSE)
				& (entry.docstatus == 0) & (entry.is_return == 0)).for_update().run(as_dict=True))
		if any(row.custom_process_workflow_action != service.ISSUE_ACTION for row in rows):
			_throw("该工单已有其他发料草稿，请先打开原单核对。")
		if len(rows) > 1:
			_throw("该工单存在重复领料草稿，请先打开原单核对。")
		existing = rows[0] if rows else None
	else:
		existing = service._existing_draft_stock_entry(doc.name, PURPOSE, service.ISSUE_ACTION)
	if not existing:
		return None
	entry = frappe.get_doc("Stock Entry", existing.name, for_update=lock)
	_check_scope(entry, scope)
	return entry


def _current_stock_facts(work_order):
	from process_simplification.production_stock_facts import SUPPORTED_PURPOSES, aggregate_work_order_stock_facts
	entry, detail = frappe.qb.DocType("Stock Entry"), frappe.qb.DocType("Stock Entry Detail")
	entries = (frappe.qb.from_(entry).select("*").where((entry.work_order == work_order)
		& (entry.docstatus == 1) & entry.purpose.isin(sorted(SUPPORTED_PURPOSES))).for_update().run(as_dict=True))
	if not entries:
		return {}
	details = (frappe.qb.from_(detail).select("*").where(detail.parent.isin([row.name for row in entries])
		& (detail.docstatus == 1)).for_update().run(as_dict=True))
	return aggregate_work_order_stock_facts(details, entries)


def _priority_caps(company):
	from process_simplification.api.production_readiness import get_production_plan_readiness
	result = {}
	for plans in (get_production_plan_readiness(company=company) or {}).values():
		for plan in plans or []:
			for order in plan.get("work_orders") or []:
				result[order["name"]] = max(flt((order.get("issue_state") or {}).get("additional_issueable_qty")), 0)
	return result


def _request_row(doc, scope, *, caps=None, stock_facts=None, preview_consumed=None):
	stock_facts = stock_facts if stock_facts is not None else service.load_work_order_stock_facts([doc.name])
	remaining = service.remaining_material_issue_qty(doc, stock_facts)
	row = {
		"name": doc.name, "work_order": doc.name, "company": doc.company,
		"production_plan": doc.production_plan, "sales_order": doc.sales_order,
		"production_item": doc.production_item, "item_name": doc.item_name or doc.production_item,
		"qty": flt(doc.qty), "stock_uom": doc.stock_uom, "remaining_qty": remaining,
		"available_qty": 0, "request_qty": 0, "can_select": False,
		"expected_state": {"work_order": _request_state(doc, stock_facts), "draft": None},
		"items": [], "block_message": "", "partial": False,
	}
	if doc.skip_transfer:
		row["block_message"] = _("该工单直接耗料，无需领料申请。")
		return row
	if doc.transfer_material_against == "Job Card" or doc.get("track_semi_finished_goods"):
		row["block_message"] = _("按工序跟踪半成品，请从原工序单办理领料。")
		return row
	existing = _existing_draft(doc, scope)
	if existing:
		row.update(stock_entry=existing.name, request_qty=flt(existing.custom_process_coverage_qty),
			can_select=True, block_message=_("已有领料申请，将复用原单。"))
		row["expected_state"]["draft"] = _draft_state(existing)
		row["items"] = _entry_items(existing)
		row["partial"] = row["request_qty"] + 1e-9 < remaining
		return row
	if remaining <= 1e-9:
		row["block_message"] = _("已完成领料。")
		return row
	available = service._current_available_by_item(doc)
	if preview_consumed:
		available = {key: max(qty - preview_consumed.get(key, 0), 0) for key, qty in available.items()}
	physical = service.issueable_finished_qty(doc, available, stock_facts)
	if caps is None:
		priority = service._production_priority_issue_cap(doc)
		cap = physical if priority is None else min(physical, flt(priority.cap))
	else:
		cap = min(physical, caps.get(doc.name, physical))
	row.update(available_qty=physical, request_qty=cap, can_select=cap > 1e-9, partial=cap + 1e-9 < remaining)
	if cap <= 1e-9:
		row["block_message"] = _("库存不足或已优先分配给其他工单。")
	elif row["partial"]:
		row["block_message"] = _("本次只能部分领料，确认后仍需补齐余料。")
	for (code, warehouse), quantity in service._guided_component_issue_quantities(doc, cap, stock_facts).items():
		row["items"].append({"item_code": code, "item_name": quantity.template.item_name or code,
			"qty": flt(quantity.qty), "uom": quantity.template.stock_uom,
			"source_warehouse": warehouse, "target_warehouse": doc.wip_warehouse,
			"available_qty": flt(available.get((code, warehouse)))})
	return row


def _entry_items(doc):
	return [{"item_code": item.item_code, "item_name": item.item_name or item.item_code,
		"qty": flt(item.transfer_qty), "uom": item.stock_uom or item.uom,
		"source_warehouse": item.s_warehouse, "target_warehouse": item.t_warehouse,
		"batch_no": item.batch_no, "serial_no": item.serial_no,
		"serial_and_batch_bundle": item.serial_and_batch_bundle} for item in doc.items]


def _requires_native_confirmation(doc):
	if any(row.get("batch_no") or row.get("serial_no") or row.get("serial_and_batch_bundle") for row in doc.items):
		return True
	return bool(frappe.get_all("Item", filters={"name": ["in", sorted({row.item_code for row in doc.items})]},
		or_filters={"has_batch_no": 1, "has_serial_no": 1}, pluck="name", limit=1))


def _stock_entry(name, scope, *, lock=False):
	if lock:
		# Match the existing request lock order, avoiding entry -> WO deadlocks.
		work_order = frappe.db.get_value("Stock Entry", name, "work_order")
		if work_order:
			frappe.db.get_value("Work Order", work_order, "name", for_update=True)
		frappe.db.get_value("Stock Entry", name, "name", for_update=True)
	doc = frappe.get_doc("Stock Entry", name, for_update=lock)
	if lock and doc.work_order != work_order:
		_throw("库存单关联工单已变化，请刷新后重试。")
	_check_scope(doc, scope)
	if doc.purpose != PURPOSE or doc.get("is_return") or doc.custom_process_workflow_action != service.ISSUE_ACTION:
		_throw("这里只能确认生产领料申请，请在原单处理其他库存业务。")
	return doc


def _confirmation_row(doc):
	batch = _requires_native_confirmation(doc)
	can_submit = bool(frappe.has_permission("Stock Entry", "submit", doc=doc)
		and frappe.has_permission("Stock Entry", "write", doc=doc))
	return {"name": doc.name, "stock_entry": doc.name, "work_order": doc.work_order,
		"company": doc.company, "request_qty": flt(doc.custom_process_coverage_qty),
		"posting_date": doc.posting_date, "items": _entry_items(doc),
		"expected_state": _draft_state(doc), "requires_native": batch,
		"can_select": doc.docstatus == 0 and can_submit and not batch,
		"block_message": (_("含批次或序列号，请打开原单核对具体批次并确认出库。") if batch else
			(_("没有出库确认权限。") if not can_submit else ""))}


def get_material_workbench(view=None, company=None, search=None, start=0):
	view, capabilities, scope = _access(view, company)
	companies = frappe.get_all("Company", filters={"name": ["in", sorted(scope)]} if scope is not None else {},
		pluck="name", order_by="name", limit_page_length=0)
	selected_companies = [company] if company else companies
	search = str(search or "").strip()[:140]
	start = max(cint(start), 0)
	doctype = "Work Order" if view == "request" else "Stock Entry"
	frappe.has_permission(doctype, "read", throw=True)
	filters = {"company": ["in", selected_companies], "docstatus": 1 if view == "request" else 0}
	if view == "request":
		filters.update(status=["not in", sorted(service.TERMINAL_WORK_ORDER_STATUSES)], production_plan=["is", "set"])
	else:
		filters.update(purpose=PURPOSE, is_return=0, custom_process_workflow_action=service.ISSUE_ACTION)
	search_fields = ("name", "production_item", "item_name", "sales_order") if view == "request" else ("name", "work_order")
	or_filters = [[doctype, key, "like", "%" + search + "%"] for key in search_fields] if search else []
	rows, cursor, more, caps = [], start, False, {}
	while True:
		candidates = frappe.get_list(doctype, filters=filters, or_filters=or_filters, fields=["name", "company"],
			order_by="creation asc, name asc", limit_start=cursor, limit_page_length=50)
		for candidate in candidates:
			if len(rows) == LIMIT:
				more = True
				break
			cursor += 1
			try:
				doc = _work_order(candidate.name, scope) if view == "request" else _stock_entry(candidate.name, scope)
				if view == "request":
					if doc.company not in caps:
						caps[doc.company] = _priority_caps(doc.company)
					row = _request_row(doc, scope, caps=caps[doc.company])
					if row["remaining_qty"] <= 1e-9 and not row.get("stock_entry"):
						continue
				else:
					row = _confirmation_row(doc)
				rows.append(row)
			except frappe.PermissionError:
				continue
			except frappe.ValidationError as error:
				# This document was readable, but an existing malformed/duplicate
				# draft needs attention. Preserve a visible original-form route.
				rows.append({"name": candidate.name, "work_order": candidate.name if view == "request" else None,
					"company": candidate.company, "can_select": False, "block_message": str(error), "items": []})
		if more or len(candidates) < 50:
			break
	return {"view": view, "capabilities": capabilities, "companies": companies, "company": company,
		"rows": rows, "start": start, "next_start": cursor if more else None}


def _same_company(rows):
	if len({row["company"] for row in rows}) > 1:
		_throw("每批只能处理同一公司的单据。")


def preview_material_requests(work_orders):
	_, _, scope = _access("request")
	docs = [_work_order(name, scope) for name in _names(work_orders)]
	_same_company([{"company": doc.company} for doc in docs])
	caps = _priority_caps(docs[0].company)
	facts = service.load_work_order_stock_facts([doc.name for doc in docs])
	rows, consumed = [], {}
	for doc in docs:
		row = _request_row(doc, scope, caps=caps, stock_facts=facts, preview_consumed=consumed)
		rows.append(row)
		# A preview must not promise the same shared raw stock to every selected
		# WO. Existing hard reservations already reduce other orders' availability;
		# subtract only each new request's additional claim on the free pool.
		if row.get("stock_entry") or not row["can_select"]:
			continue
		for item in row["items"]:
			key = (item["item_code"], item["source_warehouse"])
			owned = sum(service._active_work_order_item_reserved_qty(required)
				for required in doc.required_items if (required.item_code, required.source_warehouse) == key)
			consumed[key] = consumed.get(key, 0) + max(flt(item["qty"]) - owned, 0)
	return {"rows": rows}


def request_material(work_order, expected_state, qty, allow_partial=0):
	_, _, scope = _access("request")
	doc = _work_order(work_order, scope, lock=True)
	state = frappe.parse_json(expected_state) if isinstance(expected_state, str) else expected_state
	facts = _current_stock_facts(doc.name)
	if not isinstance(state, dict) or state.get("work_order") != _request_state(doc, facts):
		_throw("工单或已发料数量已变化，请重新预览。")
	try:
		quantity = float(qty)
	except (TypeError, ValueError):
		_throw("领料覆盖数量无效。")
	if not isfinite(quantity) or quantity <= 0:
		_throw("领料覆盖数量必须大于零。")
	# Native service reads use the transaction snapshot. Refuse a transaction
	# that waited behind a stock change, rather than let an old snapshot drive
	# its quantity calculations. Current-read guards above never use that view.
	if _request_state(doc, service.load_work_order_stock_facts([doc.name])) != _request_state(doc, facts):
		_throw("库存刚刚更新，请刷新后重新申请。")
	existing = _existing_draft(doc, scope, lock=True)
	if state.get("draft") and (not existing or state["draft"] != _draft_state(existing)):
		_throw("已有申请已被修改，请重新预览。")
	if existing and abs(flt(existing.custom_process_coverage_qty) - quantity) > 1e-6:
		_throw("已有申请的数量与本次确认不同，请重新预览。")
	if existing:
		service.validate_guided_stock_entry_before_submit(existing)
		return {"status": "requested", "work_order": work_order, "stock_entry": existing.name, "reused": True}
	# The original entry point owns priority, partial-issue consent, hard stock
	# reservations, notifications and validation. Never override priority here.
	return {"status": "requested", "work_order": work_order,
		**service.request_material_issue(work_order, qty=quantity, allow_partial=cint(allow_partial), override_priority=0)}


def preview_material_submissions(stock_entries):
	_, _, scope = _access("confirm")
	rows = [_confirmation_row(_stock_entry(name, scope)) for name in _names(stock_entries)]
	_same_company(rows)
	return {"rows": rows}


def _assert_physical_stock(doc, work_order):
	from process_simplification.production_workflow.stock_reservation import locked_available_qty
	requested = {}
	for row in doc.items:
		key = (row.item_code, row.s_warehouse)
		requested[key] = requested.get(key, 0) + flt(row.transfer_qty)
	for key in sorted(requested):
		free = locked_available_qty(*key)
		actual = flt(frappe.db.get_value("Bin", {"item_code": key[0], "warehouse": key[1]}, "actual_qty", for_update=True))
		owned = _current_owned_reservation_qty(work_order.name, *key)
		if requested[key] > min(actual, free + owned) + 1e-6:
			_throw("库存刚刚发生变化，本次出库量不足，请刷新后核对。")


def _current_owned_reservation_qty(work_order, item_code, warehouse):
	reservation = frappe.qb.DocType("Stock Reservation Entry")
	rows = (frappe.qb.from_(reservation).select(reservation.reserved_qty, reservation.transferred_qty,
		reservation.consumed_qty, reservation.delivered_qty).where((reservation.docstatus == 1)
		& (reservation.voucher_type == "Work Order") & (reservation.voucher_no == work_order)
		& (reservation.item_code == item_code) & (reservation.warehouse == warehouse)).for_update().run(as_dict=True))
	return sum(max(flt(row.reserved_qty) - flt(row.transferred_qty)
		- flt(row.consumed_qty) - flt(row.delivered_qty), 0) for row in rows)


def submit_material(stock_entry, expected_state):
	_, _, scope = _access("confirm")
	doc = _stock_entry(stock_entry, scope, lock=True)
	for ptype in ("write", "submit"):
		frappe.has_permission("Stock Entry", ptype, doc=doc, throw=True)
	if doc.docstatus == 1:
		return {"status": "submitted", "stock_entry": doc.name, "reused": True}
	if doc.docstatus != 0 or not expected_state or expected_state != _draft_state(doc):
		_throw("单据已被修改、撤回或取消，请重新预览后确认。")
	if _requires_native_confirmation(doc):
		_throw("含批次或序列号，请在原库存单核对后提交。")
	work_order = _work_order(doc.work_order, scope, lock=True)
	current_facts = _current_stock_facts(work_order.name)
	if _request_state(work_order, current_facts) != _request_state(work_order, service.load_work_order_stock_facts([work_order.name])):
		_throw("库存刚刚更新，请刷新后重新确认。")
	service.validate_guided_stock_entry_before_submit(doc)
	_assert_physical_stock(doc, work_order)
	doc.submit()
	return {"status": "submitted", "stock_entry": doc.name, "reused": False}


def get_material_outcome(work_order=None, stock_entry=None):
	_, _, scope = _access("confirm" if stock_entry else "request")
	if stock_entry:
		doc = _stock_entry(stock_entry, scope)
		return {"status": "submitted" if doc.docstatus == 1 else "unconfirmed", "stock_entry": doc.name}
	doc = _work_order(work_order, scope)
	entry = _existing_draft(doc, scope)
	if entry:
		return {"status": "exists", "stock_entry": entry.name}
	return {"status": "unconfirmed"}
