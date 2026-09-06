"""Install the existing Desk as a PWA without taking over another app's worker."""

from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from werkzeug.wrappers import Response

import frappe
from frappe import _
from frappe.utils import cint

from process_simplification.management_access import OWNER_ROLE, SYSTEM_MANAGER_ROLE

SETTINGS_DOCTYPE = "Process Simplification Settings"
APP_ID = "/process-simplification-pwa"
# No trailing slash: the Desk home is /desk, as well as /desk/<page>.
SCOPE = "/desk"
START_URL = "/desk"
MANIFEST_URL = "/api/method/process_simplification.pwa.manifest"
WORKER_URL = "/api/method/process_simplification.pwa.service_worker"
ICON_ROOT = "/assets/process_simplification/images/pwa"
PWA_DEFAULTS = {
	"enable_pwa": 1,
	"pwa_app_name": "工厂工作台",
	"pwa_short_name": "工厂工作台",
	"pwa_install_prompt": 1,
	"pwa_prompt_interval_days": 7,
}
PWA_FIELDS = (*PWA_DEFAULTS, "pwa_https_url")


def require_settings_access():
	if frappe.session.user == "Administrator":
		return
	if not {OWNER_ROLE, SYSTEM_MANAGER_ROLE}.intersection(frappe.get_roles()):
		frappe.throw(_("只有老板或系统管理员可以配置手机应用。"), frappe.PermissionError)


def normalize_https_url(value: str | None) -> str:
	"""Only store an HTTPS origin. Never fetch user-supplied hosts on the server."""
	value = (value or "").strip()
	if not value:
		return ""
	try:
		url = urlsplit(value)
		if (
			url.scheme != "https"
			or not url.hostname
			or url.username is not None
			or url.password is not None
			or url.path not in ("", "/")
			or url.query
			or url.fragment
			or re.search(r"[\s\\\x00-\x1f\x7f]", value)
		):
			raise ValueError
		port = url.port
		if port is not None and not 1 <= port <= 65535:
			raise ValueError
		host = url.hostname.encode("idna").decode("ascii").lower()
		try:
			address = ipaddress.ip_address(host)
			host = f"[{address}]" if address.version == 6 else str(address)
		except ValueError:
			if len(host) > 253 or not all(
				re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
				for label in host.split(".")
			):
				raise ValueError from None
		return urlunsplit(("https", host + (f":{port}" if port and port != 443 else ""), "", "", ""))
	except (ValueError, UnicodeError):
		frappe.throw(_("HTTPS 访问网址应为完整域名，例如 https://erp.example.com，不含路径、账号或参数。"))


def validate_settings(doc):
	if any(doc.has_value_changed(field) for field in PWA_FIELDS):
		require_settings_access()
	doc.pwa_https_url = normalize_https_url(doc.get("pwa_https_url"))
	for field, limit in (("pwa_app_name", 40), ("pwa_short_name", 12)):
		value = (doc.get(field) or PWA_DEFAULTS[field]).strip()
		if not value or len(value) > limit or re.search(r"[\x00-\x1f\x7f]", value):
			frappe.throw(_("{0}应为 1 至 {1} 个字符。").format(_(doc.meta.get_label(field)), limit))
		setattr(doc, field, value)
	interval = doc.get("pwa_prompt_interval_days")
	if interval is None:
		interval = PWA_DEFAULTS["pwa_prompt_interval_days"]
	if not 1 <= cint(interval) <= 365 or float(interval) != cint(interval):
		frappe.throw(_("安装提醒间隔应为 1 至 365 天的整数。"))
	doc.pwa_prompt_interval_days = cint(interval)


def ensure_defaults():
	"""Fill new Single fields once; preserve an administrator's disabled switches."""
	# get_single_value casts a missing Check to 0, which is indistinguishable from
	# an explicitly disabled setting. Inspect stored keys before applying defaults.
	stored = frappe.db.get_singles_dict(SETTINGS_DOCTYPE)
	for field, value in PWA_DEFAULTS.items():
		if field not in stored:
			frappe.db.set_single_value(SETTINGS_DOCTYPE, field, value)


def _settings():
	return frappe.get_cached_doc(SETTINGS_DOCTYPE)


def _value(doc, field):
	value = doc.get(field)
	return PWA_DEFAULTS.get(field) if value is None else value


def _config():
	doc = _settings()
	return {
		"enabled": bool(cint(_value(doc, "enable_pwa"))),
		"app_name": _value(doc, "pwa_app_name") or PWA_DEFAULTS["pwa_app_name"],
		"short_name": _value(doc, "pwa_short_name") or PWA_DEFAULTS["pwa_short_name"],
		"https_url": doc.get("pwa_https_url") or "",
		"auto_prompt": bool(cint(_value(doc, "pwa_install_prompt"))),
		"prompt_interval_days": cint(_value(doc, "pwa_prompt_interval_days")),
		"id": APP_ID,
		"scope": SCOPE,
		"start_url": START_URL,
		"manifest_url": MANIFEST_URL,
		"worker_url": WORKER_URL,
		"icon_url": f"{ICON_ROOT}/icon-192.png",
	}


@frappe.whitelist(methods=["GET"])
def get_config():
	from process_simplification.permissions import can_access_app

	return {**_config(), "available": frappe.session.user != "Guest" and can_access_app()}


def boot_session(bootinfo):
	bootinfo.process_pwa = get_config()


@frappe.whitelist(methods=["GET"])
def get_status():
	require_settings_access()
	return {"config": get_config(), "hrms_installed": "hrms" in frappe.get_installed_apps()}


@frappe.whitelist(allow_guest=True, methods=["GET"])
def manifest():
	config = _config()
	if not config["enabled"]:
		return Response(status=404, headers={"Cache-Control": "no-store"})
	data = {
		"id": APP_ID,
		"name": config["app_name"],
		"short_name": config["short_name"],
		"description": "生产、报工与工厂管理",
		"lang": "zh-CN",
		"start_url": START_URL,
		"scope": SCOPE,
		"display": "standalone",
		"theme_color": "#2563eb",
		"background_color": "#ffffff",
		"prefer_related_applications": False,
		"icons": [
			{"src": f"{ICON_ROOT}/icon-{size}.png", "sizes": f"{size}x{size}", "type": "image/png", "purpose": "any"}
			for size in (192, 512)
		] + [{"src": f"{ICON_ROOT}/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
	}
	# All URLs stay on the installing origin, including behind a TLS reverse proxy.
	# Do not include user IDs, permissions, notification routing, or session tokens.
	return Response(
		json.dumps(data, ensure_ascii=False),
		content_type="application/manifest+json; charset=utf-8",
		headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
	)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def service_worker():
	# Keep this endpoint available when disabled so already installed workers can update.
	script = Path(__file__).with_name("public").joinpath("js", "process_pwa_worker.js").read_text(encoding="utf-8")
	return Response(
		script,
		content_type="application/javascript; charset=utf-8",
		headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": SCOPE, "X-Content-Type-Options": "nosniff"},
	)
