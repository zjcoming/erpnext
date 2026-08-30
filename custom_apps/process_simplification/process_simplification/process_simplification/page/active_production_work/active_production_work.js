const activeProductionWorkApi =
	typeof module !== "undefined" && module.exports
		? require("../../../public/js/worker_reporting.js")
		: window.process_simplification.worker_reporting;

if (typeof module !== "undefined" && module.exports) module.exports = activeProductionWorkApi;

if (typeof frappe !== "undefined") {
	frappe.pages["active-production-work"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("正在做"),
			single_column: true,
		});
		page.main.html(`
			<div class="process-simplification-page worker-reporting-page worker-active-page">
				<div class="worker-reporting-summary"></div>
				<section>
					<h4>${__("正在做的任务")}</h4>
					<div class="worker-active-list"></div>
				</section>
			</div>`);
		activeProductionWorkApi.mountWorkerReportingPage({
			page,
			root: page.main.find(".worker-reporting-page"),
			mode: "active",
		});
	};

	frappe.pages["active-production-work"].refresh = function (wrapper) {
		return wrapper.page?.worker_reporting?.load?.();
	};
}
