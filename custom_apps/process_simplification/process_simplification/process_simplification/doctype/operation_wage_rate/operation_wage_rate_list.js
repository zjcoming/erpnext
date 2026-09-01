frappe.listview_settings["Operation Wage Rate"] = {
	add_fields: [
		"operation",
		"company",
		"enable_piecework",
		"piecework_rate",
		"enable_time",
		"hourly_rate",
		"valid_from",
		"valid_to",
		"enabled",
	],
	get_indicator(doc) {
		if (!doc.enabled) return [__("已停用"), "gray", "enabled,=,0"];
		const today = frappe.datetime.get_today();
		if (doc.valid_from && doc.valid_from > today) {
			return [__("待生效"), "orange", `valid_from,>,${today}`];
		}
		if (doc.valid_to && doc.valid_to < today) {
			return [__("已失效"), "gray", `valid_to,<,${today}`];
		}
		const modes = [
			doc.enable_piecework ? __("计件") : "",
			doc.enable_time ? __("计时") : "",
		].filter(Boolean);
		return [modes.join(" + ") || __("待配置"), modes.length ? "green" : "orange", "enabled,=,1"];
	},
	onload(listview) {
		listview.page.add_inner_button(__("工资汇总"), () => {
			frappe.set_route("List", "Monthly Worker Wage Summary");
		});
	},
};
