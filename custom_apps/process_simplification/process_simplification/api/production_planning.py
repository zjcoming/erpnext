"""The same production pool, with planning and execution shown separately."""

from __future__ import annotations

import frappe
from math import ceil
from frappe.utils import cint


@frappe.whitelist()
def get_planning_pool(page=1, page_length=20, filters=None):
	from process_simplification.production_reporting.dispatch_pool import get_operation_dispatch_pool
	from process_simplification.production_reporting.prearrangement import get_plan_context

	parsed = frappe.parse_json(filters) if isinstance(filters, str) else filters
	result = (_saved_pool(page, page_length, parsed) if isinstance(parsed, dict) and cint(parsed.get("saved_only"))
		else get_operation_dispatch_pool(page, page_length, filters))
	if result["rows"]:
		context = get_plan_context([row.job_card for row in result["rows"]])
		for row in result["rows"]:
			row["planning"] = context["cards"].get(row.job_card, {})
	return result


def _saved_pool(page, page_length, filters):
	"""Keep stopped or invalid pending plans reachable for cancellation."""
	from process_simplification.production_reporting import dispatch_pool

	companies = dispatch_pool._require_access()
	filters = dispatch_pool._normalize_filters(filters)
	if filters["company"] and companies is not None and filters["company"] not in companies:
		frappe.throw("无权查看该公司的生产任务。", frappe.PermissionError)
	conditions = ["p.status = 'Planned'", "p.company = jc.company", "wo.company = jc.company"]
	values = {}
	if companies is not None:
		conditions.append("p.company in %(companies)s" if companies else "1 = 0")
		values["companies"] = tuple(sorted(companies))
	for field in ("company", "operation"):
		if filters[field]:
			conditions.append(f"jc.{field} = %({field})s")
			values[field] = filters[field]
	if filters["search"]:
		conditions.append("(jc.name like %(search)s or wo.name like %(search)s or wo.sales_order like %(search)s or jc.production_item like %(search)s or item.item_name like %(search)s)")
		values["search"] = "%" + filters["search"] + "%"
	scope = """from `tabJob Card Prearrangement` p
		inner join `tabJob Card` jc on jc.name = p.job_card
		inner join `tabWork Order` wo on wo.name = jc.work_order
		left join `tabItem` item on item.name = jc.production_item
		where """ + " and ".join(conditions)
	page, page_length = max(cint(page), 1), min(max(cint(page_length), 1), 20)
	total = cint(frappe.db.sql("select count(*) " + scope, values)[0][0])
	values.update(offset=(page - 1) * page_length, page_length=page_length)
	rows = frappe.db.sql(f"""select jc.name as job_card, jc.work_order, jc.company,
		jc.production_item, item.item_name, item.stock_uom, jc.operation, jc.operation_id,
		jc.workstation, jc.sequence_id, jc.for_quantity, jc.total_completed_qty as completed_qty,
		wo.production_plan, wo.sales_order, wo.expected_delivery_date as delivery_date,
		{dispatch_pool._ASSIGNMENT_EXISTS} as has_assignment_history
		{scope} order by jc.operation, wo.name, jc.sequence_id, jc.name
		limit %(page_length)s offset %(offset)s""", values, as_dict=True)
	company_filters = {"name": ["in", sorted(companies)]} if companies is not None else {}
	return {"rows": rows, "filters": filters,
		"companies": frappe.get_list("Company", filters=company_filters, pluck="name", order_by="name", limit_page_length=0),
		"pagination": {"page": page, "page_length": page_length, "total_count": total,
			"total_pages": ceil(total / page_length), "has_more": page * page_length < total}}
