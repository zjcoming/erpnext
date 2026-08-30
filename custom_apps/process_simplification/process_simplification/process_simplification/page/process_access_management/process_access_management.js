class ProcessSimplificationAccessManagement {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("流程简化权限管理"),
			single_column: true,
		});
		this.make_user_field();
		this.make_body();
		this.page.set_primary_action(__("保存权限"), () => this.save(), "check");
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
					<div><span>APP ACCESS</span><h2>${__("按岗位分配流程简化权限")}</h2></div>
					<p>${__("岗位会获得完成简化流程所需的精确单据权限，不删除用户已有的 ERPNext 角色。公司和仓库范围仍通过 User Permission 限制。")}</p>
				</section>
				<div class="ps-access-placeholder" data-placeholder>${__("请先选择一个系统用户")}</div>
				<section class="ps-access-content hide" data-content>
					<div class="ps-access-user" data-user></div>
					<div class="ps-access-role-grid" data-role-grid></div>
					<div class="ps-access-security-note">${__("安全说明：只有系统管理员可以授予或移除老板、工资核算、APP 权限管理员三个敏感角色。Administrator 始终保留技术超级用户能力。")}</div>
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

	render() {
		const data = this.current;
		this.root.find("[data-placeholder]").addClass("hide");
		this.root.find("[data-content]").removeClass("hide");
		this.root.find("[data-user]").html(`${frappe.avatar(data.user.name, "avatar-medium")}
			<div><strong>${frappe.utils.escape_html(data.user.full_name || data.user.name)}</strong><span>${frappe.utils.escape_html(data.user.name)}</span></div>`);
		this.root.find("[data-role-grid]").html((data.roles || []).map((role) => {
			const disabled = role.sensitive && !data.can_manage_sensitive;
			return `<label class="ps-access-role ${disabled ? "ps-access-role-disabled" : ""}">
				<input type="checkbox" data-role="${frappe.utils.escape_html(role.role)}" ${role.assigned ? "checked" : ""} ${disabled ? "disabled" : ""}>
				<div><span>${frappe.utils.escape_html(role.label)}</span><strong>${frappe.utils.escape_html(role.role)}</strong><p>${frappe.utils.escape_html(role.description)}</p></div>
				<i>${role.sensitive ? __("敏感角色") : __("普通岗位")}</i>
			</label>`;
		}).join(""));
		this.page.btn_primary.prop("disabled", false);
	}

	async save() {
		if (!this.current) return;
		const roles = this.root.find("[data-role]:checked").map((_, input) => input.dataset.role).get();
		await frappe.call({
			method: "process_simplification.api.access_management.set_user_access",
			type: "POST",
			freeze: true,
			freeze_message: __("正在保存 APP 权限…"),
			args: { user: this.current.user.name, roles },
		});
		frappe.show_alert({ message: __("流程简化权限已更新"), indicator: "green" });
		await this.load_user();
	}
}

frappe.pages["process-access-management"].on_page_load = (wrapper) => {
	wrapper.process_access_management = new ProcessSimplificationAccessManagement(wrapper);
};
