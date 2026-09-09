from __future__ import annotations

import json

import frappe
from process_simplification.navigation_layout import arrange_workspace_card_links
from process_simplification.patches.v0_0.group_process_simplification_navigation import _group_sidebar
from process_simplification.stock_permissions import ensure_company_warehouse_reference_permissions

ITEMS = (
	("库房工作台", "warehouse-workbench", "Page", "stock"),
	("库存余额", "Stock Balance", "Report", "stock"),
	("库存流水", "Stock Ledger", "Report", "list"),
	("收发记录", "Stock Entry", "DocType", "stock"),
	("发货记录", "Delivery Note", "DocType", "truck"),
	("采购订单", "Purchase Order", "DocType", "shopping-cart"),
	("采购收货", "Purchase Receipt", "DocType", "package"),
)


def execute():
	ensure_company_warehouse_reference_permissions()
	for doctype, name, fieldname in (
		("Workspace Sidebar", "Process Simplification", "items"),
		("Workspace", "process-simplification", "links"),
	):
		if not frappe.db.exists(doctype, name):
			continue
		doc = frappe.get_doc(doctype, name)
		rows = doc.get(fieldname)
		if fieldname == "links" and not any(row.type == "Card Break" and row.label == "库房与库存" for row in rows):
			card = doc.append(fieldname, {"type": "Card Break", "label": "库房与库存"})
			rows.remove(card)
			index = next((i for i, row in enumerate(rows) if row.type == "Card Break" and row.label == "采购与工资"), len(rows))
			rows.insert(index, card)
		for label, route, link_type, icon in ITEMS:
			matches = [row for row in rows if row.link_to == route]
			if fieldname == "links" and matches and route in {"Stock Entry", "Delivery Note"}:
				continue
			row = matches[0] if matches else doc.append(fieldname, {})
			for duplicate in matches[1:]:
				rows.remove(duplicate)
			row.update(dict(type="Link", label=label, link_type=link_type, link_to=route))
			if fieldname == "items":
				row.update(dict(icon=icon, child=1, collapsible=1))
			else:
				row.onboard = 1
				row.is_query_report = int(link_type == "Report")
		if fieldname == "links":
			arrange_workspace_card_links(rows)
			content = json.loads(doc.content or "[]")
			if not any(block.get("data", {}).get("card_name") == "库房与库存" for block in content):
				index = next((i for i, block in enumerate(content) if block.get("data", {}).get("card_name") == "采购与工资"), len(content))
				content.insert(index, {"id": "ps-card-warehouse", "type": "card", "data": {"card_name": "库房与库存", "col": 4}})
				doc.content = json.dumps(content, ensure_ascii=False)
		doc.save(ignore_permissions=True)
	_group_sidebar()
	frappe.clear_cache()
