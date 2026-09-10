from unittest import TestCase
from unittest.mock import patch

import frappe

from process_simplification.production_reporting import worker_load


class TestWorkerLoad(TestCase):
	def summarize(self, assignments, reports=(), movements=None):
		with patch.object(worker_load, "job_card_qty_precision", return_value=6), patch(
			"process_simplification.production_reporting.assignment_movement.job_card_qty_precision", return_value=6
		):
			return worker_load.summarize_worker_loads(
				[frappe._dict(name="EMP-1", employee_name="张师傅")], assignments,
				reports, movements or {}, current_job_card="JOB-1",
			)["EMP-1"]

	def assignment(self, name, qty=10, job=None):
		return frappe._dict(name=name, employee="EMP-1", job_card=job or name, work_order="WO",
			assigned_qty=qty, operation="装配", item_name="产品", stock_uom="Nos")

	def test_active_paused_queued_and_review_are_separate_jobs(self):
		result = self.summarize(
			[self.assignment(name) for name in ["JOB-1", "JOB-2", "JOB-3", "JOB-4"]],
			[frappe._dict(assignment="JOB-1", active_count=1, paused_count=1),
			 frappe._dict(assignment="JOB-3", pending_qty=4),
			 frappe._dict(assignment="JOB-4", approved_qty=10)],
		)
		self.assertEqual((result.active_count, result.queued_count, result.pending_review_count), (1, 1, 1))
		self.assertTrue(result.jobs[0].paused)
		self.assertTrue(result.jobs[0].is_current)
		self.assertEqual(result.jobs[1].remaining_qty, 6)
		self.assertNotIn("JOB-4", [row.job_card for row in result.jobs])

	def test_released_and_redispatched_quantities_follow_effective_allocations(self):
		result = self.summarize(
			[self.assignment("RELEASED"), self.assignment("TRANSFER", qty=0)],
			movements={"RELEASED": frappe._dict(released_qty=10), "TRANSFER": frappe._dict(redispatched_qty=4.5)},
		)
		self.assertEqual(result.queued_count, 1)
		self.assertEqual(result.jobs[0].remaining_qty, 4.5)

	def test_pending_full_quantity_does_not_count_as_waiting_to_start(self):
		result = self.summarize([self.assignment("JOB")], [frappe._dict(assignment="JOB", pending_qty=10)])
		self.assertEqual((result.queued_count, result.pending_review_count), (0, 1))
		self.assertEqual(result.jobs[0].remaining_qty, 0)

	def test_distinct_job_count_and_unknown_employee_are_not_overcounted(self):
		rows = [self.assignment("A", job="JOB-1"), self.assignment("B", job="JOB-1")]
		rows.append(frappe._dict(name="OTHER", employee="EMP-2", assigned_qty=100))
		result = self.summarize(rows, [frappe._dict(assignment="B", active_count=1)])
		self.assertEqual((result.active_count, result.queued_count), (1, 0))
		self.assertEqual(result.jobs[0].remaining_qty, 20)

	def test_bulk_reader_uses_two_queries_and_one_movement_read(self):
		rows = [self.assignment(f"JOB-{index}") for index in range(20)]
		with patch.object(frappe.db, "sql", side_effect=[rows, []]) as sql, patch.object(
			worker_load, "movement_totals", return_value={}
		) as movement, patch.object(worker_load, "summarize_worker_loads", return_value={}) as summarize:
			worker_load.read_worker_loads("Company", [frappe._dict(name="EMP-1")])
		self.assertEqual(sql.call_count, 2)
		self.assertEqual(movement.call_count, 1)
		self.assertEqual(summarize.call_count, 1)

	def test_company_restriction_is_checked_before_any_load_is_read(self):
		from process_simplification.production_reporting import service
		with patch.object(service, "require_reviewer"), patch.object(
			service, "job_card_values", return_value=frappe._dict(company="Other Company")
		), patch.object(service, "_assert_supervisor_company", side_effect=frappe.PermissionError), patch.object(
			worker_load, "read_worker_loads"
		) as read:
			with self.assertRaises(frappe.PermissionError):
				worker_load.get_worker_loads("JOB", ["EMP-1"])
		read.assert_not_called()

	def test_worker_account_cannot_query_team_load(self):
		from process_simplification.production_reporting import service
		with patch.object(service, "require_reviewer", side_effect=frappe.PermissionError), patch.object(
			service, "job_card_values"
		) as job_card:
			with self.assertRaises(frappe.PermissionError):
				worker_load.get_worker_loads("JOB", ["EMP-1"])
		job_card.assert_not_called()

	def test_database_reader_excludes_final_documents_and_counts_live_work(self):
		# Rollback-only rows exercise the SQL joins without creating accounts,
		# submitting business documents, stock entries or dispatch notifications.
		prefix = "_Test Worker Load " + frappe.generate_hash(length=10)
		frappe.db.savepoint("worker_load_test")
		try:
			for suffix, wo_status, jc_status, assignment_status, report_status in [
				("active", "In Process", 0, "Active", "In Progress"),
				("queued", "Not Started", 0, "Active", None),
				("review", "In Process", 0, "Active", "Pending Approval"),
				("complete", "Completed", 1, "Completed", "Approved"),
				("closed", "Closed", 0, "Active", None),
				("stopped", "Stopped", 0, "Active", None),
				("cancelled", "Cancelled", 0, "Active", None),
				("submitted_job", "In Process", 1, "Active", None),
				("removed", "In Process", 0, "Cancelled", None),
				("closed_active", "Closed", 0, "Active", "In Progress"),
			]:
				name = prefix + suffix
				frappe.get_doc(dict(doctype="Work Order", name=name, company=prefix, docstatus=1, status=wo_status)).db_insert()
				frappe.get_doc(dict(doctype="Job Card", name=name, company=prefix, work_order=name, docstatus=jc_status)).db_insert()
				frappe.get_doc(dict(doctype="Job Card Worker Assignment", name=name, company=prefix,
					job_card=name, work_order=name, employee=prefix, assigned_qty=10, status=assignment_status)).db_insert()
				if report_status:
					frappe.get_doc(dict(doctype="Job Card Work Report", name=name, assignment=name,
						status=report_status, completed_qty=10, timer_paused_at=None)).db_insert()
			result = worker_load.read_worker_loads(prefix, [frappe._dict(name=prefix, employee_name="测试工人")])[prefix]
			self.assertEqual((result.active_count, result.queued_count, result.pending_review_count), (2, 1, 1))
			self.assertEqual({row.job_card for row in result.jobs}, {prefix + state for state in ["active", "queued", "review", "closed_active"]})
			self.assertTrue(next(row for row in result.jobs if row.job_card == prefix + "closed_active").requires_attention)
		finally:
			frappe.db.rollback(save_point="worker_load_test")
