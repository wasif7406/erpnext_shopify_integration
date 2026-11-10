"""Catalogue helpers for the desk picker (no `frappe` import, unit-testable).

The Shopify Admin REST API has no free-text product search, so the picker
downloads the (small) catalogue once per store, caches it server-side and
matches the search term in memory. Keeping this module free of `frappe` means
the matching rules can be tested without a site.
"""

# Only these columns are needed by the dialog; asking for everything would make
# a 200-variant catalogue several hundred kilobytes and slow the desk down.
FIELDS = "id,title,handle,status,product_type,vendor,image,variants"

# Hard stops: a mis-typed store should not stream 40k products into a browser.
MAX_PRODUCTS = 5000
MAX_ROWS = 300

OPTION_KEYS = ("option1", "option2", "option3")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def _text(value) -> str:
	return (value if value is not None else "").strip() if isinstance(value, str) else (
		str(value).strip() if value is not None else ""
	)


def _float(value) -> float:
	try:
		return float(value)
	except (TypeError, ValueError):
		return 0.0


def _int(value) -> int:
	try:
		return int(float(value))
	except (TypeError, ValueError):
		return 0


def normalize_variant(variant: dict) -> dict:
	"""One Shopify variant -> the flat row the dialog renders."""
	row = variant or {}
	options = [_text(row.get(key)) for key in OPTION_KEYS]
	return {
		"variant_id": _text(row.get("id")),
		"title": _text(row.get("title")) or "Default Title",
		"sku": _text(row.get("sku")),
		"barcode": _text(row.get("barcode")),
		"price": _float(row.get("price")),
		"compare_at_price": _float(row.get("compare_at_price")),
		"inventory_item_id": _text(row.get("inventory_item_id")),
		"inventory_quantity": _int(row.get("inventory_quantity")),
		"weight": _float(row.get("weight")),
		"weight_unit": _text(row.get("weight_unit")),
		"requires_shipping": bool(row.get("requires_shipping", True)),
		"options": [value for value in options if value],
	}


def normalize_product(product: dict) -> dict:
	"""One Shopify product -> product row with nested variant rows."""
	row = product or {}
	variants = [normalize_variant(item) for item in (row.get("variants") or [])]
	image = row.get("image") or {}
	if not isinstance(image, dict):
		image = {"src": image}
	return {
		"product_id": _text(row.get("id")),
		"title": _text(row.get("title")),
		"handle": _text(row.get("handle")),
		"status": _text(row.get("status")) or "active",
		"product_type": _text(row.get("product_type")),
		"vendor": _text(row.get("vendor")),
		"image": _text(image.get("src")),
		"variants": variants,
		"variant_count": len(variants),
		"skus": [item["sku"] for item in variants if item["sku"]],
		"inventory_total": sum(item["inventory_quantity"] for item in variants),
	}


def sort_products(products) -> list:
	return sorted(products or [], key=lambda row: ((row.get("title") or "").lower(), row.get("product_id") or ""))


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def _matches(term: str, *values) -> bool:
	for value in values:
		if value is None:
			continue
		if term in str(value).lower():
			return True
	return False


def search_products(products, term=None) -> list:
	"""Products whose title/handle/type/vendor/id or any variant SKU matches."""
	term = _text(term).lower()
	products = products or []
	if not term:
		return list(products)
	return [
		row
		for row in products
		if _matches(
			term,
			row.get("title"),
			row.get("handle"),
			row.get("product_type"),
			row.get("vendor"),
			row.get("product_id"),
			*(row.get("skus") or []),
			*[variant.get("barcode") for variant in row.get("variants") or []],
			*[variant.get("variant_id") for variant in row.get("variants") or []],
		)
	]


def variant_rows(products) -> list:
	"""Flatten every variant, keeping its product context for the dialog."""
	rows = []
	for product in products or []:
		for variant in product.get("variants") or []:
			row = dict(variant)
			row.update(
				{
					"product_id": product.get("product_id"),
					"product_title": product.get("title"),
					"product_status": product.get("status"),
					"product_handle": product.get("handle"),
					"image": product.get("image"),
				}
			)
			rows.append(row)
	return rows


def search_variants(products, term=None) -> list:
	"""Variants whose product, title, SKU, barcode or id matches."""
	rows = variant_rows(products)
	term = _text(term).lower()
	if not term:
		return rows
	return [
		row
		for row in rows
		if _matches(
			term,
			row.get("product_title"),
			row.get("product_handle"),
			row.get("product_id"),
			row.get("title"),
			row.get("sku"),
			row.get("barcode"),
			row.get("variant_id"),
		)
	]


def clamp(rows, limit=None) -> tuple:
	"""Split matches into (visible rows, total) so the dialog can say 'N of M'."""
	rows = list(rows or [])
	limit = MAX_ROWS if limit in (None, "") else max(1, int(limit))
	return rows[:limit], len(rows)
