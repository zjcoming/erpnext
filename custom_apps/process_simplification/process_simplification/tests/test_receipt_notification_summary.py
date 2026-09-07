import json
import unittest

import frappe

from process_simplification.purchasing.receipts import _notification_message


class TestReceiptNotificationSummary(unittest.TestCase):
	def event(self, count=10, kind="Arrival", requests=None, supplier="原料供应商"):
		return frappe._dict(
			event_type=kind, supplier=supplier, receipt="MAT-PRE-2026-00045",
			snapshot=json.dumps({
				"items": [{"item_code": f"item-{i}", "qty": 90000, "uom": "Gram"} for i in range(count)],
				"requests": [{"complete": False}] if requests is None else requests,
			}),
		)

	def test_large_receipt_stays_short_and_keeps_partial_status(self):
		subject, description = _notification_message(self.event(500))
		self.assertIn("本批到货 500 项物料", description)
		self.assertIn("尚未到齐", description)
		self.assertNotIn("90000", description)
		self.assertLess(len(subject + description), 120)
		self.assertEqual(description.count("<br>"), 1)

	def test_completion_requires_all_related_requests(self):
		for requests, expected in (([{"complete": True}], "已全部到货"), ([{"complete": True}, {"complete": False}], "尚未到齐")):
			self.assertIn(expected, _notification_message(self.event(requests=requests))[1])
		self.assertNotIn("全部到货", _notification_message(self.event(requests=[]))[1])

	def test_returns_and_cancellations_are_corrections_not_arrivals(self):
		for kind in ("Return", "Receipt Cancelled", "Return Cancelled"):
			subject, description = _notification_message(self.event(kind=kind))
			self.assertNotIn("本批到货", description)
			self.assertNotIn("全部到货", description)
			self.assertIn("核对", description)

	def test_duplicate_material_lines_count_once_without_mixing_units(self):
		event = self.event()
		event.snapshot = json.dumps({"items": [{"item_code": "A", "qty": 60, "uom": "Box"}, {"item_code": "A", "qty": 40, "uom": "Nos"}], "requests": []})
		self.assertIn("1 项物料", _notification_message(event)[1])
		self.assertNotIn("100", _notification_message(event)[1])

	def test_rejected_goods_remain_visible_in_short_summary(self):
		event = self.event()
		snapshot = json.loads(event.snapshot)
		snapshot["items"][0]["rejected_qty"] = 5
		event.snapshot = json.dumps(snapshot)
		self.assertIn("含拒收", _notification_message(event)[1])

	def test_long_names_are_bounded_and_description_is_escaped(self):
		event = self.event(supplier="很长的供应商" * 40)
		event.receipt = "<script>" * 40
		subject, description = _notification_message(event)
		self.assertLess(len(subject), 40)
		self.assertIn("…", subject)
		self.assertNotIn("<script>", description)
		self.assertIn("&lt;script&gt;", description)
