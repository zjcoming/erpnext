"""Keep the factory workspace and native page sidebar identity in agreement."""


def repair_workspace_sidebar(doc, method=None):
	if doc.name != "process-simplification":
		return
	from process_simplification.patches.v0_0.add_worker_reporting_navigation import (
		_ensure_native_sidebar_identity,
	)

	if sidebar := _ensure_native_sidebar_identity():
		sidebar.save(ignore_permissions=True)
