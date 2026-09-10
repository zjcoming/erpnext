"""Physical stock commitments made atomically with guided plan confirmation."""

from collections import defaultdict

import frappe
from frappe.utils import flt

from process_simplification.production_workflow.stock_reservation import (
	locked_available_qty,
)


def net_subassemblies(plan):
	"""Reuse native BOM expansion, then net each physical pool exactly once.

	Full expansion is in BOM preorder. Scaling against the nearest non-phantom
	ancestor preserves native BOM quantities/UOMs without using projected stock.
	The returned row references survive plan.save and identify the exact parent
	WO to receive the stock commitment after the native WO engine has run.
	"""
	plan.skip_available_sub_assembly_item = 0
	plan.get_sub_assembly_items()
	plan.skip_available_sub_assembly_item = 1
	rows = plan.sub_assembly_items or []
	keys = {(row.production_item, row.fg_warehouse) for row in rows}
	pools = {key: locked_available_qty(*key) for key in sorted(keys)}
	precision = frappe.get_precision("Production Plan Sub Assembly Item", "qty")
	roots = {row.name: row for row in plan.po_items}
	stacks = defaultdict(list)
	commitments = []
	for row in rows:
		stack = stacks[row.production_plan_item]
		level = int(row.bom_level or 0)
		while stack and stack[-1][0] >= level:
			stack.pop()
		parent = stack[-1][1] if stack else roots[row.production_plan_item]
		gross_qty = flt(row.required_qty)
		parent_gross = stack[-1][2] if stack else flt(parent.planned_qty)
		parent_net = flt(parent.qty) if stack else flt(parent.planned_qty)
		required = flt(gross_qty * parent_net / parent_gross, precision) if parent_gross else 0
		key = (row.production_item, row.fg_warehouse)
		covered = min(required, pools[key])
		# Round the production remainder first: the reserved amount must match
		# exactly what was deducted from the persisted plan, even for fractions.
		row.required_qty = required
		row.qty = flt(max(required - covered, 0), precision)
		covered = max(required - row.qty, 0)
		pools[key] = max(pools[key] - covered, 0)
		row.bom_level = len(stack)
		row.parent_item_code = parent.production_item if stack else parent.item_code
		if covered > 1e-9:
			commitments.append((row, parent if stack else None, covered))
		stack.append((level, row, gross_qty))
	return commitments


def commit_plan_stock(plan, work_order_names, commitments, *, reserve_raw=False):
	"""Reserve each netted amount for its consuming WO or abort the whole plan.

	Native plan/WO auto-reservation is disabled during staging because it can
	partially reserve and transfer stock to a different branch. The enable flags
	are set only after exact reservations succeed, in the same transaction.
	"""
	from process_simplification.production_workflow.service import (
		_active_work_order_item_reserved_qty,
		_new_work_order_reservation,
	)

	orders = [frappe.get_doc("Work Order", name) for name in work_order_names]
	by_subrow = defaultdict(list)
	by_root = defaultdict(list)
	for order in orders:
		if order.production_plan_sub_assembly_item:
			by_subrow[order.production_plan_sub_assembly_item].append(order)
		else:
			by_root[order.production_plan_item].append(order)
	for row, parent, qty in commitments:
		candidates = by_subrow[parent.name] if parent else by_root[row.production_plan_item]
		if len(candidates) != 1:
			frappe.throw("抵扣排产的半成品无法唯一关联到领料工单，本次计划未创建。")
		order = candidates[0]
		items = [
			item
			for item in order.required_items
			if item.item_code == row.production_item
			and item.source_warehouse == row.fg_warehouse
			and item.include_item_in_manufacturing
		]
		if not items:
			frappe.throw("抵扣排产的半成品与领料工单物料或仓库不一致，本次计划未创建。")
		required = sum(flt(item.required_qty) for item in items)
		reserved = sum(_active_work_order_item_reserved_qty(item) for item in items)
		if reserved + qty > required + 1e-9:
			frappe.throw("半成品库存被重复抵扣，本次计划未创建。")
		sre = _new_work_order_reservation(
			work_order=order,
			work_order_item=items[0],
			qty=qty,
			voucher_qty=required,
			from_production_plan=plan.name,
			from_detail=row.name,
		)
		if not sre or flt(sre.reserved_qty) + 1e-9 < qty:
			frappe.throw("库存发生变化，抵扣排产的半成品未能完整预留；本次计划已回滚，请刷新后重试。")
	if reserve_raw:
		# Supplemental tasks retain their existing early raw-material commitment.
		# Reserve only inputs of the WOs that were actually created after netting.
		keys = {
			(item.item_code, item.source_warehouse)
			for order in orders
			for item in order.required_items
			if item.include_item_in_manufacturing
		}
		for key in sorted(keys):
			locked_available_qty(*key)
		for order in orders:
			groups = defaultdict(list)
			for item in order.required_items:
				if item.include_item_in_manufacturing:
					groups[(item.item_code, item.source_warehouse)].append(item)
			for items in groups.values():
				required = sum(flt(item.required_qty) for item in items)
				reserved = sum(_active_work_order_item_reserved_qty(item) for item in items)
				qty = min(
					max(required - reserved, 0),
					locked_available_qty(items[0].item_code, items[0].source_warehouse),
				)
				if qty > 1e-9:
					sre = _new_work_order_reservation(
						work_order=order, work_order_item=items[0], qty=qty, voucher_qty=required
					)
					if not sre or flt(sre.reserved_qty) + 1e-9 < qty:
						frappe.throw("补产物料预留失败，本次计划已回滚，请刷新后重试。")
	for order in orders:
		frappe.db.set_value("Work Order", order.name, "reserve_stock", 1, update_modified=False)
	frappe.db.set_value("Production Plan", plan.name, "reserve_stock", 1, update_modified=False)
	plan.reserve_stock = 1
