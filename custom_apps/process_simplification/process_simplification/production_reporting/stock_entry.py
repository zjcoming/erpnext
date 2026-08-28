from __future__ import annotations


def before_submit(doc, method=None):
	# Finished-goods posting is the only native path that can make a Work Order
	# Completed. Do not let it strand a Draft Job Card with immutable wage history.
	if doc.get("purpose") != "Manufacture" or not doc.get("work_order"):
		return
	from process_simplification.production_reporting.work_order import (
		assert_no_managed_draft_job_cards,
	)

	assert_no_managed_draft_job_cards(doc.work_order)
