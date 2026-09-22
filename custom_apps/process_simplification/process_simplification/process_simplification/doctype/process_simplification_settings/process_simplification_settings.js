frappe.ui.form.on("Process Simplification Settings", {
	refresh(frm) {
		if (!psCanConfigurePWA()) return;
		frm.add_custom_button("重新检查", () => psRefreshInitialization(frm), "开用前检查");
		frm.add_custom_button("选择公司", () => frappe.prompt({
			fieldname: "company", fieldtype: "Link", options: "Company", label: "公司", reqd: 1,
			default: frm.__psInitializationCompany,
		}, ({ company }) => {
			frm.__psInitializationCompany = company;
			psRefreshInitialization(frm);
		}, "检查哪家公司", "检查"), "开用前检查");
		psResetInitialization(frm);
		frm.add_custom_button("检查安装条件", () => psRefreshPWAStatus(frm), "手机应用");
		frm.add_custom_button("查看安装引导", () => {
			if (frm.is_dirty()) return frappe.msgprint("请先保存设置，再查看安装引导。");
			window.process_simplification?.pwa?.install();
		}, "手机应用");
		psRefreshPWAStatus(frm);
	},
	after_save(frm) {
		if (psCanConfigurePWA()) {
			psRefreshPWAStatus(frm);
			psResetInitialization(frm);
		}
	},
	setup(frm) {
		frm.set_query("user", "notification_recipients", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return {
				query: "process_simplification.process_simplification.doctype.process_simplification_settings.process_simplification_settings.search_notification_users",
				filters: {
					company: row.company,
					responsibility: row.responsibility,
				},
			};
		});
		frm.set_query("role_profile", "notification_role_recipients", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return {
				query: "process_simplification.process_simplification.doctype.process_simplification_settings.process_simplification_settings.search_notification_role_profiles",
				filters: { responsibility: row.responsibility },
			};
		});
	},
});

function psResetInitialization(frm) {
	// A refresh/save invalidates both an old result and any in-flight manual check.
	frm.__psInitializationRequest = (frm.__psInitializationRequest || 0) + 1;
	frm.fields_dict.initialization_status?.$wrapper.html('<p class="text-muted">首次启用或调整基础配置后，可点击上方“开用前检查 → 重新检查”。检查按需运行，不影响使用本页设置。</p>');
}

function psInitializationHtml(status, escape) {
	const errors = (status.checks || []).filter((row) => row.status === "error").length;
	const warnings = (status.checks || []).filter((row) => row.status === "warning").length;
	const heading = errors ? `基础配置还有 ${errors} 项需要处理` : warnings ? `基础配置还有 ${warnings} 项需要核对` : "基础配置检查通过，继续核对资料并试跑第一单";
	const badge = { error: "需要处理", warning: "需要核对", ok: "已核对", info: "按需设置" };
	const link = (row) => `${row.can_open === false ? "" : `<button type="button" class="btn btn-default btn-xs" data-setup-doctype="${escape(row.doctype)}" data-setup-name="${escape(row.name || "")}">${row.can_configure === false ? "查看" : "打开"}${escape(row.label)}</button>`}${row.can_configure === false ? '<span class="text-muted ml-2">如需配置，请系统管理员处理。</span>' : ""}`;
	return `<div class="ps-initialization-status">
		<p><strong>${escape(heading)}</strong></p>
		<p>当前公司：${escape(status.company || "未选择")}。这里只检查配置，不会自动更改设置或新增资料。</p>
		${(status.checks || []).map((row) => `<div class="mb-3"><strong>${escape(row.label)} · ${escape(badge[row.status] || "待核对")}</strong><p class="mb-1">${escape(row.value || "")} · ${escape(row.detail || "")}</p>${link(row)}</div>`).join("")}
		<hr><p><strong>接下来按顺序准备</strong></p>
		${(status.next_steps || []).map((row) => `<div class="mb-3"><strong>${escape(row.label)}</strong><p class="mb-1">${escape(row.detail)}</p>${link(row)}</div>`).join("")}
		<p class="text-muted">最后用销售、采购、库房、主管和工人账号完成一张小订单，核对收货、领料、报工、入库、发货及通知。基础配置检查通过不代表这些业务已验收。</p>
	</div>`;
}

async function psRefreshInitialization(frm) {
	const wrapper = frm.fields_dict.initialization_status?.$wrapper;
	if (!wrapper) return;
	const request = (frm.__psInitializationRequest || 0) + 1;
	frm.__psInitializationRequest = request;
	wrapper.html('<p class="text-muted">正在读取开用前配置…</p>');
	try {
		const { message } = await frappe.call({
			method: "process_simplification.initialization.get_status", type: "GET",
			args: { company: frm.__psInitializationCompany || null },
		});
		if (request !== frm.__psInitializationRequest) return;
		frm.__psInitializationCompany = message.company;
		wrapper.html(psInitializationHtml(message, frappe.utils.escape_html));
		wrapper.off("click.ps-init").on("click.ps-init", "[data-setup-doctype]", (event) => {
			const { setupDoctype, setupName } = event.currentTarget.dataset;
			frappe.set_route(setupName ? ["Form", setupDoctype, setupName] : ["List", setupDoctype]);
		});
	} catch (_) {
		if (request !== frm.__psInitializationRequest) return;
		wrapper.html('<p class="text-danger">未能读取开用前配置，尚不能确认配置是否完整。请使用上方“开用前检查 → 重新检查”重试。</p>');
	}
}

function psCanConfigurePWA() {
	return frappe.session.user === "Administrator" ||
		["Process Simplification Owner", "System Manager"].some((role) => frappe.user.has_role(role));
}

async function psRefreshPWAStatus(frm) {
	const wrapper = frm.fields_dict.pwa_status?.$wrapper;
	if (!wrapper) return;
	if (frm.is_dirty()) return frappe.msgprint("请先保存设置，再检查安装条件。");
	const escape = frappe.utils.escape_html;
	wrapper.html('<div class="process-pwa-status text-muted">正在检查当前浏览器的安装条件…</div>');
	try {
		const { message } = await frappe.call({ method: "process_simplification.pwa.get_status", type: "GET" });
		const pwa = window.process_simplification?.pwa;
		const state = pwa ? await pwa.configure(message.config) : { state: "missing" };
		const descriptions = {
			ready: "安装服务已就绪。Android 按浏览器确认安装，iPhone 按添加到主屏幕引导操作。",
			insecure: "当前为 HTTP，手机需要通过有效证书的 HTTPS 网址访问后安装。",
			unsupported: "当前浏览器不支持安装服务，请使用新版 Safari、Chrome 或 Edge 检查。",
			disabled: "已关闭安装入口与主动提示。已安装的桌面图标需由用户自行移除。",
			conflict: "检测到作用范围重叠的其他应用，已停止注册以避免覆盖。请联系管理员检查。",
			"manifest-conflict": "当前页面已有其他应用的安装清单，已保留原配置。请联系管理员检查。",
			error: "安装服务加载失败，请检查 HTTPS 证书、访问权限与服务器响应后重试。",
			missing: "前端资源尚未更新，请刷新页面；仍未恢复时请管理员更新资源。",
		};
		const secure = window.isSecureContext ?
			(window.location.protocol === "https:" ? "HTTPS 安全访问" : "本机调试安全环境（不代表手机的局域网 HTTP 可安装）") : "未建立安全访问";
		let endpointCheck = "";
		if (message.config.enabled) {
			try {
				const response = await fetch(message.config.manifest_url, { cache: "no-store", credentials: "same-origin" });
				const manifest = await response.json();
				if (!response.ok || manifest.id !== message.config.id || manifest.scope !== message.config.scope) throw new Error("manifest");
				endpointCheck = "安装信息读取正常。";
			} catch (_) { endpointCheck = "安装信息读取失败，请检查服务器与反向代理。"; }
		}
		const configuredURL = message.config.https_url || "未指定，使用当前网址";
		wrapper.html(`<div class="process-pwa-status">
			<p><strong>当前访问：</strong>${escape(window.location.origin)} · ${escape(secure)}</p>
			<p><strong>HTTPS 访问网址：</strong>${escape(configuredURL)}</p>
			<p><strong>安装检查：</strong>${escape(descriptions[state.state] || "正在准备")} ${escape(endpointCheck)}</p>
			${state.scope ? `<p><strong>已有作用范围：</strong>${escape(state.scope)}</p>` : ""}
			${state.message ? `<p><strong>失败原因：</strong>${escape(state.message)}</p>` : ""}
			<p><strong>官方 HR：</strong>${message.hrms_installed ? "本站已安装 HRMS。本应用使用独立安装标识，并检查注册冲突。" : "本站未安装 HRMS，无需为安装生产应用而安装 HRMS。"}</p>
			<p><strong>后台通知：</strong>尚未接入手机锁屏推送；当前通知在系统内查看。</p>
			<p class="text-muted">证书请在服务器入口使用 Let’s Encrypt 配置并自动续期。此检查只验证当前浏览器；正式使用前需用实际手机打开 HTTPS 网址验收。</p>
		</div>`);
	} catch (_) {
		wrapper.html('<div class="process-pwa-status">暂时无法读取安装设置，请刷新后重试。</div>');
	}
}
