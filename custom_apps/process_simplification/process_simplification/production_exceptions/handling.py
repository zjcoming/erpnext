"""Company-scoped closeout and downstream disposition, with one review per request."""

import json
from collections import defaultdict
import frappe
from frappe.utils import flt, now_datetime
from process_simplification.management_access import user_company_scope
from process_simplification.production_reporting.domain import user_roles
from process_simplification.production_reporting.constants import ADMIN_REVIEW_ROLES
from process_simplification.production_exceptions.material_routes import load_routes, stock_rows, EPSILON

TERMINAL = {"Completed", "Closed", "Stopped"}
OPEN = {"Pending Approval", "Awaiting Stock Entry"}
LABELS = {
	"Closeout Return": "收尾退余料",
	"Closeout Quarantine": "收尾待检",
	"Closeout Scrap": "收尾报废",
	"Release": "检验放行",
	"Scrap": "确认报废",
	"Rework": "转入返工",
	"Supplier Return": "退供应商",
	"Dispose": "报废处置",
}
STOCK_ROLES = {"Process Simplification Warehouse Operator", "Stock User", "Stock Manager", "System Manager"}


def authorize(company=None, review=False):
	roles = user_roles()
	if not roles.intersection(ADMIN_REVIEW_ROLES if review else ADMIN_REVIEW_ROLES | STOCK_ROLES):
		frappe.throw("当前账号不能处理物料收尾或处置。", frappe.PermissionError)
	companies = user_company_scope()
	if company and companies is not None and company not in companies:
		frappe.throw("不能处理其他公司的物料。", frappe.PermissionError)


def save(doc):
	doc.flags.material_handling_action = True
	doc.save(ignore_permissions=True)
	return doc


def validate_document(doc):
	if not doc.flags.material_handling_action:
		frappe.throw("请通过物料异常页面操作，不允许直接修改处理单。")
	if doc.action not in LABELS or doc.status not in OPEN | {"Completed", "Withdrawn", "Rejected"}:
		frappe.throw("无效的物料处理方式或状态。")
	if doc.action == "Dispose" and not str(doc.reason or "").strip():
		frappe.throw("请填写报废处置依据。")
	if not doc.company or not doc.request_key or not doc.items:
		frappe.throw("缺少公司、请求编号或物料明细。")
	for row in doc.items:
		if not row.item_code or flt(row.qty) <= 0 or not row.source_warehouse:
			frappe.throw("物料、来源仓和正数数量不能为空。")
		if (
			frappe.get_cached_value("UOM", row.stock_uom, "must_be_whole_number")
			and abs(row.qty - round(row.qty)) > EPSILON
		):
			frappe.throw("单位为 {0} 的物料数量必须为整数。".format(row.stock_uom))
	if not doc.is_new():
		old = frappe.get_doc(doc.doctype, doc.name)
		keys = (
			"action",
			"company",
			"work_order",
			"source_stock_entry",
			"reason",
			"request_key",
			"purchase_receipt",
			"rework_bom",
			"requested_by",
			"requested_at",
		)
		if any(doc.get(k) != old.get(k) for k in keys):
			frappe.throw("物料处理事实不能修改；请撤回后重新申请。")
		item_fields = (
			"material_key",
			"item_code",
			"original_item",
			"stock_uom",
			"qty",
			"source_warehouse",
			"target_warehouse",
			"original_source_warehouse",
			"source_detail",
			"lots_json",
			"purchase_receipt_item",
		)
		if [[r.get(k) for k in item_fields] for r in doc.items] != [
			[r.get(k) for k in item_fields] for r in old.items
		]:
			frappe.throw("物料处理明细不能修改；请撤回后重新申请。")


def lock_scope(work_order=None, source=None):
	# Stock submit and all creation/withdrawal paths share these parent locks.
	if work_order:
		frappe.db.get_value("Work Order", work_order, "name", for_update=True)
	if source:
		frappe.db.get_value("Stock Entry", source, "name", for_update=True)


def held_quantities(work_order=None, source=None, exclude=None):
	parent = frappe.qb.DocType("Material Handling Request")
	child = frappe.qb.DocType("Material Handling Item")
	q = (
		frappe.qb.from_(parent)
		.join(child)
		.on(child.parent == parent.name)
		.select(child.material_key, child.qty)
		.where(parent.status.isin(sorted(OPEN)))
	)
	if work_order:
		q = q.where(parent.work_order == work_order)
	if source:
		q = q.where(parent.source_stock_entry == source)
	if exclude:
		q = q.where(parent.name != exclude)
	totals = defaultdict(float)
	for r in q.for_update().run(as_dict=True):
		totals[r.material_key] += flt(r.qty)
	return totals


def closeout_options(work_order, exclude=None, for_update=False):
	wo = frappe.get_doc("Work Order", work_order)
	authorize(wo.company)
	if wo.docstatus != 1 or wo.status not in TERMINAL:
		frappe.throw("收尾入口用于已完工、已关闭或已停工工单；在产任务请从工人的退料入口申请。")
	from process_simplification.production_exceptions.service import (
		material_warehouse_settings,
		_open_material_qty,
	)

	held = held_quantities(work_order=work_order, exclude=exclude)
	settings = material_warehouse_settings(wo)
	rows = load_routes(work_order, for_update=for_update)
	for r in rows:
		r.item_name = frappe.get_cached_value("Item", r.item_code, "item_name")
		r.requestable_qty = max(
			r.native_available_qty
			- held[r.key]
			- _open_material_qty(work_order, r.item_code, r.source_warehouse, material_key=r.key),
			0,
		)
	return dict(
		work_order=wo.name,
		company=wo.company,
		status=wo.status,
		materials=[r for r in rows if r.requestable_qty > EPSILON],
		warehouse_settings=settings,
	)


def disposition_options(source_stock_entry, exclude=None):
	entry = frappe.get_doc("Stock Entry", source_stock_entry, for_update=True)
	authorize(entry.company)
	if entry.docstatus != 1:
		frappe.throw("来源退料单尚未过账或已取消。")
	per = entry.get("custom_production_exception_request")
	mhr = entry.get("custom_material_handling_request")
	if not per and not mhr:
		frappe.throw("请选择物料异常流程产生的退料或处置单。")
	if per:
		origin = frappe.get_doc("Production Exception Request", per)
		original_sources = {r.item_code: entry.custom_return_source_warehouse for r in entry.items}
		quarantine = (
			origin.request_type == "Material Return"
			and origin.cause == "Material Defect"
			and bool(origin.material_action)
		)
		scrap = origin.request_type == "Material Scrap"
	else:
		origin = frappe.get_doc("Material Handling Request", mhr)
		original_sources = {r.item_code: r.original_source_warehouse for r in origin.items}
		quarantine = origin.action == "Closeout Quarantine"
		scrap = origin.action in {"Closeout Scrap", "Scrap"}
	if not quarantine and not scrap:
		frappe.throw("该单据没有待检或待处置物料。")
	# Downstream submitted movements are deducted by the immutable source detail.
	used = defaultdict(float)
	parent = frappe.qb.DocType("Material Handling Request")
	child = frappe.qb.DocType("Material Handling Item")
	q = (
		frappe.qb.from_(parent)
		.join(child)
		.on(child.parent == parent.name)
		.select(child.material_key, child.qty)
		.where(
			(parent.source_stock_entry == entry.name)
			& parent.status.isin(["Pending Approval", "Awaiting Stock Entry", "Completed"])
		)
	)
	if exclude:
		q = q.where(parent.name != exclude)
	for r in q.for_update().run(as_dict=True):
		used[r.material_key] += flt(r.qty)
	from process_simplification.production_exceptions.material_routes import _fragments

	bundles = defaultdict(list)
	names = [r.serial_and_batch_bundle for r in entry.items if r.serial_and_batch_bundle]
	if names:
		for r in frappe.get_all(
			"Serial and Batch Entry",
			filters={"parent": ["in", names]},
			fields=["parent", "batch_no", "serial_no", "qty"],
			limit=0,
		):
			bundles[r.parent].append(r)
	deducted = defaultdict(float)
	dependents = frappe.get_all(
		"Material Handling Request",
		filters={"source_stock_entry": entry.name, "status": ["in", list(OPEN | {"Completed"})]},
		fields=["name", "stock_entry", "purchase_return"],
		limit=0,
	)
	for child_doc in dependents:
		if child_doc.name == exclude:
			continue
		linked = (
			frappe.get_doc("Purchase Receipt", child_doc.purchase_return)
			if child_doc.purchase_return
			else frappe.get_doc("Stock Entry", child_doc.stock_entry)
			if child_doc.stock_entry
			else None
		)
		if not linked:
			continue
		child_bundles = defaultdict(list)
		bundle_ids = [x.serial_and_batch_bundle for x in linked.items if x.serial_and_batch_bundle]
		if bundle_ids:
			for x in frappe.get_all(
				"Serial and Batch Entry",
				filters={"parent": ["in", bundle_ids]},
				fields=["parent", "batch_no", "serial_no", "qty"],
				limit=0,
			):
				child_bundles[x.parent].append(x)
		for x in linked.items:
			adapted = frappe._dict(x.as_dict())
			if linked.doctype == "Purchase Receipt":
				adapted.transfer_qty = abs(flt(x.stock_qty))
			for batch, serial, amount in _fragments(adapted, child_bundles):
				deducted[(x.get("custom_material_source_detail"), batch or "", serial or "")] += amount
	rows = []
	for r in entry.items:
		if not r.t_warehouse:
			continue
		qty = max(flt(r.transfer_qty) - used[r.name], 0)
		if qty <= EPSILON:
			continue
		lots = []
		for b, s, q in _fragments(r, bundles):
			lot_key = (r.name, b or "", s or "")
			taken = min(q, deducted[lot_key])
			deducted[lot_key] -= taken
			available = q - taken
			if available > EPSILON:
				lots.append(frappe._dict(batch_no=b, serial_no=s, qty=available))
		rows.append(
			frappe._dict(
				key=r.name,
				item_code=r.item_code,
				item_name=r.item_name,
				stock_uom=r.stock_uom,
				original_item=r.original_item or r.item_code,
				source_warehouse=r.t_warehouse,
				return_warehouse=r.get("custom_return_source_warehouse") or original_sources.get(r.item_code),
				requestable_qty=qty,
				lots=lots,
				source_detail=r.name,
			)
		)
	return dict(
		company=entry.company,
		source_stock_entry=entry.name,
		work_order=origin.work_order,
		materials=rows,
		actions=["Release", "Scrap", "Rework", "Supplier Return"] if quarantine else ["Dispose"],
		action_labels=LABELS,
	)


def valid_warehouse(name, company, source=None):
	w = (
		frappe.db.get_value("Warehouse", name, ["company", "disabled", "is_group"], as_dict=True)
		if name
		else None
	)
	if not w or w.company != company or w.disabled or w.is_group or name == source:
		frappe.throw("请先配置同公司、启用的独立目标仓库。")
	return name


def create_request(
	action,
	items,
	request_key,
	work_order=None,
	source_stock_entry=None,
	reason=None,
	purchase_receipt=None,
	rework_bom=None,
):
	authorize()
	if action == "Dispose" and not str(reason or "").strip():
		frappe.throw("请填写报废处置依据。")
	if action not in LABELS or not request_key:
		frappe.throw("处理方式或请求编号无效。")
	items = frappe.parse_json(items) if isinstance(items, str) else items
	if not isinstance(items, list) or not items or len(items) > 100:
		frappe.throw("请选择 1 至 100 行物料。")
	if source_stock_entry:
		source = frappe.db.get_value(
			"Stock Entry",
			source_stock_entry,
			["company", "custom_production_exception_request", "custom_material_handling_request"],
			as_dict=True,
		)
		if not source:
			frappe.throw("来源库存单不存在。")
		authorize(source.company)
		source_wo = (
			frappe.db.get_value(
				"Production Exception Request", source.custom_production_exception_request, "work_order"
			)
			if source.custom_production_exception_request
			else frappe.db.get_value(
				"Material Handling Request", source.custom_material_handling_request, "work_order"
			)
			if source.custom_material_handling_request
			else None
		)
		if work_order and work_order != source_wo:
			frappe.throw("来源退料单与生产工单不一致。")
		work_order = source_wo
	lock_scope(work_order, source_stock_entry)
	if purchase_receipt:
		frappe.db.get_value("Purchase Receipt", purchase_receipt, "name", for_update=True)
	existing = frappe.db.get_value("Material Handling Request", {"request_key": request_key}, "name")
	if existing:
		doc = frappe.get_doc("Material Handling Request", existing)
		authorize(doc.company)
		fingerprint = sorted(
			(str(r.get("material_key")), flt(r.get("qty")), r.get("purchase_receipt_item") or "")
			for r in items
		)
		if (
			doc.requested_by != frappe.session.user
			or doc.action != action
			or doc.work_order != (work_order or None)
			or doc.source_stock_entry != (source_stock_entry or None)
			or (doc.purchase_receipt or None) != (purchase_receipt or None)
			or (doc.rework_bom or None) != (rework_bom or None)
			or doc.reason != str(reason or LABELS[action]).strip()[:1000]
			or fingerprint
			!= sorted((r.material_key, flt(r.qty), r.purchase_receipt_item or "") for r in doc.items)
		):
			frappe.throw("该请求编号已用于其他内容，请刷新后重试。")
		return doc
	options = (
		closeout_options(work_order, for_update=True)
		if action.startswith("Closeout")
		else disposition_options(source_stock_entry)
	)
	if not action.startswith("Closeout") and action not in options["actions"]:
		frappe.throw("当前物料不允许这种处置方式。")
	company = options["company"]
	authorize(company)
	configured = frappe.db.get_value(
		"Company", company, ["default_scrap_warehouse", "custom_material_rework_warehouse"], as_dict=True
	)
	routes = {r.key: r for r in options["materials"]}
	selected = []
	seen = set()
	for value in items:
		key = value.get("material_key")
		qty = flt(value.get("qty"), 6)
		r = routes.get(key)
		if key in seen or not r or qty <= 0 or qty > r.requestable_qty + EPSILON:
			frappe.throw("所选物料重复、数量无效或可处理数量已变化，请刷新。")
		seen.add(key)
		target = (
			r.return_warehouse
			if action in {"Closeout Return", "Release"}
			else options["warehouse_settings"].quarantine_warehouse
			if action == "Closeout Quarantine"
			else options["warehouse_settings"].scrap_warehouse
			if action == "Closeout Scrap"
			else configured.default_scrap_warehouse
			if action == "Scrap"
			else configured.custom_material_rework_warehouse
			if action == "Rework"
			else None
		)
		if action not in {"Dispose", "Supplier Return"}:
			valid_warehouse(target, company, r.source_warehouse)
		if action in {"Closeout Quarantine", "Closeout Scrap", "Scrap", "Rework"}:
			wip = frappe.db.get_value("Work Order", work_order, "wip_warehouse") if work_order else None
			forbidden = {r.return_warehouse, wip, r.source_warehouse}
			if action == "Rework":
				forbidden.add(configured.default_scrap_warehouse)
			if target in forbidden:
				frappe.throw("待检、报废或返工仓必须独立配置，不能使用正常发料仓或在制品仓。")
		selected.append(
			dict(
				material_key=key,
				item_code=r.item_code,
				item_name=r.item_name,
				original_item=r.original_item,
				stock_uom=r.stock_uom,
				qty=qty,
				source_warehouse=r.source_warehouse,
				target_warehouse=target,
				original_source_warehouse=r.return_warehouse,
				source_detail=r.get("source_detail"),
				purchase_receipt_item=value.get("purchase_receipt_item"),
				lots_json=json.dumps([dict(x) for x in r.lots]),
			)
		)
	doc = frappe.get_doc(
		dict(
			doctype="Material Handling Request",
			request_key=request_key,
			action=action,
			status="Pending Approval",
			company=company,
			work_order=work_order or options.get("work_order"),
			source_stock_entry=source_stock_entry,
			reason=str(reason or LABELS[action]).strip()[:1000],
			purchase_receipt=purchase_receipt,
			rework_bom=rework_bom,
			requested_by=frappe.session.user,
			requested_at=now_datetime(),
			items=selected,
		)
	)
	save(doc)
	# A manager's recorded decision is the approval; ordinary closeout returns
	# need warehouse receipt only. No second approval of the same decision.
	if user_roles().intersection(ADMIN_REVIEW_ROLES) or action == "Closeout Return":
		approve(doc.name)
	else:
		notify(doc)
	return doc.reload()


def lock_request(name):
	initial = frappe.db.get_value(
		"Material Handling Request", name, ["work_order", "source_stock_entry", "company"], as_dict=True
	)
	if not initial:
		frappe.throw("物料处理申请不存在。")
	authorize(initial.company)
	lock_scope(initial.work_order, initial.source_stock_entry)
	return frappe.get_doc("Material Handling Request", name, for_update=True)


def current_routes(doc):
	options = (
		closeout_options(doc.work_order, doc.name, for_update=True)
		if doc.action.startswith("Closeout")
		else disposition_options(doc.source_stock_entry, doc.name)
	)
	return {r.key: r for r in options["materials"]}


def approve(name):
	doc = lock_request(name)
	if doc.status in {"Awaiting Stock Entry", "Completed"}:
		return doc
	if doc.action != "Closeout Return":
		authorize(doc.company, review=True)
	if doc.status != "Pending Approval":
		frappe.throw("只能确认待审核的申请。")
	routes = current_routes(doc)
	for row in doc.items:
		r = routes.get(row.material_key)
		if not r or row.qty > r.requestable_qty + EPSILON:
			frappe.throw("可处理数量已变化，请撤回并重新申请。")
	if doc.action == "Supplier Return":
		from process_simplification.production_exceptions.handling_followup import make_supplier_return

		native = make_supplier_return(doc, routes)
		doc.purchase_return = native.name
	else:
		entry = frappe.new_doc("Stock Entry")
		entry.company = doc.company
		entry.custom_material_handling_request = doc.name
		entry.purpose = (
			"Material Transfer for Manufacture"
			if doc.action.startswith("Closeout")
			else "Material Issue"
			if doc.action == "Dispose"
			else "Material Transfer"
		)
		entry.work_order = doc.work_order if doc.action.startswith("Closeout") else None
		entry.is_return = int(doc.action.startswith("Closeout"))
		entry.remarks = LABELS[doc.action] + "：" + doc.reason + "（" + doc.name + "）"
		for row in doc.items:
			for value in stock_rows(routes[row.material_key], row.qty, row.target_warehouse):
				value["custom_return_source_warehouse"] = row.original_source_warehouse
				value["custom_material_source_detail"] = row.source_detail
				entry.append("items", value)
		entry.set_stock_entry_type()
		entry.insert(ignore_permissions=True)
		doc.stock_entry = entry.name
		if doc.action == "Rework":
			from process_simplification.production_exceptions.handling_followup import make_rework_order

			doc.rework_order = make_rework_order(doc).name
	doc.reviewed_by = frappe.session.user
	doc.reviewed_at = now_datetime()
	doc.status = "Awaiting Stock Entry"
	save(doc)
	notify(doc)
	return doc


def withdraw(name, reason, reject=False):
	doc = lock_request(name)
	if doc.status in {"Withdrawn", "Rejected"}:
		return doc
	if doc.status not in OPEN:
		frappe.throw("已过账申请不能撤回，请先核对关联单据。")
	if (doc.stock_entry and frappe.db.get_value("Stock Entry", doc.stock_entry, "docstatus") == 1) or (
		doc.purchase_return and frappe.db.get_value("Purchase Receipt", doc.purchase_return, "docstatus") == 1
	):
		frappe.throw("关联单据已过账，请先取消后再撤回。")
	if doc.requested_by != frappe.session.user:
		authorize(doc.company, review=True)
	if reject:
		authorize(doc.company, review=True)
	if not str(reason or "").strip():
		frappe.throw("请填写原因。")
	doc.status = "Rejected" if reject else "Withdrawn"
	doc.withdrawal_reason = reason
	doc.withdrawn_by = frappe.session.user
	doc.withdrawn_at = now_datetime()
	save(doc)
	notify(doc)
	return doc


def validate_stock(entry):
	name = entry.get("custom_material_handling_request") or frappe.db.get_value(
		"Material Handling Request", {"stock_entry": entry.name}, "name"
	)
	if not name:
		return
	entry.custom_material_handling_request = name
	doc = lock_request(name)
	if doc.status != "Awaiting Stock Entry" or doc.stock_entry != entry.name:
		frappe.throw("该处理单已撤回或不再等待此库存单，不能提交。")
	expected_purpose = (
		"Material Transfer for Manufacture"
		if doc.action.startswith("Closeout")
		else "Material Issue"
		if doc.action == "Dispose"
		else "Material Transfer"
	)
	if (
		entry.company != doc.company
		or entry.purpose != expected_purpose
		or bool(entry.is_return) != doc.action.startswith("Closeout")
		or (entry.work_order or None) != (doc.work_order if doc.action.startswith("Closeout") else None)
	):
		frappe.throw("库存单类型或公司与已确认申请不一致。")
	expected = defaultdict(float)
	actual = defaultdict(float)
	for r in doc.items:
		expected[
			(
				r.item_code,
				r.original_item,
				r.source_warehouse,
				r.target_warehouse or None,
				r.original_source_warehouse,
				r.source_detail or None,
			)
		] += r.qty
	for r in entry.items:
		actual[
			(
				r.item_code,
				r.original_item or r.item_code,
				r.s_warehouse,
				r.t_warehouse or None,
				r.get("custom_return_source_warehouse"),
				r.get("custom_material_source_detail") or None,
			)
		] += flt(r.transfer_qty or r.qty)
	if {k: flt(v, 6) for k, v in expected.items()} != {k: flt(v, 6) for k, v in actual.items()}:
		frappe.throw("库存单的物料、数量或仓库路径与确认申请不一致。")
	routes = current_routes(doc)
	for r in doc.items:
		route = routes.get(r.material_key)
		if not route or r.qty > route.requestable_qty + EPSILON:
			frappe.throw("来源物料剩余量不足，请重新核对。")
	validate_lots(entry, doc, routes)
	validate_physical_stock(entry)


def validate_lots(entry, doc, routes):
	from process_simplification.production_exceptions.material_routes import _fragments

	bundles = defaultdict(list)
	names = [r.serial_and_batch_bundle for r in entry.get("items") if r.serial_and_batch_bundle]
	if names:
		for r in frappe.get_all(
			"Serial and Batch Entry",
			filters={"parent": ["in", names]},
			fields=["parent", "batch_no", "serial_no", "qty"],
			limit=0,
		):
			bundles[r.parent].append(r)
	allowed = defaultdict(float)
	used = defaultdict(float)
	approved_keys = (
		{r.material_key for r in doc.items}
		if doc.doctype == "Material Handling Request"
		else {doc.material_key}
	)
	selected = {key: route for key, route in routes.items() if key in approved_keys}
	for key, route in selected.items():
		for lot in route.lots:
			allowed[(key, lot.batch_no or "", lot.serial_no or "")] += lot.qty
	for r in entry.get("items"):
		matching = [
			key
			for key, route in selected.items()
			if route.item_code == r.item_code
			and route.source_warehouse == r.s_warehouse
			and (not r.get("custom_material_source_detail") or key == r.custom_material_source_detail)
			and (
				not r.get("custom_return_source_warehouse")
				or route.return_warehouse == r.custom_return_source_warehouse
			)
			and (not r.get("original_item") or route.original_item == r.original_item)
		]
		if len(matching) != 1:
			frappe.throw("批次或序列号的物料来源不唯一，请核对来源行。")
		for batch, serial, qty in _fragments(r, bundles):
			used[(matching[0], batch or "", serial or "")] += qty
	if any(qty > allowed[key] + EPSILON for key, qty in used.items()):
		frappe.throw("批次或序列号不属于该来源的可退物料，不能串用其他工单的物料。")


def complete_stock(entry):
	name = entry.get("custom_material_handling_request")
	if not name:
		return
	doc = lock_request(name)
	doc.status = "Completed"
	doc.processed_by = frappe.session.user
	doc.processed_at = now_datetime()
	save(doc)
	notify(doc)


def assert_no_downstream(entry):
	if not entry.get("custom_production_exception_request") and not entry.get(
		"custom_material_handling_request"
	):
		return
	work_order = entry.work_order
	if not work_order and entry.get("custom_material_handling_request"):
		work_order = frappe.db.get_value(
			"Material Handling Request", entry.custom_material_handling_request, "work_order"
		)
	lock_scope(work_order, entry.name)
	request = frappe.qb.DocType("Material Handling Request")
	active = (
		frappe.qb.from_(request)
		.select(request.name)
		.where((request.source_stock_entry == entry.name) & request.status.isin(list(OPEN | {"Completed"})))
		.for_update()
		.run()
	)
	if active:
		frappe.throw("已有待检、返工、报废或供应商退货的后续处理，请先撤回或取消后续单据。")


def cancel_stock(entry):
	name = entry.get("custom_material_handling_request")
	if not name:
		return
	doc = lock_request(name)
	if doc.rework_order and frappe.db.get_value("Work Order", doc.rework_order, "docstatus") == 1:
		frappe.throw("返工工单已提交，请先处理返工工单再取消转仓。")
	doc.stock_entry = None
	doc.status = "Pending Approval"
	doc.processed_by = None
	doc.processed_at = None
	save(doc)
	notify(doc)


def post(name):
	doc = lock_request(name)
	if doc.status == "Completed":
		return doc
	if doc.status != "Awaiting Stock Entry":
		frappe.throw("当前申请还没有等待过账的单据。")
	native = (
		frappe.get_doc("Purchase Receipt", doc.purchase_return)
		if doc.purchase_return
		else frappe.get_doc("Stock Entry", doc.stock_entry)
	)
	if not frappe.has_permission(native.doctype, "submit", doc=native):
		frappe.throw("当前账号没有库房过账权限。", frappe.PermissionError)
	native.submit()
	return doc.reload()


def dashboard():
	authorize()
	companies = user_company_scope()
	filters = {} if companies is None else {"company": ["in", sorted(companies)]}
	fields = [
		"name",
		"action",
		"status",
		"company",
		"work_order",
		"source_stock_entry",
		"stock_entry",
		"purchase_return",
		"rework_order",
		"reason",
		"requested_by",
		"requested_at",
	]
	# Open work must never disappear behind a page of newer completed records.
	requests = frappe.get_all(
		"Material Handling Request",
		filters={**filters, "status": ["in", sorted(OPEN)]},
		fields=fields,
		order_by="creation asc",
		limit=0,
	)
	requests += frappe.get_all(
		"Material Handling Request",
		filters={**filters, "status": ["not in", sorted(OPEN)]},
		fields=fields,
		order_by="modified desc",
		limit=30,
	)
	for r in requests:
		r.label = LABELS[r.action]
		r.items = frappe.get_all(
			"Material Handling Item",
			filters={"parent": r.name},
			fields=["item_code", "item_name", "qty", "stock_uom", "source_warehouse", "target_warehouse"],
			order_by="idx",
			limit=100,
		)
	sources = frappe.get_all(
		"Production Exception Request",
		filters={**filters, "status": "Completed"},
		fields=["stock_entry", "item_code", "item_name", "qty", "request_type", "cause", "material_action"],
		order_by="modified desc",
		limit=0,
	)
	sources = [
		s
		for s in sources
		if s.stock_entry
		and (s.request_type == "Material Scrap" or (s.cause == "Material Defect" and s.material_action))
	]
	handled = frappe.get_all(
		"Material Handling Request",
		filters={
			**filters,
			"status": "Completed",
			"action": ["in", ["Closeout Quarantine", "Closeout Scrap", "Scrap"]],
		},
		fields=["stock_entry", "action"],
		order_by="modified desc",
		limit=0,
	)
	sources += [
		frappe._dict(stock_entry=r.stock_entry, item_name=LABELS[r.action]) for r in handled if r.stock_entry
	]
	if sources:
		names = list({s.stock_entry for s in sources})
		total = defaultdict(float)
		used = defaultdict(float)
		for r in frappe.get_all(
			"Stock Entry Detail",
			filters={"parent": ["in", names], "docstatus": 1},
			fields=["parent", "transfer_qty"],
			limit=0,
		):
			total[r.parent] += flt(r.transfer_qty)
		parent = frappe.qb.DocType("Material Handling Request")
		child = frappe.qb.DocType("Material Handling Item")
		query = (
			frappe.qb.from_(parent)
			.join(child)
			.on(child.parent == parent.name)
			.select(parent.source_stock_entry, child.qty)
			.where(parent.source_stock_entry.isin(names) & parent.status.isin(list(OPEN | {"Completed"})))
		)
		for r in query.run(as_dict=True):
			used[r.source_stock_entry] += flt(r.qty)
		sources = [s for s in sources if total[s.stock_entry] - used[s.stock_entry] > EPSILON]
	return dict(
		requests=requests,
		sources=sources,
		can_post=bool(user_roles().intersection(STOCK_ROLES)),
		can_review=bool(user_roles().intersection(ADMIN_REVIEW_ROLES)),
		labels=LABELS,
	)


def validate_cancel(entry):
	name = entry.get("custom_material_handling_request")
	if not name:
		return
	doc = lock_request(name)
	if doc.stock_entry != entry.name or doc.status != "Completed":
		frappe.throw("库存单与物料处理状态不一致，不能取消。")
	if doc.rework_order and frappe.db.get_value("Work Order", doc.rework_order, "docstatus") == 1:
		frappe.throw("返工工单已提交，请先处理返工工单再取消转仓。")


def query_condition(user=None):
	from process_simplification.management_access import user_company_scope

	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	if user != "Administrator" and not roles.intersection(ADMIN_REVIEW_ROLES | STOCK_ROLES):
		return "1=0"
	companies = user_company_scope(user)
	if companies is None:
		return ""
	if not companies:
		return "1=0"
	return (
		"`tabMaterial Handling Request`.company in ("
		+ ", ".join(frappe.db.escape(c) for c in sorted(companies))
		+ ")"
	)


def document_permission(doc, ptype=None, user=None, **kwargs):
	if (ptype or "read") not in {"read", "select", "print", "report", "export"}:
		return False
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	companies = user_company_scope(user)
	return bool(
		(user == "Administrator" or roles.intersection(ADMIN_REVIEW_ROLES | STOCK_ROLES))
		and (companies is None or doc.company in companies)
	)


def posting_preview(name):
	doc = frappe.get_doc("Material Handling Request", name)
	authorize(doc.company)
	native = (
		frappe.get_doc("Purchase Receipt", doc.purchase_return)
		if doc.purchase_return
		else frappe.get_doc("Stock Entry", doc.stock_entry)
	)
	if not frappe.has_permission(native.doctype, "read", doc=native):
		frappe.throw("当前账号不能查看这张库房单据。", frappe.PermissionError)
	from process_simplification.production_exceptions.material_routes import _fragments

	bundles = defaultdict(list)
	names = [r.serial_and_batch_bundle for r in native.items if r.serial_and_batch_bundle]
	if names:
		for row in frappe.get_all(
			"Serial and Batch Entry",
			filters={"parent": ["in", names]},
			fields=["parent", "batch_no", "serial_no", "qty"],
			limit=0,
		):
			bundles[row.parent].append(row)
	rows = []
	for r in native.items:
		v = frappe._dict(r.as_dict())
		if native.doctype == "Purchase Receipt":
			v.transfer_qty = abs(flt(r.stock_qty))
		rows.append(
			dict(
				item_code=r.item_code,
				item_name=r.item_name,
				qty=v.transfer_qty,
				stock_uom=r.stock_uom,
				source=r.get("s_warehouse") or r.get("warehouse"),
				target=r.get("t_warehouse"),
				expense_account=r.get("expense_account"),
				lots=[dict(batch_no=b, serial_no=s, qty=q) for b, s, q in _fragments(v, bundles)],
			)
		)
	return dict(
		doctype=native.doctype,
		name=native.name,
		items=rows,
		supplier=native.get("supplier"),
		currency=native.get("currency"),
		grand_total=native.get("grand_total"),
		return_against=native.get("return_against"),
	)


def notify(doc):
	from process_simplification.notifications import notify_material_handling

	return notify_material_handling(doc)


def validate_physical_stock(entry):
	"""This guided flow cannot issue nonexistent stock even if native negatives are enabled."""
	required = defaultdict(float)
	for row in entry.get("items"):
		if row.s_warehouse:
			required[(row.item_code, row.s_warehouse)] += flt(row.transfer_qty)
	for (item, warehouse), qty in sorted(required.items()):
		actual = (
			frappe.db.get_value(
				"Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty", for_update=True
			)
			or 0
		)
		if flt(actual) + EPSILON < qty:
			frappe.throw(
				"物料 {0} 在 {1} 的当前库存不足，请库房核对实物和库存流水后再处理。".format(item, warehouse)
			)
