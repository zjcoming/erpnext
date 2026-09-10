from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, nowdate

from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry
from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
	get_available_qty_to_reserve,
)
from process_simplification.api.setup import get_default_bom
from process_simplification.production_reporting.domain import (
	require_reviewer,
	reviewer_companies,
)
from process_simplification.production_stock_facts import (
	get_work_order_stock_fact,
	load_work_order_stock_facts,
)
from process_simplification.request_transaction import retry_request_transaction

RECEIPT_ACTION = "Receipt Request"
ISSUE_ACTION = "Material Issue Request"
REPLENISHMENT_RECEIPT_ACTION = "Replenishment Receipt"
TERMINAL_WORK_ORDER_STATUSES = {"Completed", "Stopped", "Closed", "Cancelled"}


def _throw(message: str):
	frappe.throw(_(message))


def _require_stock_reservation_enabled():
	if not frappe.db.get_single_value("Stock Settings", "enable_stock_reservation"):
		_throw("请先在库存设置中启用库存预留，才能锁定发料或补产物料。")


def _work_order_for_action(work_order: str, *, for_update: bool = True):
	require_reviewer(for_update=for_update)
	if for_update:
		frappe.db.get_value("Work Order", work_order, "name", for_update=True)
	doc = frappe.get_doc("Work Order", work_order)
	frappe.has_permission("Work Order", "read", doc=doc, throw=True)
	companies = reviewer_companies()
	if companies is not None and doc.company not in companies:
		frappe.throw(_("You are not permitted to manage this Work Order company."), frappe.PermissionError)
	if doc.docstatus != 1 or doc.status in {"Stopped", "Closed", "Cancelled"}:
		_throw("当前工单不可执行该操作。")
	return doc


def _resolve_work_order_stock_facts(work_order, stock_facts=None):
	"""Return an authoritative submitted-ledger snapshot when a WO is persisted."""
	if stock_facts is not None:
		return stock_facts, True
	work_order_name = work_order.get("name")
	if work_order_name:
		return load_work_order_stock_facts([work_order_name]), True
	# Pure calculation fixtures historically contain only Work Order Item child
	# aggregates. Keep that compatibility, but never use it for a persisted WO.
	return {}, False


def _material_issue_groups(work_order, stock_facts=None):
	stock_facts, facts_loaded = _resolve_work_order_stock_facts(work_order, stock_facts)
	groups = {}
	for item in work_order.get("required_items") or []:
		include = item.get("include_item_in_manufacturing")
		if include is not None and not int(include):
			continue
		required = flt(item.get("required_qty"))
		if required <= 0:
			continue
		key = (item.get("item_code"), item.get("source_warehouse"))
		group = groups.setdefault(
			key,
			frappe._dict(
				required=0.0,
				gross_issued_qty=0.0,
				returned_qty=0.0,
				net_transferred=0.0,
				template=item,
			),
		)
		group.required += required
		if not facts_loaded:
			# Legacy child fields repeat the same aggregate on operation-split rows.
			group.gross_issued_qty = max(
				group.gross_issued_qty, flt(item.get("transferred_qty"))
			)
			group.returned_qty = max(
				group.returned_qty, flt(item.get("returned_qty"))
			)
			group.net_transferred = max(
				group.net_transferred,
				max(flt(item.get("transferred_qty")) - flt(item.get("returned_qty")), 0),
			)
	if facts_loaded:
		work_order_name = work_order.get("name")
		for (item_code, warehouse), group in groups.items():
			fact = get_work_order_stock_fact(
				stock_facts, work_order_name, item_code, warehouse
			)
			group.gross_issued_qty = flt(fact.gross_issued_qty)
			group.returned_qty = flt(fact.returned_qty)
			group.net_transferred = max(flt(fact.net_issued_qty), 0)
	return groups


def _has_material_return_gap(work_order, stock_facts=None) -> bool:
	return any(
		group.returned_qty > 0
		and max(group.required - group.net_transferred, 0) > 1e-9
		for group in _material_issue_groups(work_order, stock_facts).values()
	)


def _dispatch_state(can_dispatch: bool, block_code=None, block_message=None, **details):
	return frappe._dict(
		can_dispatch=bool(can_dispatch),
		block_code=block_code,
		block_message=block_message,
		**details,
	)


def _evaluate_work_order_dispatch(doc):
	"""Evaluate the material gate without creating reservations or changing stock."""
	required_rows = [
		row
		for row in doc.required_items
		if row.get("include_item_in_manufacturing") and flt(row.get("required_qty")) > 0
	]
	if not required_rows:
		return _dispatch_state(True)
	stock_facts, facts_loaded = _resolve_work_order_stock_facts(doc)
	if doc.get("skip_transfer"):
		# Direct-consumption Work Orders have no WIP transfer. They still must not
		# notify a worker when another demand has consumed the source stock.
		# Global availability excludes hard reservations; add back only the
		# reservations owned by this exact Work Order item.
		groups = {}
		for row in required_rows:
			# Native direct consumption uses WIP when ``from_wip_warehouse``
			# is enabled; otherwise it consumes from the required row's source.
			# Availability, owned reservations and the new lock must all use that
			# same effective warehouse.
			consumption_warehouse = (
				doc.get("wip_warehouse")
				if doc.get("from_wip_warehouse")
				else row.get("source_warehouse")
			)
			key = (row.get("item_code"), consumption_warehouse)
			group = groups.setdefault(
				key,
				{"required": 0.0, "consumed": 0.0, "owned_reserved": 0.0, "items": []},
			)
			group["items"].append(row)
			group["required"] += flt(row.get("required_qty"))
			# ERPNext writes the aggregate consumed quantity back to every
			# operation-split row for the same component.
			if not facts_loaded:
				group["consumed"] = max(group["consumed"], flt(row.get("consumed_qty")))
			group["owned_reserved"] += _active_work_order_item_reserved_qty(
				row, consumption_warehouse
			)
		if facts_loaded:
			for (item_code, warehouse), group in groups.items():
				group["consumed"] = flt(
					get_work_order_stock_fact(
						stock_facts, doc.get("name"), item_code, warehouse
					).consumed_qty
				)
		priority = _production_priority_issue_cap(doc)
		if priority is not None and not priority.can_dispatch:
			return _dispatch_state(
				False,
				"DIRECT_PRIORITY_CONFLICT",
				_("该直耗料工单的现场库存已优先分配给其他工单，不能直接派工抢占。"),
			)
		short_keys = []
		for key, group in groups.items():
			if not all(key):
				short_keys.append(key)
				continue
			available = max(flt(get_available_qty_to_reserve(*key)), 0) + flt(
				group["owned_reserved"]
			)
			remaining = max(flt(group["required"]) - flt(group["consumed"]), 0)
			if available + 1e-6 < remaining:
				short_keys.append(key)
		if short_keys:
			return _dispatch_state(
				False,
				"DIRECT_MATERIAL_SHORTAGE",
				_(
					"Direct-consumption materials are no longer sufficient for this Work Order. "
					"Replenish or reserve the missing material before formal worker assignment."
				),
			)
		return _dispatch_state(True, direct_groups=groups)

	issued_fraction, _potential_fraction = _material_issue_coverage(
		doc, stock_facts=stock_facts if facts_loaded else None
	)
	if issued_fraction + 1e-6 < 1:
		return _dispatch_state(
			False,
			"MATERIAL_NOT_FULLY_ISSUED",
			_(
				"Materials must be fully issued to WIP before formal worker assignment. "
				"Create and submit the material issue request first."
			),
		)
	return _dispatch_state(True)


def get_work_order_dispatch_state(work_order) -> frappe._dict:
	"""Return the read-only dispatch gate used by assignment context UIs."""
	doc = (
		frappe.get_doc("Work Order", work_order)
		if isinstance(work_order, str)
		else work_order
	)
	state = _evaluate_work_order_dispatch(doc)
	return frappe._dict(
		can_dispatch=state.can_dispatch,
		block_code=state.block_code,
		block_message=state.block_message,
	)


def assert_work_order_dispatch_ready(work_order: str):
	"""Do not notify workers until the Work Order's material boundary is safe."""
	doc = frappe.get_doc("Work Order", work_order)
	state = _evaluate_work_order_dispatch(doc)
	if not state.can_dispatch:
		frappe.throw(state.block_message)
	if doc.get("skip_transfer"):
		# Availability is only a momentary observation. Formal dispatch is an
		# opening commitment, so turn every still-needed direct-consumption row
		# into an exact-source hard reservation in this same transaction.
		_require_stock_reservation_enabled()
		for key, group in (state.get("direct_groups") or {}).items():
			remaining = max(flt(group["required"]) - flt(group["consumed"]), 0)
			deficit = max(remaining - flt(group["owned_reserved"]), 0)
			if deficit <= 1e-9:
				continue
			representative = sorted(
				group["items"],
				key=lambda item: (int(item.get("idx") or 0), str(item.get("name") or "")),
			)[0]
			sre = _new_work_order_reservation(
				work_order=doc,
				work_order_item=representative,
				qty=deficit,
				voucher_qty=group["required"],
				warehouse=key[1],
			)
			if not sre or flt(sre.reserved_qty) + 1e-9 < deficit:
				_throw("直耗料库存刚刚发生变化，未能完成开工锁料，请刷新后重试。")


def _existing_draft_stock_entry(work_order: str, purpose: str, action: str):
	rows = frappe.get_all(
		"Stock Entry",
		filters={
			"work_order": work_order,
			"purpose": purpose,
			"docstatus": 0,
			"is_return": 0,
		},
		fields=["name", "fg_completed_qty", "custom_process_workflow_action"],
		order_by="modified desc",
		limit=0,
	)
	guided = [row for row in rows if row.get("custom_process_workflow_action") == action]
	unmanaged = [row for row in rows if row.get("custom_process_workflow_action") != action]
	if unmanaged:
		_throw(
			"该工单已有未纳入流程的同用途库存草稿，请先让库房处理，或由生产主管确认后撤回，再重新申请。"
		)
	if len(guided) > 1:
		_throw("该工单存在重复的库存草稿，请先由库房处理或删除重复单据。")
	return guided[0] if guided else None


def _job_card_completion(work_order: str) -> frappe._dict:
	rows = frappe.get_all(
		"Job Card",
		filters={"work_order": work_order, "is_corrective_job_card": 0, "docstatus": ["<", 2]},
		fields=["name", "docstatus", "status"],
		limit=0,
	)
	total = len(rows)
	completed = sum(
		1 for row in rows if int(row.get("docstatus") or 0) == 1 and row.get("status") == "Completed"
	)
	return frappe._dict(total=total, completed=completed, all_completed=bool(total and completed == total))


def _all_operations_completed(work_order) -> bool:
	operation_count = frappe.db.count("Work Order Operation", {"parent": work_order.name})
	if not operation_count:
		return True
	completion = _job_card_completion(work_order.name)
	return bool(completion.total >= operation_count and completion.all_completed)


def receipt_remaining_qty(work_order, stock_facts=None) -> float:
	"""Finished quantity still allowed after already booked output and process loss."""
	if work_order.get("skip_transfer"):
		transfer_base = flt(work_order.get("qty"))
	else:
		# The header is gross and can exceed planned quantity after material was
		# returned and re-issued. Receipt eligibility must follow row-level net
		# BOM coverage instead.
		current_coverage, _potential_coverage = _material_issue_coverage(
			work_order, stock_facts=stock_facts
		)
		transfer_base = flt(work_order.get("qty")) * current_coverage
	return max(
		transfer_base - flt(work_order.get("produced_qty"))
		- flt(work_order.get("process_loss_qty")),
		0,
	)


def _material_issue_coverage(
	work_order, available_by_item=None, stock_facts=None
) -> tuple[float, float]:
	"""Return current and potential full-BOM coverage using net issued quantities."""
	groups = _material_issue_groups(work_order, stock_facts)
	if not groups:
		return 1.0, 1.0
	current = 1.0
	potential = 1.0
	for key, group in groups.items():
		required = group["required"]
		issued = group["net_transferred"]
		current = min(current, issued / required)
		available = max(flt((available_by_item or {}).get(key)), 0)
		potential = min(potential, (issued + available) / required)
	return max(min(current, 1.0), 0), max(min(potential, 1.0), 0)


def remaining_material_issue_qty(work_order, stock_facts=None) -> float:
	current, _ = _material_issue_coverage(work_order, stock_facts=stock_facts)
	return max(flt(work_order.get("qty")) * (1 - current), 0)


def issueable_finished_qty(
	work_order,
	available_by_item: dict[tuple[str, str], float],
	stock_facts=None,
) -> float:
	"""Return the safe FG-equivalent quantity that can be transferred now."""
	current, potential = _material_issue_coverage(
		work_order, available_by_item, stock_facts=stock_facts
	)
	return max(flt(work_order.get("qty")) * (potential - current), 0)


def _current_available_by_item(work_order) -> dict[tuple[str, str], float]:
	owned_reservations = {}
	for item in work_order.get("required_items") or []:
		key = (item.get("item_code"), item.get("source_warehouse"))
		if not all(key):
			continue
		# Work Order Item.stock_reserved_qty combines every warehouse for the
		# voucher row, including WIP reservations created after a partial issue.
		# Only an active SRE in this row's source warehouse is issueable here.
		owned_reservations[key] = owned_reservations.get(
			key, 0
		) + _active_work_order_item_reserved_qty(item)
	available = {}
	for key, owned_reserved in owned_reservations.items():
		# Global availability excludes every hard reservation. Add this exact Work
		# Order row's remaining reservation back because it belongs to the caller.
		available[key] = max(
			flt(get_available_qty_to_reserve(*key)) + owned_reserved,
			0,
		)
	return available


def _production_priority_issue_cap(work_order):
	"""Return this Work Order's current soft-allocation cap and provenance."""
	if not work_order.get("production_plan"):
		return None
	from process_simplification.api.production_readiness import (
		get_production_plan_readiness,
	)

	readiness = get_production_plan_readiness(company=work_order.company)
	for plans in (readiness or {}).values():
		for plan in plans or []:
			for candidate in plan.get("work_orders") or []:
				if candidate.get("name") != work_order.name:
					continue
				conflicts = []
				for item in candidate.get("required_items") or []:
					conflict = item.get("allocation_conflict") or {}
					for source in conflict.get("sources") or []:
						conflicts.append(
							{
								"item_code": item.get("item_code"),
								"warehouse": item.get("source_warehouse"),
								**dict(source),
							}
						)
				return frappe._dict(
					cap=max(flt((candidate.get("issue_state") or {}).get("additional_issueable_qty")), 0),
					conflicts=conflicts,
					can_dispatch=bool(candidate.get("can_dispatch")),
				)
	return None


def _insert_guided_stock_entry(
	work_order, purpose: str, qty: float, action: str, *, is_additional_transfer_entry: bool = False
):
	entry = frappe.get_doc(
		make_stock_entry(
			work_order.name,
			purpose,
			qty=qty,
			is_additional_transfer_entry=is_additional_transfer_entry,
		)
	)
	entry.custom_process_workflow_action = action
	entry.custom_process_requested_by = frappe.session.user
	entry.remarks = _("Created from the simplified production hand-off workflow.")
	entry.insert(ignore_permissions=True)
	return entry


def _guided_component_issue_quantities(
	work_order, finished_qty: float, stock_facts=None
) -> dict:
	"""Return component quantities that raise the minimum net BOM coverage.

	This differs from ERPNext's normal pending calculation only after a material
	return: core stores gross transfer and return separately, so the normal
	``required - transferred`` expression no longer exposes the net gap.
	"""
	groups = {
		key: group
		for key, group in _material_issue_groups(work_order, stock_facts).items()
		if all(key)
	}
	if not groups or flt(work_order.get("qty")) <= 0:
		return {}
	current_fraction = min(
		min(group["net_transferred"] / group["required"], 1) for group in groups.values()
	)
	target_fraction = min(
		current_fraction + flt(finished_qty) / flt(work_order.get("qty")), 1
	)
	result = {}
	for key, group in groups.items():
		qty = max(group["required"] * target_fraction - group["net_transferred"], 0)
		if qty > 1e-9:
			result[key] = frappe._dict(qty=qty, template=group["template"])
	return result


def _insert_guided_issue_stock_entry(work_order, finished_qty: float, stock_facts=None):
	"""Create a transfer draft, rebuilding rows from net coverage after returns."""
	has_return_gap = _has_material_return_gap(work_order, stock_facts)
	entry = frappe.get_doc(
		make_stock_entry(
			work_order.name,
			"Material Transfer for Manufacture",
			qty=finished_qty,
			is_additional_transfer_entry=has_return_gap,
		)
	)
	entry.custom_process_workflow_action = ISSUE_ACTION
	entry.custom_process_requested_by = frappe.session.user
	# Keep the business quantity separate from ERPNext's header counter.  A
	# replacement transfer after a submitted return must carry zero in
	# ``fg_completed_qty`` or core treats it as extra material beyond the Work
	# Order allowance.  The component rows still restore exactly this much net
	# BOM coverage and this immutable field keeps that intent auditable.
	entry.custom_process_coverage_qty = flt(finished_qty)
	entry.remarks = _("Created from the simplified production hand-off workflow.")
	if has_return_gap:
		entry.fg_completed_qty = 0
		entry.set("items", [])
		for (_item_code, _warehouse), details in _guided_component_issue_quantities(
			work_order, finished_qty, stock_facts
		).items():
			template = details.template
			entry.add_to_stock_entry_detail(
				{
					template.item_code: {
						"item_code": template.item_code,
						"item_name": template.get("item_name"),
						"description": template.get("description"),
						"stock_uom": template.get("stock_uom"),
						"uom": template.get("stock_uom"),
						"conversion_factor": 1,
						"qty": flt(details.qty),
						"from_warehouse": template.source_warehouse,
						"to_warehouse": work_order.wip_warehouse,
						"allow_alternative_item": template.get("allow_alternative_item"),
					}
				}
			)
		if not entry.items:
			_throw("退料后的净发料缺口无法生成库存明细，请刷新工单后重试。")
	entry.insert(ignore_permissions=True)
	return entry


@frappe.whitelist(methods=["POST"])
def request_manufacture(work_order: str):
	"""Create or reuse a draft Manufacture Stock Entry for warehouse review."""
	doc = _work_order_for_action(work_order)
	existing = _existing_draft_stock_entry(doc.name, "Manufacture", RECEIPT_ACTION)
	if existing:
		validate_guided_stock_entry_before_submit(frappe.get_doc("Stock Entry", existing.name))
		return {"stock_entry": existing.name, "reused": True}
	if doc.status == "Completed":
		_throw("当前工单已经完成入库。")
	if doc.get("track_semi_finished_goods"):
		_throw("该工单按工序跟踪半成品，请继续使用 Job Card 的标准半成品入库路径。")
	if not _all_operations_completed(doc):
		_throw("尚有工序未完成，不能发起完工入库申请。")
	stock_facts = load_work_order_stock_facts([doc.name])
	qty = receipt_remaining_qty(doc, stock_facts)
	if qty <= 0:
		_throw("当前没有可申请入库的完工数量。")
	entry = _insert_guided_stock_entry(doc, "Manufacture", qty, RECEIPT_ACTION)
	from process_simplification.notifications import notify_production_stock_request

	notify_production_stock_request(entry, request_type="receipt")
	return {"stock_entry": entry.name, "qty": qty, "reused": False}


@frappe.whitelist(methods=["POST"])
@retry_request_transaction
def request_material_issue(
	work_order: str,
	allow_partial: int | str = 0,
	qty: float | str | None = None,
	override_priority: int | str = 0,
):
	"""Create or reuse a draft material transfer, rechecking physical stock."""
	doc = _work_order_for_action(work_order)
	existing = _existing_draft_stock_entry(
		doc.name, "Material Transfer for Manufacture", ISSUE_ACTION
	)
	if existing:
		validate_guided_stock_entry_before_submit(frappe.get_doc("Stock Entry", existing.name))
		return {"stock_entry": existing.name, "reused": True}
	_require_stock_reservation_enabled()
	if doc.status == "Completed":
		_throw("已完成工单不能再次申请发料。")
	if doc.skip_transfer:
		_throw("该工单设置为跳过在制品发料，无需创建发料申请。")
	if doc.get("transfer_material_against") == "Job Card" or doc.get("track_semi_finished_goods"):
		_throw("该工单按 Job Card 发料，请在对应工序中使用标准发料路径。")
	stock_facts = load_work_order_stock_facts([doc.name])
	remaining_fg = remaining_material_issue_qty(doc, stock_facts)
	if remaining_fg <= 0:
		_throw("该工单已经完成发料。")
	physical_issueable_qty = issueable_finished_qty(
		doc, _current_available_by_item(doc), stock_facts
	)
	if physical_issueable_qty <= 0:
		_throw("当前没有可发物料，请先补料或调整物料预留。")
	priority = _production_priority_issue_cap(doc)
	priority_cap = physical_issueable_qty if priority is None else min(
		physical_issueable_qty, flt(priority.cap)
	)
	priority_overridden = bool(
		int(override_priority or 0) and priority_cap + 1e-9 < physical_issueable_qty
	)
	allowed_qty = physical_issueable_qty if int(override_priority or 0) else priority_cap
	if allowed_qty <= 0:
		_throw("当前物料已分配给更高优先级工单；如确需抢占，请使用明确的优先级覆盖操作。")
	requested_qty = allowed_qty if qty in (None, "") else flt(qty)
	if requested_qty <= 0 or requested_qty > allowed_qty + 1e-9:
		_throw("申请数量超过当前实际可锁定物料，请刷新后重试。")
	partial = requested_qty + 1e-9 < remaining_fg
	if partial and not int(allow_partial or 0):
		frappe.throw(
			_("当前只能部分发料：可覆盖 {0}，工单剩余 {1}。请确认部分发料后重试。").format(
				flt(requested_qty, 6), flt(remaining_fg, 6)
			)
		)
	entry = _insert_guided_issue_stock_entry(doc, requested_qty, stock_facts)
	_reserve_issue_request_rows(entry, doc)
	if priority_overridden:
		affected = ", ".join(
			filter(
				None,
				[
					"{0}/{1} {2}".format(
						source.get("work_order") or "-",
						source.get("sales_order") or "-",
						flt(source.get("impact_qty"), 6),
					)
					for source in (priority.conflicts or [])
				],
			)
		) or _("higher-priority allocation")
		doc.add_comment(
			"Info",
			_(
				"Production priority allocation was explicitly overridden when requesting "
				"material issue {0}. Affected allocation: {1}."
			).format(entry.name, affected),
		)
	from process_simplification.notifications import notify_production_stock_request

	notify_production_stock_request(entry, request_type="issue")
	return {
		"stock_entry": entry.name,
		"qty": requested_qty,
		"remaining_qty": max(remaining_fg - requested_qty, 0),
		"partial": partial,
		"priority_overridden": priority_overridden,
		"reused": False,
	}


def _item_stock_details(item_code: str):
	return frappe.db.get_value(
		"Item",
		item_code,
		["stock_uom", "has_serial_no", "has_batch_no"],
		as_dict=True,
	) or frappe._dict()


def _new_work_order_reservation(
	*,
	work_order,
	work_order_item,
	qty: float,
	voucher_qty: float | None = None,
	warehouse: str | None = None,
	from_stock_entry=None,
	from_production_plan=None,
	from_detail=None,
):
	if qty <= 0:
		return None
	details = _item_stock_details(work_order_item.item_code)
	reservation_warehouse = warehouse or work_order_item.source_warehouse
	available = max(
		flt(get_available_qty_to_reserve(work_order_item.item_code, reservation_warehouse)),
		0,
	)
	qty = min(flt(qty), available)
	if qty <= 0:
		return None
	frappe.db.set_value("Work Order", work_order.name, "reserve_stock", 1, update_modified=False)
	sre = frappe.new_doc("Stock Reservation Entry")
	sre.update(
		{
			"voucher_type": "Work Order",
			"voucher_no": work_order.name,
			"voucher_detail_no": work_order_item.name,
			"item_code": work_order_item.item_code,
			"warehouse": reservation_warehouse,
			"available_qty": available,
			"voucher_qty": flt(voucher_qty or work_order_item.required_qty),
			"reserved_qty": qty,
			"company": work_order.company,
			"stock_uom": details.get("stock_uom"),
			"has_serial_no": details.get("has_serial_no"),
			"has_batch_no": details.get("has_batch_no"),
			"reservation_based_on": (
				"Serial and Batch"
				if details.get("has_serial_no") or details.get("has_batch_no")
				else "Qty"
			),
			"from_voucher_type": "Stock Entry" if from_stock_entry else ("Production Plan" if from_production_plan else None),
			"from_voucher_no": from_stock_entry or from_production_plan,
			"from_voucher_detail_no": from_detail,
		}
	)
	sre.flags.ignore_permissions = True
	sre.insert(ignore_permissions=True)
	sre.submit()
	return sre


def _reserve_issue_request_rows(stock_entry, work_order):
	"""Turn a warehouse hand-off request into a hard, row-level stock lock."""
	items_by_key = {}
	for item in work_order.required_items:
		if not item.get("include_item_in_manufacturing"):
			continue
		items_by_key.setdefault((item.item_code, item.source_warehouse), []).append(item)
	requested_by_key = {}
	first_detail_by_key = {}
	for row in stock_entry.get("items") or []:
		if not row.s_warehouse or flt(row.transfer_qty) <= 0:
			continue
		key = (row.item_code, row.s_warehouse)
		if key not in items_by_key:
			continue
		requested_by_key[key] = requested_by_key.get(key, 0) + flt(row.transfer_qty)
		first_detail_by_key.setdefault(key, row.name)
	for key, requested in requested_by_key.items():
		work_order_items = items_by_key[key]
		existing_reserved = sum(
			_active_work_order_item_reserved_qty(item) for item in work_order_items
		)
		qty_to_reserve = max(requested - existing_reserved, 0)
		# ERPNext writes one aggregate transfer quantity to every operation-split
		# Work Order Item. Keep one aggregate SRE on a stable representative row
		# and give it the group's actual voucher capacity.
		representative = sorted(
			work_order_items,
			key=lambda item: (int(item.get("idx") or 0), str(item.get("name") or "")),
		)[0]
		voucher_qty = sum(flt(item.required_qty) for item in work_order_items)
		sre = _new_work_order_reservation(
			work_order=work_order,
			work_order_item=representative,
			qty=qty_to_reserve,
			voucher_qty=voucher_qty,
			from_stock_entry=stock_entry.name,
			from_detail=first_detail_by_key[key],
		) if qty_to_reserve > 0 else None
		new_reserved = flt(sre.reserved_qty) if sre else 0
		if existing_reserved + new_reserved + 1e-9 < requested:
			_throw("库存刚刚发生变化，无法锁定本次全部发料物料，请刷新后重试。")


def _active_work_order_item_reserved_qty(work_order_item, warehouse: str | None = None) -> float:
	reservation_warehouse = warehouse or work_order_item.source_warehouse
	rows = frappe.get_all(
		"Stock Reservation Entry",
		filters={
			"docstatus": 1,
			"voucher_type": "Work Order",
			"voucher_no": work_order_item.parent,
			"voucher_detail_no": work_order_item.name,
			"item_code": work_order_item.item_code,
			"warehouse": reservation_warehouse,
		},
		fields=["reserved_qty", "transferred_qty", "consumed_qty", "delivered_qty"],
		limit=0,
	)
	return sum(
		max(
			flt(row.reserved_qty)
			- flt(row.transferred_qty)
			- flt(row.consumed_qty)
			- flt(row.delivered_qty),
			0,
		)
		for row in rows
	)


def _validate_guided_receipt_rows(stock_entry, work_order):
	finished_rows = [row for row in stock_entry.get("items") or [] if row.get("is_finished_item")]
	if not finished_rows:
		_throw("完工入库申请缺少主成品明细。")
	for row in finished_rows:
		if row.get("item_code") != work_order.get("production_item"):
			_throw("完工入库主成品与生产工单不一致。")
		if row.get("t_warehouse") != work_order.get("fg_warehouse") or row.get("s_warehouse"):
			_throw("完工入库主成品必须进入生产工单指定的成品仓。")
	expected_output_qty = max(
		flt(stock_entry.get("fg_completed_qty")) - flt(stock_entry.get("process_loss_qty")),
		0,
	)
	actual_output_qty = sum(flt(row.get("transfer_qty")) for row in finished_rows)
	if abs(actual_output_qty - expected_output_qty) > 1e-6:
		_throw("完工入库主成品数量与工单完成数量不一致。")
	for row in stock_entry.get("items") or []:
		if (
			row.get("t_warehouse")
			and not row.get("s_warehouse")
			and not row.get("is_finished_item")
			and not row.get("secondary_item_type")
			and not row.get("is_legacy_scrap_item")
		):
			_throw("完工入库含有未标识为副产物或废料的额外入库物料。")


def _validate_guided_issue_targets(stock_entry, work_order):
	expected_warehouse = work_order.get("wip_warehouse")
	if not expected_warehouse:
		_throw("生产工单没有在制品仓，不能提交发料申请。")
	for row in stock_entry.get("items") or []:
		if row.get("t_warehouse") != expected_warehouse:
			_throw("生产发料的所有物料都必须进入该工单的在制品仓。")


def validate_guided_stock_entry_before_submit(stock_entry):
	"""Fail closed if a warehouse edit broke the reviewed request contract."""
	action = stock_entry.get("custom_process_workflow_action")
	if action not in {RECEIPT_ACTION, ISSUE_ACTION}:
		return
	# Stock Entry.validate may normalize header values after the mixin's first
	# identity check. Recheck against the persisted request here, immediately
	# before submit, so warehouse edits cannot silently change the contract.
	validate_guided_stock_entry_identity(stock_entry)
	if not stock_entry.get("work_order"):
		_throw("流程库存单必须关联生产工单。")
	frappe.db.get_value("Work Order", stock_entry.work_order, "name", for_update=True)
	work_order = frappe.get_doc("Work Order", stock_entry.work_order)
	if work_order.docstatus != 1 or work_order.status in {"Stopped", "Closed", "Cancelled"}:
		_throw("关联工单当前不可提交该库存单。")
	if stock_entry.company != work_order.company:
		_throw("库存单公司与生产工单不一致。")
	stock_facts = load_work_order_stock_facts([work_order.name])

	if action == RECEIPT_ACTION:
		if stock_entry.purpose != "Manufacture":
			_throw("完工入库申请不能改为其他库存用途。")
		if not _all_operations_completed(work_order):
			_throw("工序状态已经变化，全部工序完成前不能提交入库。")
		_validate_guided_receipt_rows(stock_entry, work_order)
		maximum = receipt_remaining_qty(work_order, stock_facts)
		if flt(stock_entry.fg_completed_qty) <= 0 or flt(stock_entry.fg_completed_qty) > maximum + 1e-9:
			_throw("入库数量已超过工单当前可入库数量，请重新生成申请。")
		return

	if stock_entry.purpose != "Material Transfer for Manufacture":
		_throw("生产发料申请不能改为其他库存用途。")
	_validate_guided_issue_targets(stock_entry, work_order)
	reservations = frappe.get_all(
		"Stock Reservation Entry",
		filters={
			"docstatus": 1,
			"voucher_type": "Work Order",
			"voucher_no": work_order.name,
		},
		fields=[
			"voucher_no",
			"voucher_detail_no",
			"from_voucher_type",
			"from_voucher_no",
			"from_voucher_detail_no",
			"item_code",
			"warehouse",
			"reserved_qty",
			"transferred_qty",
			"consumed_qty",
			"delivered_qty",
		],
		limit=0,
	)
	reserved_by_key = {}
	request_reserved_by_key = {}
	request_reservation_details = set()
	for row in reservations:
		remaining = max(
			flt(row.reserved_qty)
			- flt(row.transferred_qty)
			- flt(row.consumed_qty)
			- flt(row.delivered_qty),
			0,
		)
		key = (row.item_code, row.warehouse)
		reserved_by_key[key] = reserved_by_key.get(key, 0) + remaining
		if row.from_voucher_type == "Stock Entry" and row.from_voucher_no == stock_entry.name:
			request_reserved_by_key[key] = request_reserved_by_key.get(key, 0) + remaining
			request_reservation_details.add(row.from_voucher_detail_no)

	work_order_groups = _material_issue_groups(work_order, stock_facts)
	requested_by_key = {}
	request_details = set()
	for row in stock_entry.get("items") or []:
		if not row.s_warehouse or flt(row.transfer_qty) <= 0:
			continue
		request_details.add(row.name)
		key = (row.item_code, row.s_warehouse)
		if key not in work_order_groups:
			_throw("发料草稿含有不属于该工单的物料或仓库。")
		requested_by_key[key] = requested_by_key.get(key, 0) + flt(row.transfer_qty)
	if not requested_by_key:
		_throw("发料草稿没有可提交的物料明细。")
	for key, requested in requested_by_key.items():
		group = work_order_groups[key]
		remaining_component = max(group["required"] - group["net_transferred"], 0)
		if requested > remaining_component + 1e-6:
			_throw("发料草稿数量已超过工单当前净缺口，请删除草稿后重新申请。")
		total_reserved = reserved_by_key.get(key, 0)
		request_reserved = request_reserved_by_key.get(key, 0)
		unlinked_reserved = max(total_reserved - request_reserved, 0)
		expected_request_reserved = max(requested - unlinked_reserved, 0)
		if abs(request_reserved - expected_request_reserved) > 1e-6:
			_throw("发料草稿已被修改且与库存预留不一致，请删除草稿后重新申请。")
	expected_finished_qty = issueable_finished_qty(
		work_order, requested_by_key, stock_facts
	)
	requested_coverage_qty = flt(stock_entry.get("custom_process_coverage_qty"))
	if (
		requested_coverage_qty <= 0
		or abs(requested_coverage_qty - expected_finished_qty) > 1e-6
	):
		_throw("发料草稿的工单覆盖数量与物料明细不一致，请删除草稿后重新申请。")
	has_return_gap = _has_material_return_gap(work_order, stock_facts)
	if bool(stock_entry.get("is_additional_transfer_entry")) != has_return_gap:
		_throw("工单退料状态已经变化，请删除发料草稿后重新申请。")
	if has_return_gap:
		# Core sums fg_completed_qty from additional transfers and rejects it as
		# over-transfer.  Zero prevents that incompatible header update; row-level
		# net coverage above remains the source of truth.
		if abs(flt(stock_entry.get("fg_completed_qty"))) > 1e-9:
			_throw("退料补发草稿的原生工单数量必须为零，请删除草稿后重新申请。")
	elif (
		flt(stock_entry.get("fg_completed_qty")) <= 0
		or abs(flt(stock_entry.get("fg_completed_qty")) - requested_coverage_qty) > 1e-6
	):
		_throw("发料草稿的工单覆盖数量与申请数量不一致，请删除草稿后重新申请。")
	if not request_reservation_details.issubset(request_details):
		_throw("发料草稿与库存预留明细不一致，请删除草稿后重新申请。")


def validate_guided_stock_entry_identity(stock_entry):
	"""Freeze the request identity while allowing warehouse fulfilment details."""
	action = stock_entry.get("custom_process_workflow_action")
	if action not in {RECEIPT_ACTION, ISSUE_ACTION}:
		return
	expected_purpose = "Manufacture" if action == RECEIPT_ACTION else "Material Transfer for Manufacture"
	if stock_entry.get("purpose") != expected_purpose or stock_entry.get("is_return"):
		_throw("流程库存草稿的用途或退料标记不可修改。")
	if action == RECEIPT_ACTION and stock_entry.get("is_additional_transfer_entry"):
		_throw("完工入库申请不能标记为额外发料。")
	if action == ISSUE_ACTION and flt(stock_entry.get("custom_process_coverage_qty")) <= 0:
		_throw("生产发料申请缺少工单覆盖数量。")
	if stock_entry.is_new():
		if not stock_entry.get("work_order") or not stock_entry.get("custom_process_requested_by"):
			_throw("流程库存草稿缺少原申请人或生产工单。")
		return
	original = frappe.db.get_value(
		"Stock Entry",
		stock_entry.name,
		[
			"custom_process_workflow_action",
			"custom_process_requested_by",
			"work_order",
			"company",
			"purpose",
			"is_return",
			"is_additional_transfer_entry",
			"custom_process_coverage_qty",
			"fg_completed_qty",
			"process_loss_qty",
		],
		as_dict=True,
	)
	if not original:
		_throw("无法读取流程库存草稿的原始申请信息。")
	for fieldname in (
		"custom_process_workflow_action",
		"custom_process_requested_by",
		"work_order",
		"company",
		"purpose",
		"is_return",
		"is_additional_transfer_entry",
		"custom_process_coverage_qty",
		"fg_completed_qty",
		"process_loss_qty",
	):
		if stock_entry.get(fieldname) != original.get(fieldname):
			_throw("流程库存草稿的工单、公司、用途、申请类型和申请人不可修改。")


def mark_replenishment_receipt(stock_entry):
	"""Tag a supplement Manufacture entry before it is persisted as submitted."""
	if (
		stock_entry.get("purpose") != "Manufacture"
		or not stock_entry.get("work_order")
		or stock_entry.get("custom_process_workflow_action")
	):
		return
	target = frappe.db.get_value(
		"Work Order", stock_entry.work_order, "custom_replenishes_work_order"
	)
	if target:
		stock_entry.custom_process_workflow_action = REPLENISHMENT_RECEIPT_ACTION


def release_issue_request_reservations(stock_entry):
	if stock_entry.get("custom_process_workflow_action") != ISSUE_ACTION:
		return
	reservations = frappe.get_all(
		"Stock Reservation Entry",
		filters={
			"docstatus": 1,
			"from_voucher_type": "Stock Entry",
			"from_voucher_no": stock_entry.name,
		},
		pluck="name",
	)
	for name in reservations:
		reservation = frappe.get_doc("Stock Reservation Entry", name)
		# The caller has already been narrowed to a guided Stock Entry and its
		# company/Work Order.  Keep broad SRE cancel permission out of the role;
		# authorize only these exact request-owned locks internally.
		reservation.flags.ignore_permissions = True
		reservation.cancel()


@frappe.whitelist(methods=["POST"])
def withdraw_stock_request(stock_entry: str):
	"""Withdraw a guided draft and release its hard locks without broad delete rights."""
	require_reviewer(for_update=True)
	identity = frappe.db.get_value(
		"Stock Entry",
		stock_entry,
		["name", "work_order", "company", "docstatus", "custom_process_workflow_action"],
		as_dict=True,
	)
	if not identity or identity.custom_process_workflow_action not in {
		RECEIPT_ACTION,
		ISSUE_ACTION,
	}:
		_throw("只能撤回流程简化创建的入库或发料草稿。")
	if identity.work_order:
		frappe.db.get_value("Work Order", identity.work_order, "name", for_update=True)
	frappe.db.get_value("Stock Entry", stock_entry, "name", for_update=True)
	doc = frappe.get_doc("Stock Entry", stock_entry)
	if doc.docstatus != 0:
		_throw("库存单已提交或取消，不能再按草稿撤回。")
	if doc.work_order != identity.work_order:
		_throw("库存草稿的生产工单刚刚发生变化，请刷新后重试。")
	companies = reviewer_companies()
	if companies is not None and doc.company not in companies:
		frappe.throw(_("You are not permitted to manage this Stock Entry company."), frappe.PermissionError)
	if doc.work_order:
		work_order = frappe.get_doc("Work Order", doc.work_order)
		frappe.has_permission("Work Order", "read", doc=work_order, throw=True)
	request_snapshot = frappe._dict(
		name=doc.name,
		work_order=doc.work_order,
		company=doc.company,
		custom_process_workflow_action=doc.custom_process_workflow_action,
	)
	release_issue_request_reservations(doc)
	frappe.delete_doc("Stock Entry", doc.name, ignore_permissions=True)
	from process_simplification.notifications import notify_production_stock_withdrawn

	notify_production_stock_withdrawn(request_snapshot)
	return {"stock_entry": stock_entry, "withdrawn": True}


def _target_work_order_item(work_order: str, row_name: str):
	row = frappe.db.get_value(
		"Work Order Item",
		{"name": row_name, "parent": work_order},
		[
			"name",
			"parent",
			"item_code",
			"source_warehouse",
			"required_qty",
			"transferred_qty",
			"returned_qty",
			"stock_reserved_qty",
			"consumed_qty",
		],
		as_dict=True,
		for_update=True,
	)
	if not row:
		_throw("补产目标物料行不存在或已变化。")
	return row


def _target_work_order_item_group(work_order: str, seed_item):
	"""Lock and return every operation-split row for the target component."""
	names = frappe.get_all(
		"Work Order Item",
		filters={
			"parent": work_order,
			"item_code": seed_item.item_code,
			"source_warehouse": seed_item.source_warehouse,
			"include_item_in_manufacturing": 1,
		},
		pluck="name",
		order_by="name asc",
	)
	rows = []
	for name in names or [seed_item.name]:
		rows.append(_target_work_order_item(work_order, name))
	return rows


def _target_group_gap(target_items, stock_facts=None, *, work_order=None) -> tuple[float, float]:
	required = sum(flt(item.required_qty) for item in target_items)
	first = frappe._dict(target_items[0]) if target_items else frappe._dict()
	work_order_name = first.get("parent")
	skip_transfer = bool(work_order and work_order.get("skip_transfer"))
	warehouse = (work_order.get("wip_warehouse") if skip_transfer and work_order.get("from_wip_warehouse")
		else first.get("source_warehouse"))
	if stock_facts is None and work_order_name:
		stock_facts = load_work_order_stock_facts([work_order_name])
		facts_loaded = True
	else:
		facts_loaded = stock_facts is not None
	if facts_loaded:
		fact = get_work_order_stock_fact(
			stock_facts,
			work_order_name,
			first.get("item_code"),
			warehouse,
		)
		net_transferred = max(flt(fact.consumed_qty if skip_transfer else fact.net_issued_qty), 0)
	else:
		net_transferred = max(
			[
				max(flt(item.get("consumed_qty")) if skip_transfer else flt(item.transferred_qty) - flt(item.returned_qty), 0)
				for item in target_items
			]
			or [0]
		)
	reserved = sum(_active_work_order_item_reserved_qty(item, warehouse=warehouse) for item in target_items)
	return max(required - net_transferred - reserved, 0), required


def _active_replenishment(work_order: str, work_order_items):
	filters = {
		"custom_replenishes_work_order": work_order,
		"custom_replenishes_work_order_item": ["in", list(work_order_items)],
		"docstatus": ["<", 2],
		"status": ["not in", list(TERMINAL_WORK_ORDER_STATUSES)],
	}
	return frappe.db.get_value(
		"Work Order",
		filters,
		["name", "material_request", "production_plan", "qty", "produced_qty"],
		as_dict=True,
		order_by="creation desc",
	)


def _replenishment_bom(parent_work_order, item_code: str):
	bom = frappe.db.get_value(
		"Work Order",
		{
			"production_plan": parent_work_order.production_plan,
			"production_item": item_code,
			"docstatus": 1,
		},
		"bom_no",
		order_by="creation desc",
	)
	return bom or get_default_bom(item_code)


@frappe.whitelist(methods=["POST"])
@retry_request_transaction
def create_replenishment_work_order(
	work_order: str, work_order_item: str, qty: float | None = None
):
	frappe.db.savepoint("guided_replenishment_plan")
	try:
		return _create_replenishment_work_order(work_order, work_order_item, qty)
	except frappe.QueryDeadlockError:
		raise
	except Exception:
		frappe.db.rollback(save_point="guided_replenishment_plan")
		raise


def _create_replenishment_work_order(work_order: str, work_order_item: str, qty: float | None = None):
	"""Create a new, traceable supplement task; never reopen a completed task."""
	parent = _work_order_for_action(work_order)
	if parent.status in TERMINAL_WORK_ORDER_STATUSES:
		_throw("已结束工单不能再生成补产任务，请先对当前有效缺料工单重新检查。")
	item = _target_work_order_item(parent.name, work_order_item)
	target_items = _target_work_order_item_group(parent.name, item)
	existing = _active_replenishment(
		parent.name, [target_item.name for target_item in target_items]
	)
	if existing:
		return {
			"work_order": existing.name,
			"production_plan": existing.production_plan,
			"material_request": existing.material_request,
			"reused": True,
		}
	_require_stock_reservation_enabled()
	remaining, _group_required = _target_group_gap(target_items, work_order=parent)
	from process_simplification.production_workflow.replenishment import pending_internal_supply

	remaining = max(remaining - pending_internal_supply(parent, item), 0)
	requested_qty = flt(qty) if qty not in (None, "") else remaining
	if requested_qty <= 0 or requested_qty > remaining + 1e-9:
		_throw("补产数量必须大于零，且不能超过当前未覆盖缺口。")
	bom_no = _replenishment_bom(parent, item.item_code)
	if not bom_no:
		_throw("该半成品没有可用 BOM，无法生成补产任务。")

	mr = frappe.new_doc("Material Request")
	mr.material_request_type = "Manufacture"
	mr.company = parent.company
	mr.transaction_date = nowdate()
	mr.schedule_date = nowdate()
	mr.custom_replenishes_work_order = parent.name
	mr.custom_replenishes_work_order_item = item.name
	receipt_warehouse = (parent.wip_warehouse if parent.skip_transfer and parent.from_wip_warehouse
		else item.source_warehouse)
	mr.append(
		"items",
		{
			"item_code": item.item_code,
			"qty": requested_qty,
			"schedule_date": nowdate(),
			"warehouse": receipt_warehouse,
			"bom_no": bom_no,
		},
	)
	mr.set_missing_values()
	mr.flags.ignore_permissions = True
	mr.insert(ignore_permissions=True)
	mr.submit()

	from process_simplification.api.production_plan_adapter import (
		create_replenishment_work_orders_via_production_plan,
	)

	result = create_replenishment_work_orders_via_production_plan(
		material_request=mr.name,
		company=parent.company,
		source_warehouse=parent.source_warehouse or item.source_warehouse,
		sub_assembly_warehouse=item.source_warehouse,
		target_work_order=parent.name,
		target_work_order_item=item.name,
	)
	return {
		"material_request": mr.name,
		"production_plan": result.get("production_plan"),
		"work_order": result.get("replenishment_work_order"),
		"work_orders": result.get("work_orders") or [],
		"reused": False,
	}


def reserve_replenishment_output(stock_entry):
	"""Reserve as much supplement output as the active target still needs.

	The Manufacture receipt itself remains valid when the original target has
	already been completed or filled by another source. In that case the output
	stays ordinary warehouse stock and the returned result lets notifications say
	that no directed reservation was created.
	"""
	if stock_entry.get("purpose") != "Manufacture" or not stock_entry.get("work_order"):
		return None
	supply_work_order = frappe.db.get_value(
		"Work Order",
		stock_entry.work_order,
		[
			"name",
			"company",
			"production_item",
			"production_plan",
			"fg_warehouse",
			"custom_replenishes_work_order",
			"custom_replenishes_work_order_item",
		],
		as_dict=True,
	)
	if not supply_work_order:
		return None
	if not supply_work_order.custom_replenishes_work_order_item:
		from process_simplification.production_workflow.replenishment import resolve_internal_receipt_target

		supply_work_order = resolve_internal_receipt_target(supply_work_order)
		if not supply_work_order:
			return None
	target_name = supply_work_order.custom_replenishes_work_order
	# Keep one lock order everywhere: parent Work Order first, then its item row.
	target_state = frappe.db.get_value(
		"Work Order",
		target_name,
		["name", "docstatus", "status", "company"],
		as_dict=True,
		for_update=True,
	)
	received_qty = sum(
		flt(row.get("transfer_qty"))
		for row in stock_entry.get("items") or []
		if row.get("is_finished_item")
		and row.get("item_code") == supply_work_order.production_item
	)
	result = frappe._dict(
		target_work_order=target_name,
		received_qty=received_qty,
		reserved_qty=0.0,
		surplus_qty=received_qty,
		reason=None,
	)
	if not target_state or int(target_state.docstatus or 0) != 1:
		result.reason = "target_not_submitted"
		return result
	if target_state.status in TERMINAL_WORK_ORDER_STATUSES:
		result.reason = "target_terminal"
		return result
	if target_state.company != supply_work_order.company:
		_throw("补产工单与目标工单公司不一致，不能定向预留。")
	target_work_order = frappe.get_doc("Work Order", target_name)
	target_item = _target_work_order_item(
		target_work_order.name, supply_work_order.custom_replenishes_work_order_item
	)
	target_items = _target_work_order_item_group(target_work_order.name, target_item)
	reservation_warehouse = (target_work_order.wip_warehouse
		if target_work_order.skip_transfer and target_work_order.from_wip_warehouse else target_item.source_warehouse)
	finished_rows = [
		row
		for row in stock_entry.get("items") or []
		if row.get("is_finished_item")
		and row.get("item_code") == target_item.item_code
		and row.get("t_warehouse") == reservation_warehouse
	]
	if not finished_rows:
		_throw("补产入库物料或仓库与目标工单不一致，不能提交。")
	remaining, group_required = _target_group_gap(target_items, work_order=target_work_order)
	if remaining <= 0:
		result.reason = "target_gap_filled"
		return result
	# A Manufacture entry may split the same finished item over multiple rows
	# (for example, separate serial/batch bundles). Reserve across every matching
	# row so each directed lock retains its exact receipt-detail trace.
	matching_received_qty = sum(flt(row.get("transfer_qty")) for row in finished_rows)
	qty = min(matching_received_qty, remaining)
	if qty <= 0:
		result.reason = "no_receipt_qty"
		return result
	remaining_to_reserve = qty
	for finished_row in finished_rows:
		row_qty = min(flt(finished_row.get("transfer_qty")), remaining_to_reserve)
		if row_qty <= 0:
			continue
		sre = _new_work_order_reservation(
			work_order=target_work_order,
			work_order_item=target_item,
			qty=row_qty,
			voucher_qty=group_required,
			warehouse=reservation_warehouse,
			from_stock_entry=stock_entry.name,
			from_detail=finished_row.name,
		)
		reserved_qty = min(flt(sre.reserved_qty) if sre else 0, row_qty)
		result.reserved_qty += reserved_qty
		remaining_to_reserve = max(remaining_to_reserve - reserved_qty, 0)
		if reserved_qty + 1e-9 < row_qty:
			break
	result.surplus_qty = max(result.received_qty - result.reserved_qty, 0)
	if result.reserved_qty + 1e-9 < qty:
		_throw("补产入库未能完整预留给目标工单，请刷新后重试；本次入库未提交。")
	return result


def cancel_replenishment_output_reservations(stock_entry):
	if stock_entry.get("purpose") != "Manufacture":
		return
	reservations = frappe.get_all(
		"Stock Reservation Entry",
		filters={
			"docstatus": 1,
			"from_voucher_type": "Stock Entry",
			"from_voucher_no": stock_entry.name,
		},
		pluck="name",
	)
	for name in reservations:
		reservation = frappe.get_doc("Stock Reservation Entry", name)
		# Stock Entry cancellation has already passed its own permission check;
		# this is the exact derived reservation created from that receipt.
		reservation.flags.ignore_permissions = True
		reservation.cancel()
