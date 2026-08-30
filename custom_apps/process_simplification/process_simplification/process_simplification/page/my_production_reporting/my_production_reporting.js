const myProductionReportingApi =
	typeof module !== "undefined" && module.exports
		? require("../../../public/js/worker_reporting.js")
		: window.process_simplification.worker_reporting;

if (typeof module !== "undefined" && module.exports) module.exports = myProductionReportingApi;

if (typeof frappe !== "undefined") {
	frappe.pages["my-production-reporting"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("我的报工"),
			single_column: true,
		});
		page.main.html(`
			<div class="process-simplification-page worker-reporting-page worker-queue-page">
				<div class="worker-reporting-summary"></div>
				<div class="worker-active-shortcut"></div>
				<section class="worker-current-assignments">
					<h4>${__("当前派工")}</h4>
					<div class="worker-assignment-list"></div>
				</section>
			</div>`);
		myProductionReportingApi.mountWorkerReportingPage({
			page,
			root: page.main.find(".worker-reporting-page"),
			mode: "queue",
		});
	};

	frappe.pages["my-production-reporting"].refresh = function (wrapper) {
		return wrapper.page?.worker_reporting?.load?.();
	};
}
