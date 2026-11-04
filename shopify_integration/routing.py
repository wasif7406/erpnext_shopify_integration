"""Pure routing helpers: which ERPNext company owns an order of a store.

Kept free of `frappe` imports so it can be unit-tested without a site. The
caller passes `Shopify Store Company` rows as plain dicts.
"""

from .constants import (
	ROUTE_ANY,
	ROUTE_APP_ID,
	ROUTE_CURRENCY,
	ROUTE_ORDER_TAG,
	ROUTE_SHIPPING_COUNTRY,
	ROUTE_SOURCE_NAME,
)

# Order-payload keys used by each routing mode.
ROUTE_KEYS = {
	ROUTE_SOURCE_NAME: ("source_name",),
	ROUTE_CURRENCY: ("currency", "presentment_currency"),
	ROUTE_APP_ID: ("app_id",),
}


class AmbiguousCompany(Exception):
	"""More than one company matched the order."""


class UnroutableOrder(Exception):
	"""No company matched and the store has no default company."""


def _text(value) -> str:
	return ("" if value is None else str(value)).strip().lower()


def _order_value(field: str, order: dict) -> list[str]:
	"""All candidate values of an order for a routing field (lower-cased)."""
	if field == ROUTE_SHIPPING_COUNTRY:
		address = order.get("shipping_address") or order.get("billing_address") or {}
		return [
			_text(address.get("country")),
			_text(address.get("country_code")),
		]
	if field == ROUTE_ORDER_TAG:
		tags = order.get("tags") or ""
		if isinstance(tags, str):
			return [_text(tag) for tag in tags.split(",") if tag.strip()]
		return [_text(tag) for tag in tags]

	values = []
	for key in ROUTE_KEYS.get(field, ()):
		value = order.get(key)
		if value not in (None, ""):
			values.append(_text(value))
	return values


def route_matches(field: str, route_value: str, order: dict) -> bool:
	"""Whether one company row matches this order."""
	route_value = _text(route_value)
	if not field or field == ROUTE_ANY or not route_value:
		return False
	return route_value in _order_value(field, order)


def resolve_company(rows: list[dict], order: dict) -> dict:
	"""Pick the `Shopify Store Company` row that owns `order`.

	Rules, in order:
	  1. one enabled row  -> that row (the normal 1 store / 1 company setup);
	  2. rows with a routing rule that matches -> exactly one must match;
	  3. the row flagged `is_default`;
	  4. otherwise refuse instead of guessing.

	A store may therefore serve several companies (N:M), with the routing rule
	deciding per order. Two matching rules are an error: silently picking one
	would post revenue to the wrong company.
	"""
	enabled = [row for row in rows if int(row.get("enabled", 1) or 0)]
	if not enabled:
		raise UnroutableOrder("The store has no enabled company.")

	if len(enabled) == 1:
		return enabled[0]

	matched = [
		row
		for row in enabled
		if route_matches(row.get("route_field"), row.get("route_value"), order)
	]
	if len(matched) == 1:
		return matched[0]
	if len(matched) > 1:
		labels = ", ".join(f"{row.get('company')} ({row.get('route_field')}={row.get('route_value')})" for row in matched)
		raise AmbiguousCompany(f"Order matches several companies: {labels}")

	defaults = [row for row in enabled if int(row.get("is_default", 0) or 0)]
	if len(defaults) == 1:
		return defaults[0]
	if len(defaults) > 1:
		labels = ", ".join(str(row.get("company")) for row in defaults)
		raise AmbiguousCompany(f"Several companies are flagged as default: {labels}")

	raise UnroutableOrder(
		"No company matched the order and none of the store's companies is flagged as default."
	)
