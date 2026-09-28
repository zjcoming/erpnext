"use strict";

function reportFilterHasValue(filter) {
	const value = filter.get_value();
	if (filter.df.fieldtype === "Check") return Boolean(Number(value));
	if (Array.isArray(value)) return value.length > 0;
	return value !== null && value !== undefined && String(value).trim() !== "";
}

function classifyMobileReportFilters(filters) {
	const visible = filters.filter((filter) => !filter.df.hidden && filter.wrapper.style.display !== "none");
	const required = visible.filter((filter) => filter.df.reqd);
	const basic = new Set(required.length ? required : visible.slice(0, 3));
	return filters.map((filter) => ({
		filter,
		extra: visible.includes(filter) && !basic.has(filter) && !reportFilterHasValue(filter),
		wide: filter.df.fieldname === "company" || ["Check", "MultiSelectList"].includes(filter.df.fieldtype),
	}));
}

if (typeof module !== "undefined" && module.exports) {
	module.exports = { reportFilterHasValue, classifyMobileReportFilters };
}

if (typeof frappe !== "undefined" && typeof document !== "undefined") {
	let currentForm, observer, timer, reportName, expanded = false;
	const schedule = () => {
		clearTimeout(timer);
		timer = setTimeout(refresh, 0);
	};

	function refresh() {
		if (frappe.get_route()?.[0] !== "query-report") return;
		const report = frappe.query_report;
		const form = report?.page?.page_form?.[0];
		if (!form || !report.filters) return;
		// Reports with a native collapse control keep ownership of their visibility.
		if (report.report_settings?.collapsible_filters) {
			form.classList.remove("ps-report-filters", "ps-report-filters-collapsed");
			form.querySelector(".ps-report-filter-toggle")?.remove();
			return;
		}
		if (currentForm !== form) {
			observer?.disconnect();
			currentForm?.removeEventListener("change", schedule);
			currentForm = form;
			form.addEventListener("change", schedule);
			observer = new MutationObserver(schedule);
			observer.observe(form, { childList: true });
		}
		if (reportName !== report.report_name) {
			reportName = report.report_name;
			expanded = false;
		}
		const fields = classifyMobileReportFilters(report.filters);
		const extraCount = fields.filter(({ extra }) => extra).length;
		form.classList.add("ps-report-filters");
		form.classList.toggle("ps-report-filters-collapsed", !expanded);
		for (const { filter, extra, wide } of fields) {
			filter.wrapper.classList.toggle("ps-report-filter-extra", extra);
			filter.wrapper.classList.toggle("ps-report-filter-wide", wide);
			filter.wrapper.setAttribute("data-ps-filter-label", __(filter.df.label || ""));
		}
		let button = form.querySelector(".ps-report-filter-toggle");
		if (!button) {
			button = document.createElement("button");
			button.type = "button";
			button.className = "btn btn-default ps-report-filter-toggle";
			form.id ||= "ps-query-report-filters";
			button.setAttribute("aria-controls", form.id);
			button.addEventListener("click", () => {
				expanded = !expanded;
				refresh();
			});
			form.appendChild(button);
		}
		button.hidden = !extraCount && !expanded;
		button.setAttribute("aria-expanded", String(expanded));
		button.textContent = expanded ? __("收起筛选") : __("更多筛选（{0}）", [extraCount]);
	}

	$(document).on("app_ready.ps_mobile page-change.ps_mobile ajaxComplete.ps_mobile", schedule);
	frappe.router.on("change", schedule);
	$(schedule);
}
