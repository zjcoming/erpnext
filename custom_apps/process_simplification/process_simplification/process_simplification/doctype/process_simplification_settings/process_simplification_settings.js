frappe.ui.form.on("Process Simplification Settings", {
	refresh(frm) {
		if (!psCanConfigurePWA()) return;
		frm.add_custom_button("检查安装条件", () => psRefreshPWAStatus(frm), "手机应用");
		frm.add_custom_button("查看安装引导", () => {
			if (frm.is_dirty()) return frappe.msgprint("请先保存设置，再查看安装引导。");
			window.process_simplification?.pwa?.install();
		}, "手机应用");
		psRefreshPWAStatus(frm);
	},
	after_save(frm) {
		if (psCanConfigurePWA()) psRefreshPWAStatus(frm);
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
