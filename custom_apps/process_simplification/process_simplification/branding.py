"""Hengsuan product identity, separate from document and route identifiers."""

from __future__ import annotations

PRODUCT_NAME = "恒算 ERP"
COMPANY_NAME = "重庆恒算科技有限公司"
COMPANY_SHORT_NAME = "恒算科技"
COMPANY_WEBSITE = "https://www.hengsuankeji.com/"
PRODUCT_DESCRIPTION = "企业经营管理系统"
LOGO_URL = "/assets/process_simplification/images/hengsuan.svg"


def get_branding():
	return {
		"product_name": PRODUCT_NAME,
		"company_name": COMPANY_NAME,
		"company_short_name": COMPANY_SHORT_NAME,
		"website": COMPANY_WEBSITE,
		"description": PRODUCT_DESCRIPTION,
		"logo_url": LOGO_URL,
	}


def update_website_context(context):
	"""Brand the existing login controller and Desk without replacing authentication."""
	context.update(
		app_name=PRODUCT_NAME,
		favicon=LOGO_URL,
		hengsuan_branding=get_branding(),
	)
	path = context.get("path") or ""
	if path == "desk" or path.startswith("desk/"):
		context.splash_image = LOGO_URL
	if path == "login":
		context.update(
			logo=LOGO_URL,
			title=f"登录 · {PRODUCT_NAME}",
			template="process_simplification/templates/hengsuan_login.html",
			body_class=f"{context.get('body_class') or ''} hs-login".strip(),
		)


def boot_session(bootinfo):
	bootinfo.hengsuan_branding = get_branding()
	bootinfo.app_logo_url = LOGO_URL
	# Stored names remain stable for routing, permissions and saved links.
	bootinfo.setdefault("__messages", {}).update(
		{
			"Process Simplification": PRODUCT_NAME,
			"process-simplification": PRODUCT_NAME,
			"流程简化": PRODUCT_NAME,
		}
	)
	for icon in bootinfo.get("desktop_icons") or []:
		if icon.get("app") == "process_simplification":
			icon["logo_url"] = LOGO_URL
	for sidebar in (bootinfo.get("workspace_sidebar_item") or {}).values():
		if sidebar.get("app") != "process_simplification":
			continue
		for item in sidebar.get("items") or []:
			if item.get("link_to") == "process-simplification":
				item["label"] = "工作台"
			elif item.get("link_to") == "Process Simplification Settings":
				item["label"] = "系统设置"
