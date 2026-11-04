"""Pure Shopify payload helpers (no `frappe` import, unit-testable).

They turn a raw Shopify order line into the numbers ERPNext must store. The
important rule — learned the hard way on the historical import — is that
ERPNext recomputes `amount = qty x rate`, so a discounted or free line has to
be passed as its **net** rate; `discount_amount` is ignored by the mapper.
"""

import base64
import datetime
import hashlib
import hmac
import re
from zoneinfo import ZoneInfo

# Shopify timestamps look like `2026-09-27T16:29:59+0500` (no colon in the
# offset). Frappe's Datetime column wants `2026-09-27 16:29:59`.
_OFFSET_WITHOUT_COLON = re.compile(r"([+-]\d{2})(\d{2})$")


def to_float(value, default=0.0) -> float:
	try:
		if value in (None, ""):
			return default
		return float(value)
	except (TypeError, ValueError):
		return default


def money(value) -> float:
	"""Two decimals, matching the Currency field precision ERPNext stores."""
	return round(to_float(value), 2)


def normalize_datetime(value, tz=None):
	"""Shopify ISO timestamp -> naive `datetime` in `tz` (or None).

	Shopify sends the offset; ERPNext stores a naive datetime in the site's
	timezone. When `tz` is given, an aware timestamp is converted to it before
	the tzinfo is dropped — `17:31Z` becomes `22:31` in Asia/Karachi instead of
	being mistaken for 17:31 local time. Without `tz`, the wall-clock part is
	kept (the original behaviour).
	"""
	if not value:
		return None
	text = str(value).strip().replace("T", " ")
	text = _OFFSET_WITHOUT_COLON.sub(r"\1:\2", text)

	parsed = None
	for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
		try:
			parsed = datetime.datetime.strptime(text, fmt)
			break
		except ValueError:
			continue
	if parsed is None:
		try:
			# Last resort: dateutil is a Frappe dependency.
			from dateutil import parser as dateutil_parser

			parsed = dateutil_parser.parse(text)
		except Exception:
			return None

	if parsed.tzinfo is not None:
		if tz:
			try:
				parsed = parsed.astimezone(ZoneInfo(tz))
			except Exception:
				pass
		return parsed.replace(tzinfo=None)
	return parsed


def iso_date(value):
	"""Shopify ISO timestamp -> `date` (or None)."""
	parsed = normalize_datetime(value)
	return parsed.date() if parsed else None


def total_discount(line_item) -> float:
	"""Sum of the line's discount allocations."""
	return sum(money(item.get("amount")) for item in (line_item.get("discount_allocations") or []))


def line_net_rate(line_item, taxes_inclusive: bool = False) -> float:
	"""Per-unit rate that reproduces Shopify's net line total.

	`price` is the pre-discount, tax-exclusive (or tax-inclusive) unit price;
	`discount_allocations` is the money actually taken off the line. When
	Shopify prices include tax, the tax portion is removed too, because ERPNext
	adds the mapped tax rows on top.
	"""
	price = money(line_item.get("price"))
	quantity = to_float(line_item.get("quantity"))
	if quantity <= 0:
		return 0.0

	discount_per_unit = total_discount(line_item) / quantity
	rate = price - discount_per_unit

	if taxes_inclusive:
		tax_total = sum(money(tax.get("price")) for tax in (line_item.get("tax_lines") or []))
		rate -= tax_total / quantity

	return money(rate)


def shipping_net_amount(shipping_line, taxes_inclusive: bool = False) -> float:
	"""The shipping charge without discounts (and without tax when included)."""
	amount = money(shipping_line.get("price"))
	amount -= sum(money(item.get("amount")) for item in (shipping_line.get("discount_allocations") or []))
	if taxes_inclusive:
		amount -= sum(money(tax.get("price")) for tax in (shipping_line.get("tax_lines") or []))
	return money(amount)


def tax_rate_percent(tax_line) -> float:
	"""Shopify stores the rate as a fraction (0.17) -> ERPNext wants 17.0."""
	return round(to_float(tax_line.get("rate")) * 100, 4)


def order_tag_list(order) -> list[str]:
	tags = order.get("tags") or ""
	if isinstance(tags, str):
		return [tag.strip() for tag in tags.split(",") if tag.strip()]
	return [str(tag).strip() for tag in tags if str(tag).strip()]


def is_paid(order) -> bool:
	return (order.get("financial_status") or "").lower() == "paid"


def is_cancelled(order) -> bool:
	return bool(order.get("cancelled_at")) or (order.get("financial_status") or "").lower() == "voided"


def valid_shopify_signature(body: bytes, signature: str, secret: str) -> bool:
	"""Verify Shopify's `X-Shopify-Hmac-Sha256` over the raw request body."""
	if not body or not signature or not secret:
		return False
	digest = base64.b64encode(hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest())
	try:
		return hmac.compare_digest(digest, signature.encode("utf-8"))
	except Exception:
		return False
