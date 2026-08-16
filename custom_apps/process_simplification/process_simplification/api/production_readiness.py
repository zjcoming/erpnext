from __future__ import annotations

from collections import defaultdict

import frappe


def plan_priority_key(plan) -> tuple:
	plan = frappe._dict(plan or {})
	return (
		str(plan.get("planned_date") or plan.get("posting_date") or "9999-12-31"),
		str(plan.get("creation") or ""),
		str(plan.get("name") or ""),
	)


def build_work_order_graph(
	plan,
	work_orders,
	required_items,
	sub_assemblies,
	active_bom_items=None,
):
	plan = frappe._dict(plan or {})
	active_bom_items = set(active_bom_items or [])
	required_by_work_order = defaultdict(list)
	for item in required_items or []:
		required_by_work_order[item.get("parent")].append(frappe._dict(item))

	sub_assembly_by_name = {row.get("name"): frappe._dict(row) for row in sub_assemblies or []}
	work_orders_by_name = {}
	work_orders_by_item = defaultdict(list)
	for source in work_orders or []:
		row = frappe._dict(dict(source))
		sub_assembly = sub_assembly_by_name.get(row.get("production_plan_sub_assembly_item"))
		row.bom_level = int(sub_assembly.get("bom_level") or 0) + 1 if sub_assembly else 0
		row.priority_date = str(
			(sub_assembly or {}).get("schedule_date")
			or row.get("planned_start_date")
			or plan.get("planned_date")
			or plan.get("posting_date")
			or ""
		)
		row.parent_item_code = (sub_assembly or {}).get("parent_item_code")
		row.parent_work_order = None
		row.child_work_orders = []
		row.required_items = required_by_work_order.get(row.name, [])
		row.is_finished_good = not bool(sub_assembly)
		for item in row.required_items:
			item.is_manufactured = item.get("item_code") in active_bom_items
		work_orders_by_name[row.name] = row
		work_orders_by_item[row.get("production_item")].append(row)

	for rows in work_orders_by_item.values():
		rows.sort(key=lambda row: (str(row.get("creation") or ""), row.name))

	for row in work_orders_by_name.values():
		if not row.parent_item_code:
			continue
		parents = work_orders_by_item.get(row.parent_item_code) or []
		if not parents:
			continue
		parent = parents[0]
		row.parent_work_order = parent.name
		parent.child_work_orders.append(row.name)

	for row in work_orders_by_name.values():
		row.child_work_orders.sort(
			key=lambda name: (
				-work_orders_by_name[name].bom_level,
				str(work_orders_by_name[name].get("creation") or ""),
				name,
			)
		)

	execution_order = sorted(
		work_orders_by_name,
		key=lambda name: (
			-work_orders_by_name[name].bom_level,
			str(work_orders_by_name[name].get("creation") or ""),
			name,
		),
	)
	return frappe._dict(
		{
			"name": plan.get("name"),
			"planned_date": str(plan.get("planned_date") or plan.get("posting_date") or ""),
			"priority_key": plan_priority_key(plan),
			"work_orders_by_name": work_orders_by_name,
			"execution_order": execution_order,
		}
	)
