from __future__ import annotations


WORKSPACE_CARD_LINKS = (
	("经营总览", ("executive-dashboard",)),
	("销售与订单", ("quick-sales-order", "order-workbench")),
	(
		"生产执行",
		(
			"production-workbench",
			"active-production-work",
			"my-production-reporting",
			"production-report-history",
			"production-report-review",
			"production-exception-review",
		),
	),
	(
		"采购与工资",
		(
			"shortage-purchase-planning",
			"Operation Wage Rate",
			"Monthly Worker Wage Summary",
		),
	),
	(
		"系统管理",
		("Process Simplification Settings", "process-access-management"),
	),
	(
		"标准单据",
		(
			"Sales Order",
			"Stock Reservation Entry",
			"Work Order",
			"Material Request",
			"Delivery Note",
			"Stock Entry",
		),
	),
)


def arrange_workspace_card_links(rows):
	"""Keep idempotent upgrade repairs aligned with the task-based workspace."""
	available_cards = {
		row.label
		for row in rows
		if row.type == "Card Break"
	}
	if not available_cards.intersection(label for label, _routes in WORKSPACE_CARD_LINKS):
		_recalculate_link_counts(rows)
		return

	managed_routes = {
		route
		for _label, routes in WORKSPACE_CARD_LINKS
		for route in routes
	}
	managed_rows = {}
	for row in list(rows):
		if row.type != "Link" or row.link_to not in managed_routes:
			continue
		if row.link_to not in managed_rows:
			managed_rows[row.link_to] = row
		rows.remove(row)

	for label, routes in WORKSPACE_CARD_LINKS:
		card = next(
			(row for row in rows if row.type == "Card Break" and row.label == label),
			None,
		)
		if card is None:
			continue
		insert_at = rows.index(card) + 1
		for route in routes:
			row = managed_rows.get(route)
			if row is None:
				continue
			rows.insert(insert_at, row)
			insert_at += 1

	_recalculate_link_counts(rows)
	for index, row in enumerate(rows, start=1):
		row.idx = index


def _recalculate_link_counts(rows):
	card = None
	count = 0
	for row in rows:
		if row.type == "Card Break":
			if card:
				card.link_count = count
			card = row
			count = 0
		elif card and row.type == "Link":
			count += 1
	if card:
		card.link_count = count
