frappe.pages["document-scan"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("扫一扫"), single_column: true });
	page.document_scan = window.process_simplification.document_scan.mountScanPage(page);
};

frappe.pages["document-scan"].refresh = function (wrapper) {
	wrapper.page?.document_scan?.refresh();
};
