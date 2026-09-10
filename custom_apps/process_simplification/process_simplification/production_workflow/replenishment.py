"""Resolve supplement execution links without changing native sales demand links."""

from __future__ import annotations

import frappe


def target_item_for_supply(supply, target):
	"""Only a matching item in the exact receiving/source warehouse can be supplied."""
	if not target or supply.get("company") != target.get("company"):
		return None
	items = [
		item
		for item in target.get("required_items") or []
		if item.get("item_code") == supply.get("production_item")
		and (
			target.get("wip_warehouse")
			if target.get("skip_transfer") and target.get("from_wip_warehouse")
			else item.get("source_warehouse")
		)
		== supply.get("fg_warehouse")
		and item.get("include_item_in_manufacturing", 1)
	]
	explicit = supply.get("custom_replenishes_work_order_item")
	if explicit:
		return next((item for item in items if item.get("name") == explicit), None)
	return (
		min(items, key=lambda item: (int(item.get("idx") or 0), str(item.get("name") or "")))
		if items
		else None
	)


def attach_replenishment_context(graphs):
	"""Attach presentation/priority context to uniquely linked supplement branches.

	The graph contains only permission-filtered Work Orders. Missing targets and
	cycles stay ungrouped rather than leaking or guessing another order's context.
	Native sales_order/sales_order_item fields are deliberately left untouched.
	"""
	by_name = {name: work_order for graph in graphs for name, work_order in graph.work_orders_by_name.items()}

	def branch_root(row):
		seen = set()
		while row and row.get("name") not in seen:
			seen.add(row.get("name"))
			if row.get("custom_replenishes_work_order"):
				return row
			parent = by_name.get(row.get("parent_work_order"))
			if not parent or parent.get("production_plan") != row.get("production_plan"):
				return None
			row = parent
		return None

	def order_context(row, seen):
		if not row or row.get("name") in seen:
			return None
		seen = seen | {row.get("name")}
		root = branch_root(row)
		if root:
			target = by_name.get(root.get("custom_replenishes_work_order"))
			if not target_item_for_supply(root, target):
				return None
			return order_context(target, seen | ({root.name} if root.name != row.name else set()))
		return row if row.get("sales_order_item") and row.get("sales_order") else None

	for row in by_name.values():
		root = branch_root(row)
		if not root:
			continue
		row.is_replenishment = True
		row.replenishment_root = root.name
		row.replenishment_target = root.get("custom_replenishes_work_order")
		target = by_name.get(
			row.get("custom_replenishes_work_order")
			if row.name == root.name
			else row.get("parent_work_order")
		)
		item = target_item_for_supply(row, target)
		if item:
			row.supply_target_work_order = target.name
			row.supply_target_work_order_item = item.get("name")
		context = order_context(row, set())
		if not context or not item:
			row.replenishment_link_error = True
			continue
		row.context_sales_order = context.get("sales_order")
		row.context_sales_order_item = context.get("sales_order_item")
		for field in ("order_delivery_date", "order_creation", "sales_order_item_idx"):
			row[field] = context.get(field)
	return by_name


def load_plan_supply_graph(plan):
	work_orders = frappe.get_all(
		"Work Order",
		filters={"production_plan": plan, "docstatus": ["in", [1, 2]]},
		fields=[
			"name",
			"company",
			"production_plan",
			"production_item",
			"fg_warehouse",
			"production_plan_item",
			"production_plan_sub_assembly_item",
			"creation",
			"skip_transfer",
			"from_wip_warehouse",
			"wip_warehouse",
			"qty",
			"produced_qty",
			"process_loss_qty",
			"status",
			"bom_no",
			"custom_replenishes_work_order",
			"custom_replenishes_work_order_item",
		],
		limit=0,
	)
	if not work_orders:
		return None
	items = frappe.get_all(
		"Work Order Item",
		filters={"parent": ["in", [row.name for row in work_orders]]},
		fields=["name", "parent", "idx", "item_code", "source_warehouse", "include_item_in_manufacturing"],
		limit=0,
	)
	sub_assemblies = frappe.get_all(
		"Production Plan Sub Assembly Item",
		filters={"parent": plan},
		fields=["name", "idx", "production_plan_item", "parent_item_code", "production_item", "bom_level"],
		limit=0,
	)
	from process_simplification.api.production_readiness import build_work_order_graph

	return build_work_order_graph(frappe._dict(name=plan), work_orders, items, sub_assemblies)


def resolve_internal_receipt_target(supply):
	"""Resolve the immediate consuming WO for normal and supplemental receipts."""
	plan = supply.get("production_plan")
	if not plan:
		return None
	graph = load_plan_supply_graph(plan)
	by_name = graph.work_orders_by_name if graph else {}
	row = by_name.get(supply.name)
	if row and row.get("graph_link_ambiguous"):
		frappe.throw("入库的上级工单关联不唯一，请核对生产计划后再入库。")
	if not row or not row.get("production_plan_sub_assembly_item"):
		return None
	target = by_name.get(row.get("parent_work_order"))
	item = target_item_for_supply(row, target)
	if not item:
		frappe.throw("入库物料、仓库或上级工单关联不一致，请核对后再入库。")
	return frappe._dict(
		{
			**dict(supply),
			"custom_replenishes_work_order": target.name,
			"custom_replenishes_work_order_item": item.name,
		}
	)


def pending_internal_supply(work_order, item):
	"""Pending output is a linked task, never physical stock or a second shortage."""
	if not work_order.get("production_plan"):
		return 0.0
	from frappe.utils import flt

	from process_simplification.api.production_readiness import TERMINAL_WORK_ORDER_STATUSES

	graph = load_plan_supply_graph(work_order.production_plan)
	if not graph:
		return 0.0
	warehouse = (
		work_order.get("wip_warehouse")
		if work_order.get("skip_transfer") and work_order.get("from_wip_warehouse")
		else item.source_warehouse
	)
	return sum(
		max(flt(row.qty) - flt(row.produced_qty) - flt(row.process_loss_qty), 0)
		for row in graph.work_orders_by_name.values()
		if row.parent_work_order == work_order.name
		and row.production_item == item.item_code
		and row.fg_warehouse == warehouse
		and row.status not in TERMINAL_WORK_ORDER_STATUSES
	)
