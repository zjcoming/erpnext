"""Chinese print presentation, without rewriting documents or company masters."""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import frappe

PURCHASE_FORMATS = {"Purchase Order Standard", "Purchase Order with Item Image"}
DIGITS = "零壹贰叁肆伍陆柒捌玖"
GROUP_UNITS = ("", "万", "亿", "万亿")


def _four_digits(number):
	parts = []
	zero = False
	for divisor, unit in ((1000, "仟"), (100, "佰"), (10, "拾"), (1, "")):
		digit, number = divmod(number, divisor)
		if digit:
			if zero:
				parts.append("零")
			parts.append(DIGITS[digit] + unit)
			zero = False
		elif parts:
			zero = True
	return "".join(parts)


def chinese_yuan(amount):
	"""Spell a CNY amount at its displayed two-decimal precision."""
	try:
		value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
	except (InvalidOperation, ValueError, TypeError) as exc:
		raise ValueError("Invalid CNY amount") from exc
	if not value.is_finite() or abs(value) >= Decimal("10000000000000000"):
		raise ValueError("CNY amount outside supported range")
	negative = value < 0
	cents = int(abs(value) * 100)
	integer, fraction = divmod(cents, 100)
	groups = []
	while integer:
		integer, group = divmod(integer, 10000)
		groups.append(group)
	parts = []
	zero = False
	for index in range(len(groups) - 1, -1, -1):
		group = groups[index]
		if not group:
			zero = bool(parts)
			continue
		if parts and (zero or group < 1000):
			parts.append("零")
		parts.append(_four_digits(group) + GROUP_UNITS[index])
		zero = False
	words = ("".join(parts) or "零") + "元"
	jiao, fen = divmod(fraction, 10)
	if not fraction:
		words += "整"
	else:
		if jiao:
			words += DIGITS[jiao] + "角"
		elif cents >= 100:
			words += "零"
		if fen:
			words += DIGITS[fen] + "分"
	return "人民币" + ("负" if negative else "") + words


def get_print_amount_in_words(doc):
	# These two native templates print grand_total, not rounded_total.
	if doc.get("currency") == "CNY":
		return chinese_yuan(doc.get("grand_total"))
	return doc.get("in_words") or ""


def get_print_format_template(jenv, print_format):
	"""Localize only the managed native purchase formats in a Chinese preview."""
	if (
		frappe.local.lang not in {"zh", "zh-CN", "zh_CN"}
		or print_format.get("name") not in PURCHASE_FORMATS
		or print_format.get("standard") != "Yes"
		or print_format.get("doc_type") != "Purchase Order"
	):
		return None
	from frappe.www.printview import get_print_format

	source = get_print_format("Purchase Order", print_format)
	# "No" is the boolean translation "否"; this column actually means row number.
	source = re.sub(r"_\([\"']No[\"']\)", '_("Sr No")', source)
	source = re.sub(r"{{\s*item\.uom\s*}}", "{{ _(item.uom) | e }}", source)
	source = re.sub(r"{{\s*doc\.in_words\s*}}", "{{ get_print_amount_in_words(doc) | e }}", source)
	return jenv.from_string(source)


@frappe.whitelist()
def skip_automatic_company_details(doctype=None, docname=None):
	"""Optional profile fields must not interrupt entry to a print preview.

	The native print-page callback opens its setup dialog only for a truthy result.
	Company and Address remain editable through their normal permission-checked forms.
	"""
	return None
