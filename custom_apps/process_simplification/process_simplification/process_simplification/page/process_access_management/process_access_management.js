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
					<div><span>${__("岗位与范围")}</span><h2>${__("一个入口管理岗位与数据范围")}</h2></div>
					<p>${__("为员工分配岗位和可操作的公司、仓库，权限随岗位自动配置。")}</p>
				</section>
				<div class="ps-access-placeholder" data-placeholder>${__("请先选择一个系统用户")}</div>
				<section class="ps-access-content hide" data-content>
					<div class="ps-access-user" data-user></div>
					<div class="ps-access-section-heading"><div><h3>${__("业务岗位")}</h3><p>${__("可组合普通岗位；流水线工人必须独占。")}</p></div><span>${__("岗位权限自动同步")}</span></div>
					<div class="ps-access-role-grid" data-role-grid></div>
					<div class="ps-access-section-heading ps-access-scope-heading"><div><h3>${__("数据范围")}</h3><p>${__("岗位决定能做什么，数据范围决定能操作哪些公司和仓库。")}</p></div></div>
					<div class="ps-access-scope-grid">
						<section class="ps-access-scope-card"><h4>${__("公司")}</h4><div class="ps-access-option-list" data-company-list></div></section>
						<section class="ps-access-scope-card"><h4>${__("仓库与仓库组")}</h4><p>${__("勾选仓库组后，包含组内现有和以后新增的子仓库。只负责某个仓库时，可单独勾选。")}</p><div class="ps-access-option-list" data-warehouse-list></div></section>
						<section class="ps-access-scope-card"><h4>${__("关联员工")}</h4><p>${__("工人岗位必须关联一个在职员工。已经绑定后不能在此页面换绑。")}</p><select class="form-control" data-employee></select></section>
					</div>
					<section class="ps-access-effective"><h4>${__("当前岗位将获得")}</h4><div data-capabilities></div></section>
					<div class="ps-access-retained hide" data-retained></div>
					<div class="ps-access-security-note">${__("安全说明：只有系统管理员可以授予或移除老板、工资核算、APP 权限管理员。其他 ERPNext 岗位模板会被保留，但不在此页面中编辑。")}</div>
				</section>
			</div>`
		).appendTo(this.page.main);
	}

	async load_user({ force = false } = {}) {
		const user = this.user_field.get_value();
		// A toolbar Link can emit change again on blur after autocomplete selection.
		// Keep that duplicate event from replacing unsaved scope selections.
		if (!force && user && (this.current?.user.name === user || this.loading_user === user)) return;

		const request_id = this.user_load_request_id = (this.user_load_request_id || 0) + 1;
		this.loading_user = user || null;
		this.current = null;
		this.page.btn_primary.prop("disabled", true);
		this.root.find("[data-content]").addClass("hide");
		this.root.find("[data-placeholder]").removeClass("hide")
			.text(user ? __("正在加载用户岗位与数据范围…") : __("请先选择一个系统用户"));
		if (!user) return;

		try {
			const response = await frappe.call({
				method: "process_simplification.api.access_management.get_user_access",
				args: { user },
			});
			if (request_id !== this.user_load_request_id || this.user_field.get_value() !== user) return;
			this.current = response.message;
			this.render();
		} finally {
			if (request_id === this.user_load_request_id) this.loading_user = null;
		}
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
		this.warehouse_selection = new Set(data.warehouses || []);
		this.warehouse_hide_descendants = new Set(data.warehouse_hide_descendants || []);
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
		const warehouses = this.current.scope_options?.warehouses || [];
		const rows = this.warehouse_scope_rows(warehouses);
		this.root.find("[data-warehouse-list]").html(rows.map(({ warehouse, inherited, depth }) => {
			const selected = this.warehouse_selection.has(warehouse.name);
			const exact_group = warehouse.is_group && selected && this.warehouse_hide_descendants.has(warehouse.name);
			const scope = warehouse.disabled && !inherited ? __("已停用，请取消此授权后保存") : inherited
				? `${__("继承自")} ${inherited.warehouse_name || inherited.name}`
				: warehouse.is_group ? (exact_group ? __("仅组本身") : __("包含全部子仓库，新增自动纳入")) : "";
			return `<div class="ps-access-warehouse ${inherited ? "ps-access-warehouse-inherited" : ""}" data-warehouse-company="${this.escape(warehouse.company)}" style="--warehouse-depth:${Math.min(depth, 3)}">
				<label><input type="checkbox" data-warehouse-scope="${this.escape(warehouse.name)}" ${selected || inherited ? "checked" : ""} ${inherited || (warehouse.disabled && !selected) ? "disabled" : ""}><span>${this.escape(warehouse.warehouse_name || warehouse.name)}${warehouse.is_group ? `<b class="ps-access-group-badge">${__("仓库组")}</b>` : ""}<small>${this.escape(warehouse.name)} · ${this.escape(warehouse.company)}</small>${scope ? `<small class="ps-access-warehouse-scope">${this.escape(scope)}</small>` : ""}</span></label>
				${exact_group && !warehouse.disabled ? `<button type="button" class="btn btn-xs btn-default" data-include-descendants="${this.escape(warehouse.name)}">${__("改为包含子仓库")}</button>` : ""}
			</div>`;
		}).join("") || `<em>${__("没有可用仓库")}</em>`);
		this.filter_warehouses(false);
	}

	warehouse_scope_rows(warehouses) {
		const selected = this.warehouse_selection;
		const exact = this.warehouse_hide_descendants;
		return warehouses.map((warehouse) => {
			const ancestors = warehouses.filter((group) => group.is_group && group.company === warehouse.company
				&& group.lft < warehouse.lft && warehouse.rgt < group.rgt);
			const inherited = ancestors.find((group) => selected.has(group.name) && !exact.has(group.name));
			if (inherited) {
				// Removing a group later should remove its inherited grants as well.
				selected.delete(warehouse.name);
				exact.delete(warehouse.name);
			}
			return { warehouse, inherited, depth: ancestors.length };
		});
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
		this.root.find("[data-warehouse-list]").off("change.ps-access click.ps-access")
			.on("change.ps-access", "[data-warehouse-scope]", (event) => {
				const input = event.currentTarget;
				if (input.checked) this.warehouse_selection.add(input.dataset.warehouseScope);
				else {
					this.warehouse_selection.delete(input.dataset.warehouseScope);
					this.warehouse_hide_descendants.delete(input.dataset.warehouseScope);
				}
				this.render_warehouses();
			})
			.on("click.ps-access", "[data-include-descendants]", (event) => {
				this.warehouse_hide_descendants.delete(event.currentTarget.dataset.includeDescendants);
				this.render_warehouses();
			});
	}

	filter_warehouses(clear_hidden) {
		const companies = new Set(this.checked_values("company-scope"));
		if (clear_hidden) {
			for (const warehouse of this.current.scope_options?.warehouses || []) {
				if (!companies.has(warehouse.company)) {
					this.warehouse_selection.delete(warehouse.name);
					this.warehouse_hide_descendants.delete(warehouse.name);
				}
			}
			this.render_warehouses();
			return;
		}
		this.root.find("[data-warehouse-company]").each((_, row) => {
			const element = $(row);
			const visible = companies.has(element.attr("data-warehouse-company"));
			element.toggleClass("hide", !visible);
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
		if (!this.current || this.current.user.name !== this.user_field.get_value()) return;
		const user = this.current.user.name;
		const employee = this.root.find("[data-employee]").val() || this.current.employee?.name || null;
		await frappe.call({
			method: "process_simplification.api.access_management.set_user_access",
			type: "POST",
			freeze: true,
			freeze_message: __("正在保存岗位与数据范围…"),
			args: {
				user,
				profiles: this.checked_values("profile"),
				companies: this.checked_values("company-scope"),
				warehouses: Array.from(this.warehouse_selection || []),
				warehouse_hide_descendants: Array.from(this.warehouse_hide_descendants || []),
				employee,
			},
		});
		frappe.show_alert({ message: __("岗位与数据范围已更新"), indicator: "green" });
		if (this.user_field.get_value() === user) await this.load_user({ force: true });
	}
}

frappe.pages["process-access-management"].on_page_load = (wrapper) => {
	wrapper.process_access_management = new ProcessSimplificationAccessManagement(wrapper);
};
