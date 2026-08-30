from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import add_days, add_months, flt, get_first_day, getdate, now_datetime, today

from process_simplification.management_access import require_owner_access

MAX_PERIOD_DAYS = 366

CATEGORY_META = {
	"finished_goods": {"label": "成品库存", "color": "#2563eb"},
	"semi_finished": {"label": "半成品库存", "color": "#7c3aed"},
	"work_in_progress": {"label": "在制库存", "color": "#f59e0b"},
	"raw_material": {"label": "原材料库存", "color": "#059669"},
	"other": {"label": "其他库存", "color": "#94a3b8"},
}

ITEM_GROUP_CATEGORIES = {
	"products": "finished_goods",
	"product": "finished_goods",
	"finished goods": "finished_goods",
	"成品": "finished_goods",
	"sub assemblies": "semi_finished",
	"sub assembly": "semi_finished",
	"semi finished goods": "semi_finished",
	"半成品": "semi_finished",
	"raw material": "raw_material",
	"raw materials": "raw_material",
	"原材料": "raw_material",
}


def normalize_period(from_date=None, to_date=None):
	period_to = getdate(to_date or today())
	period_from = getdate(from_date or get_first_day(period_to))
	if period_from > period_to:
		frappe.throw(_("From Date cannot be after To Date."))
	if (period_to - period_from).days + 1 > MAX_PERIOD_DAYS:
		frappe.throw(_("The dashboard date range cannot exceed one year."))
	return period_from, period_to


def previous_period(period_from, period_to):
	days = (period_to - period_from).days + 1
	previous_to = getdate(add_days(period_from, -1))
	return getdate(add_days(previous_to, 1 - days)), previous_to


def percentage_change(current, previous):
	current = flt(current)
	previous = flt(previous)
	if not previous:
		return None
	return flt(((current - previous) / abs(previous)) * 100, 1)


def classify_stock(item_group: str | None, warehouse: str | None, wip_warehouses=None):
	wip_warehouses = set(wip_warehouses or [])
	warehouse_name = (warehouse or "").strip().lower()
	if warehouse in wip_warehouses or any(
		token in warehouse_name for token in ("在制", "进行中", "work in progress", "wip")
	):
		return "work_in_progress"
	return ITEM_GROUP_CATEGORIES.get((item_group or "").strip().lower(), "other")


def owner_company_scope(user: str | None = None) -> set[str] | None:
	"""Return an owner's explicit top-level Company scope, or None when unrestricted."""
	user = user or frappe.session.user
	if user == "Administrator":
		return None
	rows = frappe.get_all(
		"User Permission",
		filters={"user": user, "allow": "Company"},
		fields=["for_value", "applicable_for"],
		limit=0,
	)
	explicit = {
		row.for_value
		for row in rows
		if row.for_value and not row.applicable_for
	}
	return explicit or None


def _companies():
	scope = owner_company_scope()
	filters = {"name": ["in", sorted(scope)]} if scope is not None else None
	return frappe.get_all(
		"Company",
		filters=filters,
		fields=["name", "default_currency"],
		order_by="name asc",
		limit=0,
	)


def _resolve_company(company: str | None, companies):
	company_names = {row.name for row in companies}
	if company:
		if company in company_names:
			return company
		frappe.throw(_("You are not permitted to view this company."), frappe.PermissionError)
	default_company = frappe.defaults.get_user_default("Company")
	if default_company in company_names:
		return default_company
	if companies:
		return companies[0].name
	frappe.throw(_("Create a Company before opening the executive dashboard."))


def _order_totals(company, period_from, period_to):
	row = frappe.db.sql(
		"""
		select count(*) as order_count,
		       coalesce(sum(coalesce(base_rounded_total, base_grand_total, 0)), 0) as order_amount
		from `tabSales Order`
		where company = %(company)s
		  and docstatus = 1
		  and transaction_date between %(from_date)s and %(to_date)s
		""",
		{"company": company, "from_date": period_from, "to_date": period_to},
		as_dict=True,
	)[0]
	return {"order_count": int(row.order_count or 0), "order_amount": flt(row.order_amount)}


def _order_trend(company, period_to):
	trend_from = getdate(get_first_day(add_months(period_to, -5)))
	rows = frappe.db.sql(
		"""
		select date_format(transaction_date, '%%Y-%%m') as month_key,
		       count(*) as order_count,
		       coalesce(sum(coalesce(base_rounded_total, base_grand_total, 0)), 0) as order_amount
		from `tabSales Order`
		where company = %(company)s
		  and docstatus = 1
		  and transaction_date between %(from_date)s and %(to_date)s
		group by date_format(transaction_date, '%%Y-%%m')
		order by month_key asc
		""",
		{"company": company, "from_date": trend_from, "to_date": period_to},
		as_dict=True,
	)
	by_month = {row.month_key: row for row in rows}
	result = []
	cursor = trend_from
	while cursor <= period_to:
		month_key = cursor.strftime("%Y-%m")
		row = by_month.get(month_key, frappe._dict())
		result.append(
			{
				"month": month_key,
				"order_count": int(row.get("order_count") or 0),
				"order_amount": flt(row.get("order_amount")),
			}
		)
		cursor = getdate(add_months(cursor, 1))
	return result


def _gross_profit(company, period_from, period_to):
	try:
		from erpnext.accounts.report.gross_profit.gross_profit import execute

		filters = frappe._dict(
			company=company,
			from_date=str(period_from),
			to_date=str(period_to),
			group_by="Invoice",
			include_returned_invoices=1,
		)
		_columns, data = execute(filters)
		total = data[-1] if data else frappe._dict()
		return {
			"available": True,
			"invoiced_net_sales": flt(total.get("selling_amount")),
			"gross_profit": flt(total.get("gross_profit")),
			"gross_margin_percent": flt(total.get("gross_profit_%")),
		}
	except Exception:
		frappe.logger("process_simplification").exception("Unable to calculate executive gross profit")
		return {
			"available": False,
			"invoiced_net_sales": 0,
			"gross_profit": 0,
			"gross_margin_percent": 0,
			"message": _("Gross profit is unavailable. Check invoice and stock valuation data."),
		}


def _wip_warehouses(company):
	warehouses = set(
		frappe.get_all(
			"Work Order",
			filters={
				"company": company,
				"docstatus": 1,
				"status": ["not in", ["Completed", "Stopped", "Closed", "Cancelled"]],
			},
			pluck="wip_warehouse",
		)
	)
	for row in frappe.get_all(
		"Warehouse",
		filters={"company": company, "is_group": 0},
		fields=["name", "warehouse_type"],
	):
		warehouse_type = (row.warehouse_type or "").strip().lower()
		if "work in progress" in warehouse_type or warehouse_type == "wip":
			warehouses.add(row.name)
	return {warehouse for warehouse in warehouses if warehouse}


def _inventory_summary(company):
	wip_warehouses = _wip_warehouses(company)
	rows = frappe.db.sql(
		"""
		select bin.warehouse, item.item_group, bin.item_code,
		       coalesce(sum(bin.stock_value), 0) as stock_value
		from `tabBin` bin
		inner join `tabWarehouse` warehouse on warehouse.name = bin.warehouse
		inner join `tabItem` item on item.name = bin.item_code
		where warehouse.company = %(company)s
		  and warehouse.is_group = 0
		  and (ifnull(bin.actual_qty, 0) != 0 or ifnull(bin.stock_value, 0) != 0)
		group by bin.warehouse, item.item_group, bin.item_code
		""",
		{"company": company},
		as_dict=True,
	)
	values = defaultdict(float)
	items = defaultdict(set)
	for row in rows:
		category = classify_stock(row.item_group, row.warehouse, wip_warehouses)
		values[category] += flt(row.stock_value)
		items[category].add(row.item_code)

	categories = []
	for key, meta in CATEGORY_META.items():
		categories.append(
			{
				"key": key,
				"label": meta["label"],
				"color": meta["color"],
				"stock_value": flt(values[key], 2),
				"item_count": len(items[key]),
			}
		)
	return {
		"total_stock_value": flt(sum(row["stock_value"] for row in categories), 2),
		"categories": categories,
	}


def _stock_ageing(company, period_to):
	try:
		from erpnext.stock.report.stock_ageing.stock_ageing import execute

		filters = frappe._dict(
			company=company,
			to_date=str(period_to),
			range="90",
			show_warehouse_wise_stock=0,
		)
		columns, data, _message, _chart = execute(filters)
		fieldnames = [column.get("fieldname") for column in columns]
		value_index = fieldnames.index("range2value")
		item_index = fieldnames.index("item_code")
		aged_rows = [row for row in data if flt(row[value_index]) > 0]
		return {
			"available": True,
			"item_count": len({row[item_index] for row in aged_rows}),
			"stock_value": flt(sum(flt(row[value_index]) for row in aged_rows), 2),
		}
	except Exception:
		frappe.logger("process_simplification").exception("Unable to calculate executive stock ageing")
		return {
			"available": False,
			"item_count": 0,
			"stock_value": 0,
			"message": _("Stock ageing is temporarily unavailable."),
		}


def _order_health(company, reference_date):
	row = frappe.db.sql(
		"""
		select count(*) as open_orders,
		       sum(case when delivery_date < %(today)s then 1 else 0 end) as overdue_orders,
		       sum(case when delivery_date between %(today)s and %(within_7_days)s then 1 else 0 end) as due_within_7_days,
		       coalesce(sum(
		          coalesce(base_rounded_total, base_grand_total, 0)
		          * greatest(100 - ifnull(per_delivered, 0), 0) / 100
		       ), 0) as pending_amount
		from `tabSales Order`
		where company = %(company)s
		  and docstatus = 1
		  and status not in ('Closed', 'Completed')
		  and ifnull(per_delivered, 0) < 100
		""",
		{
			"company": company,
			"today": reference_date,
			"within_7_days": getdate(add_days(reference_date, 7)),
		},
		as_dict=True,
	)[0]
	open_orders = int(row.open_orders or 0)
	overdue_orders = int(row.overdue_orders or 0)
	due_within_7_days = int(row.due_within_7_days or 0)
	return {
		"open_orders": open_orders,
		"overdue_orders": overdue_orders,
		"due_within_7_days": due_within_7_days,
		"other_open_orders": max(open_orders - overdue_orders - due_within_7_days, 0),
		"pending_amount": flt(row.pending_amount, 2),
	}


def _overdue_orders(company, reference_date, limit=5):
	return frappe.db.sql(
		"""
		select name, customer, customer_name, delivery_date, per_delivered,
		       coalesce(base_rounded_total, base_grand_total, 0) as order_amount,
		       coalesce(base_rounded_total, base_grand_total, 0)
		          * greatest(100 - ifnull(per_delivered, 0), 0) / 100 as pending_amount
		from `tabSales Order`
		where company = %(company)s
		  and docstatus = 1
		  and status not in ('Closed', 'Completed')
		  and ifnull(per_delivered, 0) < 100
		  and delivery_date < %(today)s
		order by delivery_date asc, creation asc
		limit %(limit)s
		""",
		{"company": company, "today": reference_date, "limit": int(limit)},
		as_dict=True,
	)


@frappe.whitelist()
def get_dashboard(company=None, from_date=None, to_date=None):
	require_owner_access()
	period_from, period_to = normalize_period(from_date, to_date)
	companies = _companies()
	company = _resolve_company(company, companies)
	company_row = next(row for row in companies if row.name == company)

	current_orders = _order_totals(company, period_from, period_to)
	previous_from, previous_to = previous_period(period_from, period_to)
	previous_orders = _order_totals(company, previous_from, previous_to)
	current_orders.update(
		{
			"previous_order_amount": previous_orders["order_amount"],
			"order_amount_change": percentage_change(
				current_orders["order_amount"], previous_orders["order_amount"]
			),
			"comparison_from_date": str(previous_from),
			"comparison_to_date": str(previous_to),
		}
	)

	return {
		"checked_at": now_datetime(),
		"company": company,
		"currency": company_row.default_currency,
		"companies": [dict(row) for row in companies],
		"period": {"from_date": str(period_from), "to_date": str(period_to)},
		"orders": current_orders,
		"order_trend": _order_trend(company, period_to),
		"gross_profit": _gross_profit(company, period_from, period_to),
		"inventory": _inventory_summary(company),
		"stock_ageing": _stock_ageing(company, getdate(today())),
		"order_health": _order_health(company, getdate(today())),
		"overdue_orders": _overdue_orders(company, getdate(today())),
	}
