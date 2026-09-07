from __future__ import annotations

import json
import multiprocessing
from copy import deepcopy
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
from erpnext.stock.doctype.purchase_receipt.purchase_receipt import (
	make_purchase_invoice,
	make_purchase_return,
)
from process_simplification.management_access import (
	OWNER_ROLE,
	PRODUCTION_MANAGER_ROLE,
	ROLE_DEFINITION_BY_ROLE,
	WAREHOUSE_OPERATOR_ROLE,
	ensure_management_access,
)
from process_simplification.purchasing import allocation, receipts
from process_simplification.tests import test_material_coverage_integration as coverage_fixtures


def _parallel_purchase_action(site, barrier, output, action):
	"""A separate connection/process, not a mocked lock or shared test transaction."""
	frappe.init(site=site, sites_path="/sites")
	frappe.connect()
	frappe.set_user(action.get("user", "Administrator"))
	frappe.flags.in_test = True
	barrier.wait(timeout=30)
	try:
		for attempt in range(3):
			try:
				with patch.object(receipts, "enqueue_event"):
					if action["kind"] == "receipt":
						frappe.get_doc("Purchase Receipt", action["name"]).submit()
					elif action["kind"] == "deliver":
						receipts.deliver_event(action["name"])
					elif action["kind"] == "allocate":
						allocation.create_purchase_orders(action["name"], action["key"], action["data"])
					elif action["kind"] == "native":
						from erpnext.stock.doctype.material_request.material_request import (
							make_purchase_order,
						)

						po = make_purchase_order(action["name"], args={"supplier": action["supplier"]})
						po.items[0].qty = 100
						po.insert()
					frappe.db.commit()
				output.put({"ok": True, "attempts": attempt + 1})
				break
			except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
				frappe.db.rollback()
				if attempt == 2:
					raise
	except Exception as exc:
		frappe.db.rollback()
		output.put({"ok": False, "error": type(exc).__name__, "message": str(exc)})
	finally:
		frappe.destroy()


class TestPurchaseAllocationReceipts(IntegrationTestCase):
	TEST_COMPANY = "Material Coverage Test Company"
	TEST_COMPANY_ABBR = "MCT"
	OTHER_COMPANY = "Material Coverage Other Company"
	OTHER_COMPANY_ABBR = "MCO"
	_ensure_company = classmethod(coverage_fixtures.TestMaterialCoverageIntegration._ensure_company.__func__)
	_make_item = coverage_fixtures.TestMaterialCoverageIntegration._make_item
	_make_warehouse = coverage_fixtures.TestMaterialCoverageIntegration._make_warehouse

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		settings = frappe.get_single(receipts.SETTINGS)
		cls.original_settings = {
			key: deepcopy(settings.as_dict().get(key))
			for key in (
				"enable_process_notifications",
				"enable_notification_sound",
				"purchase_receipt_notifications_started_at",
				"notification_recipients",
				"notification_role_recipients",
			)
		}
		cls._ensure_company(cls.TEST_COMPANY, cls.TEST_COMPANY_ABBR)
		cls._ensure_company(cls.OTHER_COMPANY, cls.OTHER_COMPANY_ABBR)
		ensure_management_access()
		cls.users = {}
		for key, role in (
			("p", PRODUCTION_MANAGER_ROLE),
			("o", OWNER_ROLE),
			("w", WAREHOUSE_OPERATOR_ROLE),
			("x", PRODUCTION_MANAGER_ROLE),
		):
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": f"rcn-{key}-{frappe.generate_hash(length=8)}@example.com",
					"first_name": f"RCN Test {key}",
					"send_welcome_email": 0,
					"role_profiles": [{"role_profile": ROLE_DEFINITION_BY_ROLE[role]["profile"]}],
				}
			).insert(ignore_permissions=True)
			frappe.get_doc(
				{
					"doctype": "User Permission",
					"user": user.name,
					"allow": "Company",
					"for_value": cls.OTHER_COMPANY if key == "x" else cls.TEST_COMPANY,
					"apply_to_all_doctypes": 1,
				}
			).insert(ignore_permissions=True)
			cls.users[key] = user.name
		cls.suppliers = [
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "MSP Test " + frappe.generate_hash(length=8),
					"supplier_group": "All Supplier Groups",
				}
			)
			.insert()
			.name
			for _ in range(3)
		]
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		try:
			frappe.set_user("Administrator")
			frappe.db.rollback()
			settings = frappe.get_single(receipts.SETTINGS)
			settings.update(cls.original_settings)
			settings.save(ignore_permissions=True)
			for name in cls.users.values():
				user = frappe.get_doc("User", name)
				user.enabled = 0
				user.save(ignore_permissions=True)
			frappe.db.commit()
		finally:
			super().tearDownClass()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.warehouse = "Stores - MCT"
		settings = frappe.get_single("Process Simplification Settings")
		settings.enable_process_notifications = 1
		settings.enable_notification_sound = 1
		settings.set("notification_recipients", [])
		settings.set("notification_role_recipients", [])
		settings.save(ignore_permissions=True)
		receipts.ensure_defaults()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		super().tearDown()

	def mr(self, quantities=(100,), same_item=False, uoms=None, stock_uoms=None, warehouses=None):
		items = [self._make_item("RCN-MSP", uoms=uoms) for _ in (quantities[:1] if same_item else quantities)]
		for item, unit in zip(items, stock_uoms or [], strict=False):
			item.stock_uom = unit
			if unit not in {row.uom for row in item.uoms}:
				item.append("uoms", {"uom": unit, "conversion_factor": 1})
			item.save()
		doc = frappe.get_doc(
			{
				"doctype": "Material Request",
				"company": self.TEST_COMPANY,
				"material_request_type": "Purchase",
				"transaction_date": nowdate(),
				"schedule_date": add_days(nowdate(), 1),
				"items": [
					{
						"item_code": items[0 if same_item else i].name,
						"qty": qty,
						"warehouse": warehouses[i] if warehouses else self.warehouse,
						"schedule_date": add_days(nowdate(), 1),
						"uom": items[0 if same_item else i].stock_uom,
						"conversion_factor": 1,
					}
					for i, qty in enumerate(quantities)
				],
			}
		)
		doc.insert().submit()
		return doc

	def data(self, mr, splits=None, quantities=None):
		splits = splits or [(0, 0, mr.items[0].qty)]
		rows = [
			{
				"material_request_item": mr.items[item_index].name,
				"supplier": self.suppliers[supplier_index],
				"qty": qty,
				"uom": mr.items[item_index].uom,
				"rate": 5,
				"currency": "INR",
				"schedule_date": add_days(nowdate(), 1),
				"warehouse": mr.items[item_index].warehouse,
			}
			for item_index, supplier_index, qty in splits
		]
		totals = quantities or {
			item.name: sum(row["qty"] for row in rows if row["material_request_item"] == item.name)
			for item in mr.items
		}
		return {
			"company": self.TEST_COMPANY,
			"quantities": {name: qty for name, qty in totals.items() if qty},
			"rows": rows,
		}

	def orders(self, mr, data=None, submit=True, key=None):
		result = allocation.create_purchase_orders(
			mr.name, key or frappe.generate_hash(), data or self.data(mr)
		)
		orders = [frappe.get_doc("Purchase Order", row["name"]) for row in result["orders"]]
		orders.sort(key=lambda po: self.suppliers.index(po.supplier))
		if submit:
			for po in orders:
				po.submit()
		return orders

	def receive(self, po, qty, rejected=0, submit=True):
		pr = make_purchase_receipt(po.name)
		pr.items[0].qty = qty
		pr.items[0].received_qty = qty + rejected
		pr.items[0].rejected_qty = rejected
		if rejected:
			pr.items[0].rejected_warehouse = self._make_warehouse("RCN Rejected")
		pr.insert()
		if submit:
			pr.submit()
		return pr

	def event(self, pr, kind="Arrival", deliver=True):
		name = receipts.event_name(pr.name, kind)
		if deliver:
			receipts.deliver_event(name)
		return frappe.get_doc(receipts.EVENT, name)

	def assert_logs(self, events, count, users=("p", "o")):
		for key in users:
			self.assertEqual(
				frappe.db.count(
					"Notification Log",
					{
						"document_type": receipts.EVENT,
						"document_name": ["in", [event.name for event in events]],
						"for_user": self.users[key],
					},
				),
				count,
			)

	def stock(self, mr):
		return (
			frappe.db.get_value(
				"Bin", {"item_code": mr.items[0].item_code, "warehouse": self.warehouse}, "actual_qty"
			)
			or 0
		)

	def test_three_batches_across_suppliers_are_three_notifications(self):
		"""RCN-001/002/003, MSP-007: actual standard PR/SLE transitions, unchanged MR status."""
		mr = self.mr()
		a, b = self.orders(mr, self.data(mr, [(0, 0, 60), (0, 1, 40)]))
		events = []
		for po, qty, total in ((a, 60, 60), (b, 20, 80), (b, 20, 100)):
			pr = self.receive(po, qty)
			event = self.event(pr)
			events.append(event)
			item = json.loads(event.snapshot)["requests"][0]["items"][0]
			self.assertEqual((item["received_qty"], item["remaining_qty"]), (total, 100 - total))
			self.assertEqual(self.stock(mr), total)
			self.assertEqual(pr.items[0].material_request_item, mr.items[0].name)
			self.assert_logs(events, len(events))
			self.assertEqual(
				frappe.db.get_value("Material Request", mr.name, "status"),
				"Received" if total == 100 else "Partially Received",
			)
		self.assertIn("全部到货", receipts._message(events[-1])[0])
		self.assertEqual(
			frappe.db.count(
				"Notification Log", {"document_type": "Material Request", "document_name": mr.name}
			),
			0,
		)
		context = allocation.get_allocation_context(mr.name)
		self.assertEqual(context["items"][0]["received_qty"], 100)
		self.assertEqual(context["items"][0]["ordered_pending_qty"], 0)

	def test_compact_notification_keeps_detail_and_existing_read_state(self):
		from process_simplification.patches.v0_0.compact_receipt_notifications import execute

		mr = self.mr()
		po = self.orders(mr)[0]
		event = self.event(self.receive(po, 60))
		log = frappe.get_doc("Notification Log", event.deliveries[0].notification_log)
		self.assertIn("本批到货 1 项物料", log.description)
		self.assertNotIn("统计时点", log.description)
		self.assertEqual(log.title, log.subject)
		self.assertNotIn(event.receipt, log.title)
		detail = receipts.get_notice(event.name)
		self.assertIn("统计时点", detail["description"])
		self.assertIn("剩余 40", detail["description"])
		frappe.db.set_value("Notification Log", log.name, {"title": detail["subject"], "description": detail["description"], "read": 1}, update_modified=False)
		original = {key: log.get(key) for key in ("creation", "modified", "for_user")}
		snapshot = event.snapshot
		with patch("frappe.model.document.Document.insert") as insert:
			self.assertGreaterEqual(execute(), 1)
			self.assertEqual(execute(), 0)
			insert.assert_not_called()
		log.reload()
		self.assertEqual(log.read, 1)
		self.assertEqual({key: log.get(key) for key in original}, original)
		self.assertEqual(log.description, receipts._notification_message(event)[1])
		self.assertEqual(log.title, log.subject)
		self.assertNotIn(event.receipt, log.title)
		self.assertEqual(log.link, f"/desk/purchase-receipt-notice?name={event.name}")
		event.reload()
		self.assertEqual(event.snapshot, snapshot)

	def test_multiple_items_one_event_and_exact_source_rows(self):
		"""RCN-004, MSP-001/008: duplicate item codes retain source row identity."""
		mr = self.mr((100, 30, 20), same_item=True)
		orders = self.orders(
			mr, self.data(mr, [(0, 0, 60), (0, 1, 40), (1, 0, 30), (2, 2, 20)]), submit=False
		)
		self.assertEqual([len(po.items) for po in orders], [2, 1, 1])
		self.assertEqual(self.stock(mr), 0)
		self.assertEqual(
			{row.material_request_item for po in orders for row in po.items}, {row.name for row in mr.items}
		)
		orders[0].submit()
		pr = make_purchase_receipt(orders[0].name).insert().submit()
		event = self.event(pr)
		self.assert_logs([event], 1)
		self.assertEqual(len(json.loads(event.snapshot)["items"]), 2)

	def test_draft_and_failed_transaction_have_no_event_or_inventory(self):
		"""RCN-005: rollback the real submit including registered event."""
		mr = self.mr()
		pr = self.receive(self.orders(mr)[0], 60, submit=False)
		self.assertFalse(frappe.db.exists(receipts.EVENT, {"receipt": pr.name}))
		frappe.db.savepoint("failed_receipt_submit")
		try:
			pr.submit()
			raise RuntimeError("test transaction owner failure")
		except RuntimeError:
			frappe.db.rollback(save_point="failed_receipt_submit")
		self.assertFalse(frappe.db.exists(receipts.EVENT, {"receipt": pr.name}))
		self.assertEqual(self.stock(mr), 0)
		pr.reload().submit()
		self.assert_logs([self.event(pr)], 1)

	def test_idempotent_event_and_recipient_delivery(self):
		pr = self.receive(self.orders(self.mr())[0], 60)
		event = self.event(pr)
		for _ in range(3):
			self.assertEqual(receipts.record_receipt_event(pr), event.name)
			receipts.deliver_event(event.name)
		self.assertEqual(frappe.db.count(receipts.EVENT, {"receipt": pr.name}), 1)
		self.assert_logs([event], 1)

	def test_partial_failure_retry_limit_and_manual_retry(self):
		"""RCN-008/019: one recipient insert fails; delivered recipients never replay."""
		pr = self.receive(self.orders(self.mr())[0], 60)
		event = self.event(pr, deliver=False)
		from frappe.desk.doctype.notification_log.notification_log import NotificationLog

		insert = NotificationLog.insert

		def failing_insert(log, *args, **kwargs):
			if log.for_user == self.users["o"]:
				insert(log, *args, **kwargs)
				raise RuntimeError("injected after log insertion")
			return insert(log, *args, **kwargs)

		with patch.object(NotificationLog, "insert", failing_insert):
			for _ in range(6):
				receipts.deliver_event(event.name)
				event.reload()
				for row in event.deliveries:
					row.next_attempt = None
				event.save(ignore_permissions=True)
		self.assertEqual(event.status, "Failed")
		self.assert_logs([event], 1, users=("p",))
		self.assert_logs([event], 0, users=("o",))
		receipts.retry_notice(event.name)
		receipts.deliver_event(event.name)
		self.assert_logs([event], 1)

	def test_return_replenish_and_cancel_return(self):
		"""RCN-009/010: signed accepted quantities; no extra PO after a return."""
		mr = self.mr()
		po = self.orders(mr)[0]
		pr = self.receive(po, 100)
		first = self.event(pr)
		ret = make_purchase_return(pr.name)
		ret.items[0].qty = -10
		ret.items[0].received_qty = -10
		ret.insert().submit()
		returned = self.event(ret, "Return")
		self.assertEqual(self.stock(mr), 90)
		self.assertEqual(allocation.get_allocation_context(mr.name)["items"][0]["available_qty"], 0)
		ret.cancel()
		cancelled = self.event(ret, "Return Cancelled")
		self.assertEqual(self.stock(mr), 100)
		self.assert_logs([first, returned, cancelled], 3)

	def test_return_then_replenish_not_deduplicated_by_complete_status(self):
		mr = self.mr()
		po = self.orders(mr)[0]
		pr = self.receive(po, 100)
		first = self.event(pr)
		ret = make_purchase_return(pr.name)
		ret.items[0].qty = ret.items[0].received_qty = -10
		ret.insert().submit()
		returned = self.event(ret, "Return")
		last = self.event(self.receive(po, 10))
		self.assertEqual(self.stock(mr), 100)
		self.assert_logs([first, returned, last], 3)

	def test_cancel_amend_and_delayed_notice_order(self):
		"""RCN-010/018: correction cannot overtake pending original receipt."""
		mr = self.mr()
		po = self.orders(mr)[0]
		pr = self.receive(po, 60)
		arrival = self.event(pr, deliver=False)
		pr.cancel()
		cancelled = self.event(pr, "Receipt Cancelled")
		self.assertEqual(cancelled.status, "Pending")
		arrival = self.event(pr)
		self.assertIn("随后已撤销", receipts._message(arrival)[1])
		cancelled = self.event(pr, "Receipt Cancelled")
		self.assertEqual(self.stock(mr), 0)
		amended = frappe.copy_doc(pr)
		amended.amended_from, amended.docstatus = pr.name, 0
		amended.insert().submit()
		self.assert_logs([arrival, cancelled, self.event(amended)], 3)

	def test_rejected_and_all_rejected_stock_is_not_accepted(self):
		for qty, rejected in ((50, 10), (0, 60)):
			mr = self.mr()
			pr = self.receive(self.orders(mr)[0], qty, rejected)
			event = self.event(pr)
			snapshot = json.loads(event.snapshot)
			self.assertEqual(snapshot["items"][0]["received_qty"], 60)
			self.assertEqual(snapshot["requests"][0]["items"][0]["remaining_qty"], 100 - qty)
			self.assertEqual(self.stock(mr), qty)
			self.assertEqual(
				frappe.db.get_value(
					"Bin",
					{"item_code": mr.items[0].item_code, "warehouse": pr.items[0].rejected_warehouse},
					"actual_qty",
				),
				rejected,
			)
			self.assert_logs([event], 1)

	def test_uom_conversion_and_integer_validation(self):
		mr = self.mr(uoms=[{"uom": "Box", "conversion_factor": 10}])
		data = self.data(mr)
		data["rows"][0].update(qty=10, uom="Box", rate=50)
		po = self.orders(mr, data)[0]
		pr = self.receive(po, 6)
		event = self.event(pr)
		self.assertEqual(self.stock(mr), 60)
		self.assertEqual(json.loads(event.snapshot)["items"][0]["stock_qty"], 60)
		self.assertIn("6 Box", receipts._message(event)[1])
		other = self.mr()
		bad = self.data(other, quantities={other.items[0].name: 0.5})
		bad["rows"][0]["qty"] = 0.5
		with self.assertRaises(frappe.ValidationError):
			allocation.preview_allocation(other.name, bad)

	def test_recipient_union_company_scope_and_readonly_summary(self):
		"""RCN-013: real profiles/company permissions, including the actor."""
		settings = frappe.get_single("Process Simplification Settings")
		for key in ("p", "w"):
			settings.append(
				"notification_recipients",
				{"company": self.TEST_COMPANY, "responsibility": "采购到货", "user": self.users[key]},
			)
		settings.save(ignore_permissions=True)
		po = self.orders(self.mr())[0]
		frappe.set_user(self.users["w"])
		pr = self.receive(po, 60)
		event = self.event(pr)
		self.assert_logs([event], 1, users=("p", "o", "w"))
		self.assert_logs([event], 0, users=("x",))
		frappe.set_user(self.users["p"])
		with patch("frappe.permissions.msgprint") as permission_message:
			self.assertFalse(receipts.get_notice(event.name)["can_open_receipt"])
			permission_message.assert_not_called()
		self.assertFalse(receipts.get_notice(event.name)["can_retry"])
		self.assertNotIn("rate", receipts.get_notice(event.name)["snapshot"]["items"][0])
		with self.assertRaises(frappe.PermissionError):
			receipts.retry_notice(event.name)
		frappe.set_user(self.users["x"])
		with self.assertRaises(frappe.PermissionError):
			receipts.get_notice(event.name)
		with self.assertRaises(frappe.PermissionError):
			allocation.get_allocation_context(po.items[0].material_request)

	def test_global_and_personal_disable_no_replay_no_email(self):
		mr = self.mr()
		po = self.orders(mr)[0]
		frappe.db.set_single_value(receipts.SETTINGS, "enable_process_notifications", 0)
		pr = self.receive(po, 60)
		event = self.event(pr)
		self.assertEqual(event.status, "Suppressed")
		frappe.db.set_single_value(receipts.SETTINGS, "enable_process_notifications", 1)
		with self.assertRaises(frappe.ValidationError):
			receipts.retry_notice(event.name)
		self.assert_logs([event], 0)
		settings = frappe.get_doc("Notification Settings", self.users["p"])
		settings.enabled = 0
		settings.save(ignore_permissions=True)
		mail_count = frappe.db.count("Email Queue")
		second = self.event(self.receive(po, 20))
		self.assert_logs([second], 0, users=("p",))
		self.assert_logs([second], 1, users=("o",))
		self.assertEqual(frappe.db.count("Email Queue"), mail_count)

	def test_no_recipients_and_revoked_user_visible_status(self):
		with patch.object(receipts, "responsibility_recipients", return_value=[]):
			pr = self.receive(self.orders(self.mr())[0], 60)
		event = self.event(pr)
		self.assertEqual(event.status, "No Recipients")
		self.assertEqual(len(event.deliveries), 0)
		receipts.retry_notice(event.name)
		frappe.db.set_value("User", self.users["p"], "enabled", 0)
		receipts.deliver_event(event.name)
		self.assert_logs([event], 0, users=("p",))
		self.assert_logs([event], 1, users=("o",))

	def test_activation_does_not_backfill_and_ordinary_invoice_no_event(self):
		po = self.orders(self.mr())[0]
		with patch.object(receipts, "events_enabled", return_value=False):
			old = self.receive(po, 20)
			draft = self.receive(po, 20, submit=False)
		receipts.ensure_defaults()
		self.assertFalse(frappe.db.exists(receipts.EVENT, {"receipt": old.name}))
		draft.submit()
		event = self.event(draft)
		invoice = make_purchase_invoice(draft.name)
		invoice.bill_no = "RCN-invoice"
		invoice.insert().submit()
		self.assertEqual(frappe.db.count(receipts.EVENT, {"receipt": draft.name}), 1)
		old.cancel()
		self.assert_logs([event, self.event(old, "Receipt Cancelled")], 2)

	def test_empty_activation_datetime_is_not_enabled_and_is_initialized_once(self):
		frappe.db.set_single_value(receipts.SETTINGS, "purchase_receipt_notifications_started_at", None)
		self.assertFalse(receipts.events_enabled())
		receipts.ensure_defaults()
		started = frappe.get_single(receipts.SETTINGS).purchase_receipt_notifications_started_at
		self.assertTrue(started)
		self.assertTrue(receipts.events_enabled())
		receipts.ensure_defaults()
		self.assertEqual(
			frappe.get_single(receipts.SETTINGS).purchase_receipt_notifications_started_at, started
		)

	def test_direct_receipt_has_no_invented_request_progress(self):
		po = self.orders(self.mr())[0]
		pr = make_purchase_receipt(po.name)
		pr.items[0].material_request = pr.items[0].material_request_item = None
		pr.items[0].purchase_order = pr.items[0].purchase_order_item = None
		pr.insert().submit()
		event = self.event(pr)
		self.assertEqual(json.loads(event.snapshot)["requests"], [])
		self.assert_logs([event], 1)

	def test_partial_allocation_invalid_totals_and_idempotency(self):
		mr = self.mr()
		good = self.data(mr, [(0, 0, 50), (0, 1, 30)])
		for invalid in (0, -1, 29, 31, 500):
			bad = deepcopy(good)
			bad["rows"][1]["qty"] = invalid
			with self.assertRaises(frappe.ValidationError):
				allocation.create_purchase_orders(mr.name, frappe.generate_hash(), bad)
		self.assertEqual(frappe.db.count("Purchase Order Item", {"material_request": mr.name}), 0)
		first = allocation.create_purchase_orders(mr.name, "repeat", good)
		self.assertEqual(first, allocation.create_purchase_orders(mr.name, "repeat", good))
		changed = deepcopy(good)
		changed["rows"][0]["rate"] = 6
		with self.assertRaises(frappe.ValidationError):
			allocation.create_purchase_orders(mr.name, "repeat", changed)
		self.assertEqual(allocation.get_allocation_context(mr.name)["items"][0]["available_qty"], 20)

	def test_native_draft_edits_delete_close_reopen_guard(self):
		mr = self.mr()
		po = self.orders(mr, self.data(mr, [(0, 0, 60)]), submit=False)[0]
		from erpnext.stock.doctype.material_request.material_request import make_purchase_order

		native = make_purchase_order(mr.name, args={"supplier": self.suppliers[1]})
		with self.assertRaises(frappe.ValidationError):
			native.insert()
		po.items[0].qty = 50
		po.save()
		self.assertEqual(allocation.get_allocation_context(mr.name)["items"][0]["available_qty"], 50)
		po.submit()
		po.update_status("Closed")
		self.assertEqual(allocation.get_allocation_context(mr.name)["items"][0]["available_qty"], 100)
		second = self.orders(mr, self.data(mr), submit=False)[0]
		frappe.db.savepoint("native_reopen")
		with self.assertRaises(frappe.ValidationError):
			po.update_status("To Receive and Bill")
		frappe.db.rollback(save_point="native_reopen")
		frappe.delete_doc("Purchase Order", second.name)
		self.assertEqual(allocation.get_allocation_context(mr.name)["items"][0]["draft_qty"], 0)

	def test_second_po_failure_rolls_back_entire_batch(self):
		mr = self.mr()
		data = self.data(mr, [(0, 0, 60), (0, 1, 40)])
		from erpnext.buying.doctype.purchase_order.purchase_order import PurchaseOrder

		insert = PurchaseOrder.insert

		def fail_second(po, *args, **kwargs):
			if po.supplier == self.suppliers[1]:
				raise frappe.ValidationError("injected second PO failure")
			return insert(po, *args, **kwargs)

		with patch.object(PurchaseOrder, "insert", fail_second), self.assertRaises(frappe.ValidationError):
			allocation.create_purchase_orders(mr.name, "atomic", data)
		self.assertEqual(frappe.db.count("Purchase Order Item", {"material_request": mr.name}), 0)
		self.assertEqual(frappe.db.count(allocation.BATCH, {"material_request": mr.name}), 0)
		self.assertEqual(len(self.orders(mr, data, key="atomic")), 2)

	def test_terms_currency_company_grouping_and_submit_permissions(self):
		mr = self.mr()
		data = self.data(mr, [(0, 0, 60), (0, 0, 40)])
		terms = frappe.get_doc(
			{
				"doctype": "Terms and Conditions",
				"title": "RCN Terms " + frappe.generate_hash(length=6),
				"buying": 1,
				"terms": "Test purchasing terms",
			}
		).insert()
		data["rows"][1]["tc_name"] = terms.name
		self.assertEqual(len(allocation.preview_allocation(mr.name, data)["groups"]), 2)
		bad = deepcopy(data)
		bad["company"] = self.OTHER_COMPANY
		with self.assertRaises(frappe.ValidationError):
			allocation.preview_allocation(mr.name, bad)
		frappe.set_user(self.users["w"])
		pos = self.orders(mr, data, submit=False)
		self.assertTrue(all(po.docstatus == 0 for po in pos))
		self.assertFalse(allocation.get_allocation_context(mr.name)["can_submit"])
		with self.assertRaises(frappe.PermissionError):
			pos[0].submit()
		self.assertEqual(next(po for po in pos if po.tc_name).terms, terms.terms)

	def _parallel(self, actions):
		context = multiprocessing.get_context("spawn")
		barrier, output = context.Barrier(len(actions)), context.Queue()
		processes = [
			context.Process(
				target=_parallel_purchase_action, args=(frappe.local.site, barrier, output, action)
			)
			for action in actions
		]
		for process in processes:
			process.start()
		try:
			results = [output.get(timeout=55) for _ in processes]
		finally:
			for process in processes:
				process.join(timeout=5)
				if process.is_alive():
					process.terminate()
		frappe.db.rollback()
		return results

	def test_z_concurrent_receipts_snapshot_and_workers(self):
		"""RCN-006/007: two receipt commits; two simultaneous deliveries of the same event."""
		mr = self.mr()
		a, b = self.orders(mr, self.data(mr, [(0, 0, 60), (0, 1, 40)]))
		prs = [self.receive(a, 60, submit=False), self.receive(b, 40, submit=False)]
		frappe.db.commit()
		results = self._parallel([{"kind": "receipt", "name": pr.name} for pr in prs])
		self.assertTrue(all(result["ok"] for result in results), results)
		events = [self.event(pr, deliver=False) for pr in prs]
		totals = sorted(
			json.loads(event.snapshot)["requests"][0]["items"][0]["received_qty"] for event in events
		)
		self.assertEqual(totals[-1], 100)
		self.assertIn(totals[0], [40, 60])
		self.assertEqual(self.stock(mr), 100)
		frappe.db.commit()
		results = self._parallel([{"kind": "deliver", "name": events[0].name}] * 2)
		self.assertTrue(all(result["ok"] for result in results), results)
		self.assert_logs([events[0]], 1)
		receipts.deliver_event(events[1].name)
		self.assert_logs(events, 2)
		frappe.db.commit()

	def test_receipt_does_not_make_an_incomplete_bom_ready(self):
		"""RCN-020: real BOM/Work Order and Bin quantities feed existing readiness logic."""
		from process_simplification.api.production_readiness import (
			allocate_work_order_readiness,
			build_work_order_graph,
		)
		from process_simplification.api.shortage import get_material_stock_snapshot

		mr = self.mr((100, 50))
		finished = self._make_item("RCN-FINISHED")
		bom = (
			frappe.get_doc(
				{
					"doctype": "BOM",
					"item": finished.name,
					"company": self.TEST_COMPANY,
					"currency": "INR",
					"quantity": 1,
					"is_active": 1,
					"is_default": 1,
					"items": [
						{"item_code": row.item_code, "qty": row.qty, "uom": "Nos", "rate": 5}
						for row in mr.items
					],
				}
			)
			.insert()
			.submit()
		)
		wo = frappe.get_doc(
			{
				"doctype": "Work Order",
				"company": self.TEST_COMPANY,
				"production_item": finished.name,
				"bom_no": bom.name,
				"qty": 1,
				"source_warehouse": self.warehouse,
				"wip_warehouse": "Work In Progress - MCT",
				"fg_warehouse": "Finished Goods - MCT",
				"planned_start_date": nowdate(),
			}
		)
		wo.get_items_and_operations_from_bom()
		wo.insert().submit()
		pos = self.orders(mr, self.data(mr, [(0, 0, 100), (1, 1, 50)]))

		def readiness():
			graph = build_work_order_graph(
				{"name": "RCN readiness test"},
				[wo.as_dict()],
				[row.as_dict() for row in wo.required_items],
				[],
				active_bom_items={finished.name},
			)
			stock = {
				(row.item_code, self.warehouse): get_material_stock_snapshot(row.item_code, self.warehouse)
				for row in mr.items
			}
			return allocate_work_order_readiness([graph], stock)[0].work_orders_by_name[wo.name]

		for qty in (60, 40):
			event = self.event(self.receive(pos[0], qty))
			self.assertNotIn("可派工", receipts._message(event)[0])
			state = readiness()
			self.assertFalse(state.can_dispatch)
			self.assertEqual(
				next(
					row for row in state.required_items if row.item_code == mr.items[1].item_code
				).shortage_qty,
				50,
			)
		self.event(self.receive(pos[1], 50))
		self.assertTrue(all(row.shortage_qty == 0 for row in readiness().required_items))

	def test_mixed_units_multiple_requests(self):
		"""RCN-004: each material has its own unit and request progress."""
		first, second = self.mr((10,)), self.mr((5,), stock_uoms=["Kg"])
		a = self.orders(first)[0]
		b = self.orders(second)[0]
		pr = make_purchase_receipt(a.name)
		other = make_purchase_receipt(b.name)
		pr.append("items", other.items[0].as_dict())
		pr.insert().submit()
		event = self.event(pr)
		self.assert_logs([event], 1)
		self.assertEqual(len(json.loads(event.snapshot)["requests"]), 2)
		self.assertEqual({row["stock_uom"] for row in json.loads(event.snapshot)["items"]}, {"Nos", "Kg"})

	def test_z_concurrent_allocations_and_native_first_batch(self):
		"""MSP-003: separate users and native PO creation racing first adoption."""
		for native in (False, True):
			mr = self.mr()
			data = self.data(mr)
			frappe.db.commit()
			first = {
				"kind": "allocate",
				"name": mr.name,
				"key": "concurrent-a",
				"data": data,
				"user": self.users["w"],
			}
			second = (
				{"kind": "native", "name": mr.name, "supplier": self.suppliers[1], "user": self.users["o"]}
				if native
				else {
					"kind": "allocate",
					"name": mr.name,
					"key": "concurrent-b",
					"data": data,
					"user": self.users["o"],
				}
			)
			results = self._parallel([first, second])
			self.assertEqual(sum(result["ok"] for result in results), 1, results)
			self.assertTrue(
				all(result.get("error", "ValidationError") == "ValidationError" for result in results),
				results,
			)
			self.assertEqual(allocation.get_allocation_context(mr.name)["items"][0]["draft_qty"], 100)

	def test_same_item_different_warehouses_return_affects_only_source(self):
		warehouse = self._make_warehouse("MSP other source")
		mr = self.mr((100, 100), same_item=True, warehouses=[self.warehouse, warehouse])
		pos = self.orders(mr, self.data(mr, [(0, 0, 100), (1, 1, 100)]))
		first, second = self.receive(pos[0], 100), self.receive(pos[1], 100)
		self.event(first)
		self.event(second)
		ret = make_purchase_return(second.name)
		ret.items[0].qty = ret.items[0].received_qty = -10
		ret.insert().submit()
		items = json.loads(self.event(ret, "Return").snapshot)["requests"][0]["items"]
		self.assertEqual(
			{row["warehouse"]: row["received_qty"] for row in items}, {self.warehouse: 100, warehouse: 90}
		)
		self.assertEqual(ret.items[0].material_request_item, mr.items[1].name)
		self.assertEqual(
			frappe.db.get_value(
				"Bin", {"item_code": mr.items[0].item_code, "warehouse": warehouse}, "actual_qty"
			),
			90,
		)

	def test_backdated_direct_receipt_not_filtered_by_posting_date(self):
		item = self._make_item("RCN-BACKDATED")
		pr = (
			frappe.get_doc(
				{
					"doctype": "Purchase Receipt",
					"company": self.TEST_COMPANY,
					"supplier": self.suppliers[0],
					"set_posting_time": 1,
					"posting_date": add_days(nowdate(), -2),
					"posting_time": "09:00:00",
					"items": [
						{
							"item_code": item.name,
							"qty": 10,
							"received_qty": 10,
							"warehouse": self.warehouse,
							"rate": 5,
						}
					],
				}
			)
			.insert()
			.submit()
		)
		event = self.event(pr)
		self.assert_logs([event], 1)
		self.assertEqual(json.loads(event.snapshot)["posting_date"], add_days(nowdate(), -2))

	def test_currency_and_tax_headers_preserved_in_separate_pos(self):
		frappe.get_doc(
			{
				"doctype": "Currency Exchange",
				"date": nowdate(),
				"from_currency": "CNY",
				"to_currency": "INR",
				"exchange_rate": 2,
				"for_buying": 1,
			}
		).insert()
		account = frappe.db.get_value(
			"Account", {"company": self.TEST_COMPANY, "account_type": "Tax", "is_group": 0}, "name"
		)
		if not account:
			parent = frappe.db.get_value(
				"Account", {"company": self.TEST_COMPANY, "root_type": "Liability", "is_group": 1}, "name"
			)
			account = (
				frappe.get_doc(
					{
						"doctype": "Account",
						"company": self.TEST_COMPANY,
						"account_name": "MSP Tax " + frappe.generate_hash(length=8),
						"parent_account": parent,
						"account_type": "Tax",
						"is_group": 0,
					}
				)
				.insert()
				.name
			)
		tax = frappe.get_doc(
			{
				"doctype": "Purchase Taxes and Charges Template",
				"title": "MSP Tax " + frappe.generate_hash(length=8),
				"company": self.TEST_COMPANY,
				"taxes": [
					{
						"charge_type": "On Net Total",
						"account_head": account,
						"description": "MSP test tax",
						"rate": 5,
					}
				],
			}
		).insert()
		mr = self.mr()
		data = self.data(mr, [(0, 0, 60), (0, 0, 40)])
		data["rows"][1].update(currency="CNY", taxes_and_charges=tax.name)
		preview = allocation.preview_allocation(mr.name, data)
		self.assertEqual(len(preview["groups"]), 2)
		pos = self.orders(mr, data, submit=False)
		self.assertEqual({po.currency for po in pos}, {"INR", "CNY"})
		foreign = next(po for po in pos if po.currency == "CNY")
		self.assertEqual(foreign.conversion_rate, 2)
		self.assertEqual(foreign.taxes_and_charges, tax.name)
		self.assertEqual(foreign.taxes[0].rate, 5)
		self.assertEqual(foreign.items[0].rate, 5)
