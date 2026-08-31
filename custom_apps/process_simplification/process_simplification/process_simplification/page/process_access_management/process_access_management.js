class ProcessSimplificationAccessManagement {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("流程简化岗位与数据范围"),
			single_column: true,
		});
		this.make_user_field();
		this.make_body();
		this.page.set_primary_action(__("保存岗位与范围"), () => this.save(), "check");
		this.page.btn_primary.prop("disabled", true);
	}

	make_user_field() {
		this.user_field = this.page.add_field({
			fieldname: "user",
			label: __("用户"),
			fieldtype: "Link",
			options: "User",
			get_query: () => ({
				query: "process_simplification.api.access_management.search_users",
			}),
			change: () => this.load_user(),
		});
	}

	make_body() {
		this.page.main.addClass("process-simplification-page ps-access-page");
		this.root = $(
			`<div class="ps-access-management">
				<section class="ps-access-hero">
					<div><span>JOB ACCESS</span><h2>${__("一个入口管理岗位与数据范围")}</h2></div>
					<p>${__("这里分配固定岗位模板和公司、仓库范围。实际角色由 ERPNext Role Profile 自动同步，不再直接维护两套角色。")}</p>
				</section>
				<div class="ps-access-placeholder" data-placeholder>${__("请先选择一个系统用户")}</div>
				<section class="ps-access-content hide" data-content>
					<div class="ps-access-user" data-user></div>
					<div class="ps-access-section-heading"><div><h3>${__("业务岗位")}</h3><p>${__("可组合普通岗位；流水线工人必须独占。")}</p></div><span>${__("底层来源：Role Profile")}</span></div>
					<div class="ps-access-role-grid" data-role-grid></div>
					<div class="ps-access-section-heading ps-access-scope-heading"><div><h3>${__("数据范围")}</h3><p>${__("岗位决定能做什么，数据范围决定能操作哪些公司和仓库。")}</p></div></div>
					<div class="ps-access-scope-grid">
						<section class="ps-access-scope-card"><h4>${__("公司")}</h4><div class="ps-access-option-list" data-company-list></div></section>
						<section class="ps-access-scope-card"><h4>${__("仓库")}</h4><div class="ps-access-option-list" data-warehouse-list></div></section>
						<section class="ps-access-scope-card"><h4>${__("关联员工")}</h4><p>${__("工人岗位必须关联一个在职员工。已经绑定后不能在此页面换绑。")}</p><select class="form-control" data-employee></select></section>
					</div>
					<section class="ps-access-effective"><h4>${__("当前岗位将获得")}</h4><div data-capabilities></div></section>
					<div class="ps-access-retained hide" data-retained></div>
					<div class="ps-access-security-note">${__("安全说明：只有系统管理员可以授予或移除老板、工资核算、APP 权限管理员。其他 ERPNext 岗位模板会被保留，但不在此页面中编辑。")}</div>
				</section>
			</div>`
		).appendTo(this.page.main);
	}

	async load_user() {
		const user = this.user_field.get_value();
		this.current = null;
		this.page.btn_primary.prop("disabled", true);
		if (!user) {
			this.root.find("[data-placeholder]").removeClass("hide");
			this.root.find("[data-content]").addClass("hide");
			return;
		}
		const response = await frappe.call({
			method: "process_simplification.api.access_management.get_user_access",
			args: { user },
		});
		this.current = response.message;
		this.render();
	}

	escape(value) {
		return frappe.utils.escape_html(value || "");
	}

	render() {
		const data = this.current;
		this.root.find("[data-placeholder]").addClass("hide");
		this.root.find("[data-content]").removeClass("hide");
		this.root.find("[data-user]").html(`${frappe.avatar(data.user.name, "avatar-medium")}
			<div><strong>${this.escape(data.user.full_name || data.user.name)}</strong><span>${this.escape(data.user.name)}</span></div>`);
		this.root.find("[data-role-grid]").html((data.roles || []).map((role) => {
			const disabled = role.sensitive && !data.can_manage_sensitive;
			const abilities = (role.capability_labels || []).map((label) => this.escape(label)).join("、");
			return `<label class="ps-access-role ${disabled ? "ps-access-role-disabled" : ""}">
				<input type="checkbox" data-profile="${this.escape(role.profile)}" data-role="${this.escape(role.role)}" ${role.assigned ? "checked" : ""} ${disabled ? "disabled" : ""}>
				<div><span>${this.escape(role.label)}</span><strong>${this.escape(role.profile)}</strong><p>${this.escape(role.description)}</p><small>${abilities}</small></div>
				<i>${role.sensitive ? __("敏感岗位") : __("普通岗位")}</i>
			</label>`;
		}).join(""));

		this.render_companies();
		this.render_warehouses();
		this.render_employee();
		this.render_retained_profiles();
		this.bind_changes();
		this.render_capabilities();
		this.page.btn_primary.prop("disabled", false);
	}

	render_companies() {
		const selected = new Set(this.current.companies || []);
		const companies = this.current.scope_options?.companies || [];
		this.root.find("[data-company-list]").html(companies.map((company) => `
			<label><input type="checkbox" data-company-scope="${this.escape(company)}" ${selected.has(company) ? "checked" : ""}><span>${this.escape(company)}</span></label>`).join("") || `<em>${__("没有可用公司")}</em>`);
	}

	render_warehouses() {
		const selected = new Set(this.current.warehouses || []);
		const warehouses = this.current.scope_options?.warehouses || [];
		this.root.find("[data-warehouse-list]").html(warehouses.map((warehouse) => `
			<label data-warehouse-company="${this.escape(warehouse.company)}"><input type="checkbox" data-warehouse-scope="${this.escape(warehouse.name)}" ${selected.has(warehouse.name) ? "checked" : ""}><span>${this.escape(warehouse.warehouse_name || warehouse.name)}<small>${this.escape(warehouse.name)} · ${this.escape(warehouse.company)}</small></span></label>`).join("") || `<em>${__("没有可用仓库")}</em>`);
		this.filter_warehouses(false);
	}

	render_employee() {
		const current = this.current.employee;
		const options = this.current.scope_options?.employees || [];
		const select = this.root.find("[data-employee]");
		select.html(`<option value="">${__("不关联员工")}</option>${options.map((employee) => `<option value="${this.escape(employee.name)}" ${current?.name === employee.name ? "selected" : ""}>${this.escape(employee.employee_name || employee.name)} · ${this.escape(employee.name)} · ${this.escape(employee.company)}</option>`).join("")}`);
		select.prop("disabled", Boolean(current));
	}

	render_retained_profiles() {
		const retained = this.current.retained_profiles || [];
		const box = this.root.find("[data-retained]");
		box.toggleClass("hide", !retained.length);
		box.html(retained.length ? `${__("保留的其他 ERPNext 岗位模板：")}<strong>${retained.map((profile) => this.escape(profile)).join("、")}</strong>` : "");
	}

	bind_changes() {
		this.root.find("[data-role]").off("change.ps-access").on("change.ps-access", (event) => {
			const input = $(event.currentTarget);
			if (input.data("role") === "Production Worker" && input.prop("checked")) {
				this.root.find("[data-role]").not(input).not(":disabled").prop("checked", false);
			} else if (input.prop("checked")) {
				this.root.find('[data-role="Production Worker"]').prop("checked", false);
			}
			this.render_capabilities();
		});
		this.root.find("[data-company-scope]").off("change.ps-access").on("change.ps-access", () => this.filter_warehouses(true));
	}

	filter_warehouses(clear_hidden) {
		const companies = new Set(this.checked_values("company-scope"));
		this.root.find("[data-warehouse-company]").each((_, row) => {
			const element = $(row);
			const visible = companies.has(element.attr("data-warehouse-company"));
			element.toggleClass("hide", !visible);
			if (!visible && clear_hidden) element.find("input").prop("checked", false);
		});
	}

	render_capabilities() {
		const selectedProfiles = new Set(this.checked_values("profile"));
		const capabilities = [];
		for (const role of this.current.roles || []) {
			if (!selectedProfiles.has(role.profile)) continue;
			for (const label of role.capability_labels || []) {
				if (!capabilities.includes(label)) capabilities.push(label);
			}
		}
		this.root.find("[data-capabilities]").html(capabilities.map((label) => `<span>${this.escape(label)}</span>`).join("") || `<em>${__("未选择流程简化岗位")}</em>`);
	}

	checked_values(attribute) {
		return this.root.find(`[data-${attribute}]:checked`).map((_, input) => input.dataset[attribute.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())]).get();
	}

	async save() {
		if (!this.current) return;
		const employee = this.root.find("[data-employee]").val() || this.current.employee?.name || null;
		await frappe.call({
			method: "process_simplification.api.access_management.set_user_access",
			type: "POST",
			freeze: true,
			freeze_message: __("正在保存岗位与数据范围…"),
			args: {
				user: this.current.user.name,
				profiles: this.checked_values("profile"),
				companies: this.checked_values("company-scope"),
				warehouses: this.checked_values("warehouse-scope"),
				employee,
			},
		});
		frappe.show_alert({ message: __("岗位与数据范围已更新"), indicator: "green" });
		await this.load_user();
	}
}

frappe.pages["process-access-management"].on_page_load = (wrapper) => {
	wrapper.process_access_management = new ProcessSimplificationAccessManagement(wrapper);
};
