(function () {
	"use strict";
	const brand = frappe.boot?.hengsuan_branding;
	if (!brand) return;
	const escape = frappe.utils.escape_html;

	function showAbout() {
		frappe.msgprint({
			title: `关于${brand.product_name}`,
			message: `<div class="hs-about-product">
				<img src="${escape(brand.logo_url)}" alt="恒算科技标志">
				<h3>${escape(brand.product_name)}</h3>
				<p>${escape(brand.description)}</p>
				<p>连接销售、采购、库存、生产与工资管理，让企业经营有数。</p>
				<p class="hs-about-company">${escape(brand.company_name)}<br>产品研发与服务</p>
				<a href="${escape(brand.website)}" target="_blank" rel="noopener noreferrer">访问恒算科技官网 ↗</a>
				<p class="hs-about-foundation">基于 ERPNext / Frappe 开源软件构建</p>
			</div>`,
		});
	}

	function decorateSidebar() {
		const sidebar = document.querySelector(".body-sidebar");
		if (!sidebar) return;
		const header = sidebar.querySelector(".sidebar-header");
		if (header && frappe.app?.sidebar?.sidebar_data?.app === "process_simplification") {
			header.classList.add("hs-branded-header");
			const title = header.querySelector(".header-title");
			const subtitle = header.querySelector(".header-subtitle");
			if (title && title.textContent !== brand.product_name) title.textContent = brand.product_name;
			if (subtitle && subtitle.textContent !== brand.description) subtitle.textContent = brand.description;
			const logo = header.querySelector(".header-logo");
			if (logo && logo.querySelector("img")?.getAttribute("src") !== brand.logo_url) {
				const image = document.createElement("img");
				image.src = brand.logo_url;
				image.alt = brand.product_name;
				logo.replaceChildren(image);
			}
		} else if (header && frappe.app?.sidebar?.sidebar_data?.app === "erpnext") {
			// Keep the workspace name (for example, Organization) as navigation context.
			const subtitle = header.querySelector(".header-subtitle");
			if (subtitle && subtitle.textContent !== brand.product_name) subtitle.textContent = brand.product_name;
		}
		const bottom = sidebar.querySelector(".body-sidebar-bottom");
		if (bottom && !bottom.querySelector(".hs-brand-about")) {
			const button = document.createElement("button");
			button.type = "button";
			button.className = "hs-brand-about";
			button.setAttribute("aria-label", `关于${brand.product_name}`);
			button.title = brand.company_name;
			button.innerHTML = `<img src="${escape(brand.logo_url)}" alt=""><span>${escape(brand.company_short_name)} · 关于产品</span>`;
			button.addEventListener("click", showAbout);
			bottom.append(button);
		}
	}

	// The router sometimes restores an already decorated browser title.
	// Keep presentation idempotent without changing its stored route titles.
	function decorateTitle() {
		const prefix = `${brand.product_name} · `;
		let title = document.title;
		while (title.startsWith(prefix)) title = title.slice(prefix.length);
		const next = title && title !== brand.product_name ? prefix + title : brand.product_name;
		if (document.title !== next) document.title = next;
	}
	const titleElement = document.querySelector("title");
	if (titleElement) {
		new MutationObserver(decorateTitle).observe(titleElement, { childList: true, subtree: true, characterData: true });
	}
	decorateTitle();
	const dropdown = frappe.boot.navbar_settings?.settings_dropdown;
	if (dropdown && !dropdown.some((item) => item.name === "hengsuan-about")) {
		dropdown.push({
			name: "hengsuan-about", item_label: `关于${brand.product_name}`,
			item_type: "Action", icon: "info", onClick: showAbout,
		});
	}
	$(document).on("desktop_screen.hengsuan_branding", (_event, { desktop }) => {
		desktop.add_menu_item({ label: `关于${brand.product_name}`, icon: "info", order: 26, onClick: showAbout });
	});

	let observer;
	function start() {
		decorateSidebar();
		const sidebar = document.querySelector(".body-sidebar");
		if (sidebar && !observer) {
			// The native router rebuilds the header when switching workspaces.
			observer = new MutationObserver(decorateSidebar);
			observer.observe(sidebar, { childList: true, subtree: true });
		}
		decorateTitle();
	}
	$(document).one("app_ready.hengsuan_branding", start);
	$(document).on("page-change.hengsuan_branding", start);
	$(start);
})();
