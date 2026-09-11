"""Provide a reusable factory letterhead without replacing user customizations."""

from base64 import b64encode
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import frappe

from process_simplification.document_scan import SCAN_TYPES, document_scan_code

FACTORY_LETTERHEAD_NAME = "工厂简洁表头"
FACTORY_JOB_FORMAT_NAME = "工厂生产任务单"
FACTORY_FORMATS = {
	"Purchase Order": ("工厂采购订单", "factory_purchase_order.html"),
	"Job Card": (FACTORY_JOB_FORMAT_NAME, "factory_job_card.html"),
	"Purchase Receipt": ("工厂采购收退货单", "factory_purchase_receipt.html"),
	"Stock Entry": ("工厂库存作业单", "factory_stock_entry.html"),
	"Delivery Note": ("工厂销售出退货单", "factory_delivery_note.html"),
}
NATIVE_LETTERHEADS = {"", "Company Letterhead", "Company Letterhead - Grey"}
NATIVE_FORMAT_DEFAULTS = {
	"Purchase Order": {"Purchase Order Standard", "Purchase Order with Item Image"},
	"Delivery Note": {"Delivery Note Standard", "Delivery Note with Item Image"},
}
FACTORY_LETTERHEAD_TEMPLATE = Path(__file__).parent / "templates" / "print" / "factory_letterhead.html"
LEGACY_TEMPLATE_HASH = "547ee8ac977954713629aac53d5ed3d61a8493fae94d7822ba66f72e6b244e0f"
URL_TEMPLATE_HASH = "4fc2f562dcd1c8d9600d25c949259e0c8d001b759730f0d16d1ab4a2a9ed635b"
INTERNAL_TEMPLATE_HASH = "9159c1fe196a2b1f58d11ecb5d238b147d350f86449d75c3bc79f3ca43fa6c81"
FACTORY_LETTERHEAD_INCLUDE = '{% include "templates/print/factory_letterhead.html" %}'


def _format_html(template):
	return '{% include "templates/print/' + template + '" %}'


def _ensure_factory_format(doctype):
	name, template = FACTORY_FORMATS[doctype]
	existing = frappe.db.exists("Print Format", name)
	if not existing:
		frappe.get_doc({
			"doctype": "Print Format", "name": name,
			"doc_type": doctype, "standard": "No", "custom_format": 1,
			"print_format_type": "Jinja", "disabled": 0,
			"default_print_language": "zh", "html": _format_html(template),
		}).insert(ignore_permissions=True)
	else:
		# A same-name customer record is not evidence that our template was installed.
		current = frappe.db.get_value("Print Format", name, ["doc_type", "html", "disabled"], as_dict=True)
		if not current or current.doc_type != doctype or current.html != _format_html(template) or current.disabled:
			return
	default = frappe.get_meta(doctype).default_print_format
	native = default in (None, "", "Standard")
	if default in NATIVE_FORMAT_DEFAULTS.get(doctype, set()):
		native = frappe.db.get_value("Print Format", default, "standard") == "Yes"
	if native:
		from frappe.custom.doctype.property_setter.property_setter import make_property_setter
		make_property_setter(doctype, None, "default_print_format", name, "Data", for_doctype=True)


def ensure_factory_job_print_format():
	"""Compatibility for older migration callers."""
	_ensure_factory_format("Job Card")


def ensure_factory_print_formats():
	for doctype in FACTORY_FORMATS:
		_ensure_factory_format(doctype)


def factory_pdf_body_html(template, args, print_format=None, **kwargs):
	"""Apply the same managed header for preview, direct PDF and background printing."""
	from frappe.utils.pdf import pdf_body_html
	doc = args.get("doc")
	definition = FACTORY_FORMATS.get(doc.get("doctype")) if doc else None
	managed = definition and print_format and print_format.get("name") == definition[0] and print_format.get("html") == _format_html(definition[1])
	if managed and not args.get("no_letterhead"):
		selected = frappe.form_dict.get("letterhead") or doc.get("letter_head")
		if not selected:
			selected = frappe.db.get_value("Letter Head", {"is_default": 1}, "name") or ""
		if selected in NATIVE_LETTERHEADS:
			header = frappe.db.get_value("Letter Head", FACTORY_LETTERHEAD_NAME, ["content", "footer", "header_script", "footer_script", "disabled"], as_dict=True)
			if header and not header.disabled:
				def render_part(source, script):
					text = frappe.render_template(source, {"doc": doc.as_dict()}) if source else ""
					return text + ("<script>" + script + "</script>" if script else "")
				args = {**args, "letter_head": render_part(header.content, header.header_script), "footer": render_part(header.footer, header.footer_script)}
	return pdf_body_html(template, args, **kwargs)


def get_document_scan_qr(doc):
	"""Render an internal code locally, without an address, File record or remote service."""
	if (
		not doc
		or doc.get("doctype") not in SCAN_TYPES.values()
		or doc.get("__islocal")
		or not doc.get("name")
	):
		return None
	from pyqrcode import create

	code = document_scan_code(doc.get("doctype"), doc.get("name"))
	stream = BytesIO()
	# Raster output avoids wkhtmltopdf's distortion of SVG module strokes.
	# Eight pixels per module stays sharp at the 28 mm printed size.
	create(code, error="M").png(stream, scale=8, quiet_zone=4)
	return frappe._dict(image="data:image/png;base64," + b64encode(stream.getvalue()).decode("ascii"))


def ensure_factory_letterhead():
	"""Upgrade only our unchanged first template; preserve user content and defaults."""
	if frappe.db.exists("Letter Head", FACTORY_LETTERHEAD_NAME):
		content = frappe.db.get_value("Letter Head", FACTORY_LETTERHEAD_NAME, "content") or ""
		if isinstance(content, str) and sha256(content.encode("utf-8")).hexdigest() in {
			LEGACY_TEMPLATE_HASH,
			URL_TEMPLATE_HASH,
			INTERNAL_TEMPLATE_HASH,
		}:
			doc = frappe.get_doc("Letter Head", FACTORY_LETTERHEAD_NAME)
			if doc.source == "HTML":
				doc.content = FACTORY_LETTERHEAD_INCLUDE
				doc.save(ignore_permissions=True)
		return

	frappe.get_doc(
		{
			"doctype": "Letter Head",
			"letter_head_name": FACTORY_LETTERHEAD_NAME,
			"source": "HTML",
			"footer_source": "HTML",
			"align": "Center",
			"disabled": 0,
			"is_default": int(not frappe.db.exists("Letter Head", {"is_default": 1})),
			"content": FACTORY_LETTERHEAD_INCLUDE,
		}
	).insert(ignore_permissions=True)
