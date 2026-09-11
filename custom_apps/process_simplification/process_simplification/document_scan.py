"""Address-independent internal document codes; identifiers never grant access."""

import base64
import re
from uuid import uuid4

import frappe
from frappe import _

SCAN_TYPES = {
	"purchase-order": "Purchase Order", "job-card": "Job Card",
	"purchase-receipt": "Purchase Receipt", "stock-entry": "Stock Entry", "delivery-note": "Delivery Note",
}
SCAN_PREFIX = "HSERP|1"
SCAN_SITE_ID_KEY = "process_document_scan_site_id"
MAX_CODE_LENGTH = 1024
MAX_NAME_LENGTH = 140
INVALID_CODE = "无法识别这个单据码，请扫描本系统采购、生产或库存单据上的二维码。"
WRONG_SITE = "这不是当前系统的单据二维码，请确认单据所属系统。"
LEGACY_CODE = "旧版网址二维码已停用，请重新打印单据，在系统内“扫一扫”中扫描新码。"
NOT_INITIALIZED = "扫码功能尚未初始化，请联系管理员完成系统升级。"


def get_scan_site_id():
	"""Public installation namespace, persisted with database backups, not a credential."""
	value = frappe.defaults.get_defaults_for("__default").get(SCAN_SITE_ID_KEY)
	return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) else None


def ensure_scan_site_id():
	"""Installation/migration only: printing and resolving must never create IDs."""
	value = frappe.defaults.get_defaults_for("__default").get(SCAN_SITE_ID_KEY)
	if value:
		if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
			frappe.throw(_("系统扫码标识无效，请联系管理员检查配置；不要覆盖已有标识。"))
		return value
	value = uuid4().hex
	frappe.defaults.set_global_default(SCAN_SITE_ID_KEY, value)
	return value


def boot_session(bootinfo):
	if frappe.session.user != "Guest":
		bootinfo.process_document_scan = {"site_id": get_scan_site_id()}


def encode_document_name(name: str) -> str:
	if (
		not isinstance(name, str)
		or not name.strip()
		or len(name) > MAX_NAME_LENGTH
		or re.search(r"[\x00-\x1f\x7f]", name)
	):
		raise ValueError("Invalid document name")
	return base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("=")


def decode_scan_target(kind: str, token: str) -> tuple[str, str]:
	if not isinstance(kind, str) or kind not in SCAN_TYPES:
		raise ValueError("Unsupported document type")
	if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,750}", token):
		raise ValueError("Invalid document code")
	try:
		name = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")
		if encode_document_name(name) != token:
			raise ValueError("Non-canonical document code")
	except (ValueError, UnicodeError) as exc:
		raise ValueError("Invalid document code") from exc
	return SCAN_TYPES[kind], name


def document_scan_code(doctype: str, name: str, site_id: str | None = None) -> str:
	kind = next((key for key, value in SCAN_TYPES.items() if value == doctype), None)
	if not kind:
		raise ValueError("Unsupported document type")
	site_id = site_id or get_scan_site_id()
	if not isinstance(site_id, str) or not re.fullmatch(r"[0-9a-f]{32}", site_id):
		raise ValueError(NOT_INITIALIZED)
	return f"{SCAN_PREFIX}|{site_id}|{kind}|{encode_document_name(name)}"


def decode_document_scan(code: str, site_id: str | None) -> tuple[str, str]:
	if not isinstance(code, str) or len(code) > MAX_CODE_LENGTH:
		raise ValueError(INVALID_CODE)
	code = code.strip()
	if re.match(r"(?:https?://|/)", code, re.IGNORECASE):
		raise ValueError(LEGACY_CODE)
	if not isinstance(site_id, str) or not re.fullmatch(r"[0-9a-f]{32}", site_id):
		raise ValueError(NOT_INITIALIZED)
	parts = code.split("|")
	if len(parts) != 5 or "|".join(parts[:2]) != SCAN_PREFIX or not re.fullmatch(r"[0-9a-f]{32}", parts[2]):
		raise ValueError(INVALID_CODE)
	if parts[2] != site_id:
		raise ValueError(WRONG_SITE)
	try:
		return decode_scan_target(parts[3], parts[4])
	except ValueError as exc:
		raise ValueError(INVALID_CODE) from exc
