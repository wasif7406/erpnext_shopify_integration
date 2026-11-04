"""Products and items: pull catalogue/line items, push ERPNext items.

Pull policy
-----------
An order line is only usable if it maps to an ERPNext `Item`. When
`pull_products` is on, a missing item is created from the full Shopify product
(the product is fetched once per product id, not once per line). When it is
off, the order fails with a clear message instead of silently dropping a line.

Push policy
-----------
`Item.after_insert` / `on_update` call `upload_item`. It does nothing unless at
least one store has `push_items` on, and it never touches an item that was
itself created from Shopify (the `from_shopify` flag).
"""

import frappe
from frappe.utils import cint, flt
from frappe.utils.nestedset import get_root_of

from .client import RequestBudget, ShopifyClient
from .constants import (
	ITEM_MAP_DOCTYPE,
	ITEM_SELLING_RATE_FIELD,
	STORE_DOCTYPE,
)
from .config import StoreConfig, config, warehouse_for
from .exceptions import ShopifyClientError, ShopifyConfigError
from .payloads import line_net_rate, money
from .utils import Run, log_error, now

WEIGHT_TO_ERPNEXT_UOM = {"kg": "Kg", "g": "Gram", "oz": "Ounce", "lb": "Pound"}
SHOPIFY_VARIANT_ATTRS = ["option1", "option2", "option3"]


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------
def find_map(cfg: StoreConfig, product_id=None, variant_id=None, sku=None):
	if variant_id not in (None, ""):
		name = _scalar(
			"select name from `tabShopify Item Map` where store=%s and variant_id=%s limit 1",
			(cfg.name, str(variant_id)),
		)
		if name:
			return frappe.get_doc(ITEM_MAP_DOCTYPE, name)

	if product_id not in (None, "") and variant_id in (None, ""):
		name = _scalar(
			"select name from `tabShopify Item Map` where store=%s and product_id=%s "
			"and ifnull(variant_id, '') = '' limit 1",
			(cfg.name, str(product_id)),
		)
		if name:
			return frappe.get_doc(ITEM_MAP_DOCTYPE, name)

	if sku:
		name = _scalar(
			"select name from `tabShopify Item Map` where store=%s and sku=%s order by creation desc limit 1",
			(cfg.name, sku),
		)
		if name:
			return frappe.get_doc(ITEM_MAP_DOCTYPE, name)

	return None


def _scalar(query, values):
	rows = frappe.db.sql(query, values)
	return rows[0][0] if rows else None


def map_for_item(cfg: StoreConfig, item_code: str):
	name = frappe.db.get_value(
		ITEM_MAP_DOCTYPE,
		{"store": cfg.name, "item_code": item_code},
		"name",
		order_by="creation asc",
	)
	return frappe.get_doc(ITEM_MAP_DOCTYPE, name) if name else None


def remember_item(
	cfg: StoreConfig,
	item_code: str,
	product_id,
	variant_id=None,
	sku=None,
	has_variants=0,
	variant_of=None,
	inventory_item_id=None,
) -> "frappe.Document":
	"""Create or refresh the store <-> item mapping row."""
	product_id = str(product_id or "")
	variant_id = str(variant_id or "")
	if not product_id:
		return None

	existing = _scalar(
		"select name from `tabShopify Item Map` where store=%s and product_id=%s "
		"and ifnull(variant_id, '')=%s limit 1",
		(cfg.name, product_id, variant_id),
	)
	values = {
		"store": cfg.name,
		"item_code": item_code,
		"product_id": product_id,
		"variant_id": variant_id,
		"sku": sku or "",
		"has_variants": cint(has_variants),
		"variant_of": variant_of or "",
		"inventory_item_id": str(inventory_item_id or ""),
		"item_synced_on": now(),
	}
	if existing:
		doc = frappe.get_doc(ITEM_MAP_DOCTYPE, existing)
		for key, value in values.items():
			if key == "inventory_item_id" and not value:
				continue
			setattr(doc, key, value)
		doc.save(ignore_permissions=True)
		return doc

	doc = frappe.get_doc({"doctype": ITEM_MAP_DOCTYPE, **values})
	doc.insert(ignore_permissions=True, ignore_mandatory=True)
	return doc


def get_item_code(cfg: StoreConfig, line_item):
	"""ERPNext item code for one Shopify line, or None."""
	product_id = line_item.get("product_id")
	variant_id = line_item.get("variant_id")
	sku = line_item.get("sku")

	row = find_map(cfg, product_id=product_id, variant_id=variant_id, sku=sku)
	if row:
		return row.item_code

	# A SKU that is literally an ERPNext item code is the merchant's own link.
	if sku and frappe.db.exists("Item", sku):
		return sku

	return None


# ---------------------------------------------------------------------------
# Pull: order lines
# ---------------------------------------------------------------------------
def ensure_items(cfg: StoreConfig, order: dict, budget: RequestBudget | None = None, run: Run | None = None):
	"""Return ERPNext item rows for every line, creating missing items.

	The whole order is refused when one line cannot be mapped: importing a
	partial order would silently change the customer's invoice.
	"""
	company_cfg = cfg.resolve_company(order)
	warehouse = warehouse_for(cfg, company_cfg)
	from frappe.utils import nowdate

	delivery_date = (order.get("created_at") or "")[:10] or nowdate()
	taxes_inclusive = bool(order.get("taxes_included"))

	items = []
	missing = []
	for line in order.get("line_items") or []:
		item_code = get_item_code(cfg, line)
		if not item_code:
			if not cfg.pull_products:
				# Explicit mapping only: never invent an Item for a product the
				# merchant did not list in Shopify Item Map.
				missing.append(f"{line.get('title') or line.get('name')} (SKU {line.get('sku') or '—'})")
				continue
			item_code = create_items_from_line(cfg, line, company_cfg, budget=budget, run=run)
		if not item_code:
			missing.append(f"{line.get('title') or line.get('name')} (SKU {line.get('sku') or '—'})")
			continue

		qty = flt(line.get("quantity"))
		rate = line_net_rate(line, taxes_inclusive=taxes_inclusive)
		items.append(
			{
				"item_code": item_code,
				"item_name": (line.get("name") or line.get("title") or item_code)[:140],
				"description": line.get("title") or None,
				"rate": rate,
				"qty": qty,
				"stock_uom": line.get("uom") or "Nos",
				"warehouse": warehouse,
				"delivery_date": delivery_date,
				"is_free_item": 1 if rate <= 0 else 0,
				"shopify_item_discount": money(
					sum(money(d.get("amount")) for d in (line.get("discount_allocations") or [])) / qty
				)
				if qty
				else 0,
				"shopify_line_item_id": str(line.get("id") or ""),
			}
		)

	if missing:
		from .exceptions import ShopifyUnmappedProduct

		raise ShopifyUnmappedProduct(
			f"Shopify order {order.get('name')} has no explicit item mapping for: "
			+ "; ".join(missing[:10])
			+ ". Add a Shopify Item Map row (SKU or product/variant id), or enable "
			"'Pull Products / Auto-create Items'."
		)

	if not items:
		raise ShopifyConfigError(f"Shopify order {order.get('name')} has no sellable line.")

	return items


def create_items_from_line(cfg: StoreConfig, line, company_cfg, budget=None, run=None):
	"""Create the item(s) for one line, preferring the full product payload."""
	product_id = line.get("product_id")
	if product_id:
		try:
			client = ShopifyClient(cfg, budget=budget)
			product = client.product(product_id)
			if product:
				created = make_items_from_product(cfg, product, company_cfg, run=run)
				if run:
					run.bump("items_created", created)
				item_code = get_item_code(cfg, line)
				if item_code:
					return item_code
		except (ShopifyClientError, ShopifyConfigError) as exc:
			log_error(
				"Shopify: product fetch failed",
				f"{cfg.name} product {product_id}\n{exc}",
			)

	return create_item_from_line_fallback(cfg, line, company_cfg)


def create_item_from_line_fallback(cfg: StoreConfig, line, company_cfg):
	"""Create a single item from the order line when the product is unavailable."""
	sku = (line.get("sku") or "").strip()
	variant_id = str(line.get("variant_id") or "")
	item_code = sku or variant_id
	if not item_code:
		return None

	if frappe.db.exists("Item", item_code):
		remember_item(
			cfg,
			item_code,
			line.get("product_id"),
			variant_id=variant_id,
			sku=sku,
		)
		return item_code

	item = _new_item(
		item_code=item_code,
		item_name=line.get("name") or line.get("title") or item_code,
		company=company_cfg.company,
		warehouse=warehouse_for(cfg, company_cfg),
		description=line.get("title"),
	)
	item.insert(ignore_permissions=True, ignore_mandatory=True)
	remember_item(
		cfg,
		item.name,
		line.get("product_id"),
		variant_id=variant_id,
		sku=sku,
	)
	return item.name


# ---------------------------------------------------------------------------
# Pull: full product
# ---------------------------------------------------------------------------
def make_items_from_product(cfg: StoreConfig, product: dict, company_cfg, run=None) -> int:
	"""Create the template + variants (or the single item) for a Shopify product."""
	product_id = str(product.get("id") or "")
	if not product_id:
		return 0

	variants = product.get("variants") or []
	options = product.get("options") or []
	has_variants = _has_real_variants(options, variants)

	created = 0
	if not has_variants:
		variant = variants[0] if variants else {}
		item_code = (variant.get("sku") or str(variant.get("id") or product_id)).strip()
		item = _ensure_item(
			cfg,
			item_code=item_code,
			item_name=product.get("title") or variant.get("title") or item_code,
			product=product,
			variant=variant,
			company=company_cfg.company,
			warehouse=warehouse_for(cfg, company_cfg),
			is_stock_item=1 if variant.get("requires_shipping", True) else 0,
		)
		created += 1 if item else 0
		return created

	template_code = (product.get("handle") or f"shopify-{product_id}")[:140]
	template = _ensure_item(
		cfg,
		item_code=template_code,
		item_name=product.get("title") or template_code,
		product=product,
		variant=None,
		company=company_cfg.company,
		warehouse=warehouse_for(cfg, company_cfg),
		has_variants=1,
		attributes=_attributes_from_options(options),
		is_stock_item=0,
	)
	if not template:
		# Without a template, variant items would point at a non-existent item.
		return created
	created += 1
	# The stored item code may have been trimmed to 140 chars; variants must
	# point at the document's real name.
	template_code = template.name
	remember_item(cfg, template.name, product_id, has_variants=1)

	for variant in variants:
		variant_id = str(variant.get("id") or "")
		item_code = (variant.get("sku") or variant_id or "").strip()
		if not item_code:
			continue
		attributes = _attributes_for_variant(variant, options)
		item = _ensure_item(
			cfg,
			item_code=item_code,
			item_name=f"{product.get('title')} - {variant.get('title')}".strip(" -"),
			product=product,
			variant=variant,
			company=company_cfg.company,
			warehouse=warehouse_for(cfg, company_cfg),
			variant_of=template_code,
			attributes=attributes,
		)
		if item:
			created += 1
			remember_item(
				cfg,
				item.name,
				product_id,
				variant_id=variant_id,
				sku=variant.get("sku"),
				variant_of=template_code,
				inventory_item_id=variant.get("inventory_item_id"),
			)

	return created


def _ensure_item(
	cfg,
	item_code,
	item_name,
	product,
	variant,
	company,
	warehouse,
	has_variants=0,
	attributes=None,
	variant_of=None,
	is_stock_item=1,
):
	"""Find by SKU/ID or create; never overwrite an existing item master."""
	if frappe.db.exists("Item", item_code):
		return frappe.get_doc("Item", item_code)

	item = _new_item(
		item_code=item_code,
		item_name=item_name,
		company=company,
		warehouse=warehouse,
		description=product.get("body_html") or product.get("title"),
		item_group=_item_group(product.get("product_type")),
		has_variants=has_variants,
		attributes=attributes or [],
		variant_of=variant_of,
		weight=flt((variant or {}).get("weight") or 0),
		weight_uom=WEIGHT_TO_ERPNEXT_UOM.get((variant or {}).get("weight_unit") or ""),
		image=_image_src(product),
		is_stock_item=is_stock_item,
	)
	try:
		item.insert(ignore_permissions=True, ignore_mandatory=True)
		return item
	except Exception:
		log_error(
			"Shopify: could not create item",
			f"store={cfg.name} item={item_code} product={product.get('id')}\n{frappe.get_traceback()}",
		)
		return None


def _new_item(
	item_code,
	item_name,
	company,
	warehouse,
	description=None,
	item_group=None,
	has_variants=0,
	attributes=None,
	variant_of=None,
	weight=0,
	weight_uom=None,
	image=None,
	is_stock_item=1,
):
	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code[:140],
			"item_name": (item_name or item_code)[:140],
			"description": description,
			"item_group": item_group or _item_group(None),
			"is_stock_item": is_stock_item,
			"is_sales_item": 1,
			"stock_uom": "Nos",
			"has_variants": has_variants,
			"attributes": attributes or [],
			"variant_of": variant_of,
			"weight_per_unit": weight or 0,
			"weight_uom": weight_uom,
			"image": image,
			"item_defaults": [{"company": company, "default_warehouse": warehouse}],
		}
	)
	item.flags.from_shopify = True
	return item


def _item_group(product_type):
	if product_type and frappe.db.exists("Item Group", product_type):
		return product_type
	parent = get_root_of("Item Group")
	if product_type:
		try:
			group = frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": product_type,
					"parent_item_group": parent,
					"is_group": 0,
				}
			)
			group.insert(ignore_permissions=True)
			return group.name
		except Exception:
			pass

	# An item must live in a leaf group, not the root ("All Item Groups").
	for candidate in ("Products", "Consumable", "Services"):
		if frappe.db.exists("Item Group", candidate) and not frappe.db.get_value(
			"Item Group", candidate, "is_group"
		):
			return candidate
	return frappe.db.get_value("Item Group", {"is_group": 0}, "name") or parent


def _image_src(product):
	image = product.get("image") or {}
	return image.get("src") if isinstance(image, dict) else None


def _has_real_variants(options, variants) -> bool:
	if len(variants) > 1:
		return True
	if not options:
		return False
	values = options[0].get("values") or []
	return bool(values and "Default Title" not in values)


def _attributes_from_options(options):
	attributes = []
	for option in options[:3]:
		name = _ensure_attribute(option)
		if name:
			attributes.append({"attribute": name})
	return attributes


def _attributes_for_variant(variant, options):
	attributes = []
	for index, option in enumerate(options[:3]):
		name = option.get("name")
		value = variant.get(f"option{index + 1}")
		if name and value:
			attributes.append({"attribute": name, "attribute_value": value})
	return attributes


def _ensure_attribute(option):
	"""Create the ERPNext Item Attribute (and its values) Shopify uses."""
	name = (option.get("name") or "").strip()
	if not name:
		return None
	values = [value for value in (option.get("values") or []) if value]
	if not frappe.db.exists("Item Attribute", name):
		try:
			frappe.get_doc(
				{
					"doctype": "Item Attribute",
					"attribute_name": name,
					"item_attribute_values": [
						{"attribute_value": value, "abbr": value} for value in values
					],
				}
			).insert(ignore_permissions=True, ignore_mandatory=True)
		except Exception:
			log_error("Shopify: could not create Item Attribute", f"{name}\n{frappe.get_traceback()}")
			return None
		return name

	attribute = frappe.get_doc("Item Attribute", name)
	if attribute.numeric_values:
		return name
	existing = {(row.attribute_value or "").strip().lower() for row in attribute.item_attribute_values}
	added = False
	for value in values:
		if value.strip().lower() not in existing:
			attribute.append("item_attribute_values", {"attribute_value": value, "abbr": value})
			added = True
	if added:
		try:
			attribute.save(ignore_permissions=True)
		except Exception:
			log_error("Shopify: could not extend Item Attribute", f"{name}\n{frappe.get_traceback()}")
	return name


# ---------------------------------------------------------------------------
# Push: ERPNext item -> Shopify
# ---------------------------------------------------------------------------
def upload_item(doc, method=None):
	"""Item hook: push a new/updated item to every store that wants it."""
	if getattr(doc.flags, "from_shopify", False):
		return
	if frappe.flags.in_import or frappe.flags.in_patch:
		return

	stores = frappe.get_all(
		STORE_DOCTYPE,
		filters={"enabled": 1, "enable_erpnext_to_shopify": 1, "push_items": 1},
		fields=["name", "push_item_updates", "push_variants"],
	)
	if not stores:
		return

	for row in stores:
		cfg = StoreConfig(row.name)
		if cfg.dry_run:
			continue
		if method == "on_update" and not cfg.push_item_updates:
			continue
		if doc.has_variants and not cfg.push_variants:
			continue
		if doc.variant_of and not cfg.push_variants:
			continue
		try:
			push_item(cfg, doc)
		except Exception:
			log_error(
				"Shopify: item push failed",
				f"store={cfg.name} item={doc.name}\n{frappe.get_traceback()}",
				"Item",
				doc.name,
			)


def push_item(cfg: StoreConfig, item, budget: RequestBudget | None = None):
	"""Create or update the Shopify product that represents this item."""
	if not frappe.db.exists("Item", item.name):
		return None

	existing = map_for_item(cfg, item.name)
	if existing and existing.product_id:
		return _update_shopify_product(cfg, existing, item, budget=budget)
	return _create_shopify_product(cfg, item, budget=budget)


def _create_shopify_product(cfg: StoreConfig, item, budget=None):
	client = ShopifyClient(cfg, budget=budget)

	if item.has_variants:
		return _push_template(cfg, item, client)

	# A variant must be created under its template's product; create the whole
	# template (with all its variants) the first time, then add extras.
	if item.variant_of:
		template = frappe.get_doc("Item", item.variant_of)
		template_map = map_for_item(cfg, template.name)
		if not template_map:
			product = _push_template(cfg, template, client)
			if product:
				_log_push(cfg, item, product, "Created with template")
			return product
		return _add_variant(cfg, template_map, item, client)

	payload = {
		"product": {
			"title": item.item_name or item.name,
			"body_html": item.description or item.item_name,
			"product_type": item.item_group if frappe.db.exists("Item Group", item.item_group) else None,
			"status": "active" if cfg.store.push_new_item_as_active else "draft",
			"variants": [
				{
					"sku": item.name,
					"price": _price(item),
					"weight": flt(item.weight_per_unit or 0),
					"weight_unit": _shopify_weight_unit(item.weight_uom),
					"inventory_management": "shopify" if cint(item.is_stock_item) else None,
				}
			],
		}
	}
	response = client.post("products.json", payload) or {}
	product = response.get("product") or {}
	variant = (product.get("variants") or [{}])[0]
	remember_item(
		cfg,
		item.name,
		product.get("id"),
		variant_id=variant.get("id"),
		sku=item.name,
		inventory_item_id=variant.get("inventory_item_id"),
	)
	_log_push(cfg, item, product, "Created")
	return product


def _push_template(cfg: StoreConfig, template, client):
	"""Create a Shopify product with its options and every ERPNext variant."""
	options = []
	option_values = {}
	for row in (template.attributes or [])[:3]:
		option = row.attribute
		values = frappe.get_all(
			"Item Attribute Value",
			filters={"parent": option},
			pluck="attribute_value",
			order_by="idx asc",
		)
		if not values:
			continue
		option_values[option] = values
		options.append({"name": option, "values": values})

	variant_items = frappe.get_all(
		"Item",
		filters={"variant_of": template.name, "disabled": 0},
		fields=["name", "item_name", "weight_per_unit", "weight_uom", "is_stock_item"],
		order_by="creation asc",
	)

	variants = []
	if variant_items:
		for variant_item in variant_items:
			doc = frappe.get_doc("Item", variant_item.name)
			payload = {
				"sku": doc.name,
				"price": _price(doc),
				"weight": flt(doc.weight_per_unit or 0),
				"weight_unit": _shopify_weight_unit(doc.weight_uom),
				"inventory_management": "shopify" if cint(doc.is_stock_item) else None,
			}
			for index, option in enumerate([option["name"] for option in options]):
				value = next(
					(row.attribute_value for row in doc.attributes if row.attribute == option),
					None,
				)
				if value:
					payload[f"option{index + 1}"] = value
			variants.append(payload)
	else:
		variants.append(
			{
				"sku": template.name,
				"price": _price(template),
				"weight": flt(template.weight_per_unit or 0),
				"weight_unit": _shopify_weight_unit(template.weight_uom),
				"inventory_management": "shopify" if cint(template.is_stock_item) else None,
			}
		)

	body = {
		"title": template.item_name or template.name,
		"body_html": template.description or template.item_name,
		"status": "active" if cfg.store.push_new_item_as_active else "draft",
		"options": options or [{"name": "Title", "values": ["Default Title"]}],
		"variants": variants,
	}
	response = client.post("products.json", {"product": body}) or {}
	product = response.get("product") or {}

	remember_item(cfg, template.name, product.get("id"), has_variants=1)
	for saved in product.get("variants") or []:
		item_code = saved.get("sku")
		if item_code:
			remember_item(
				cfg,
				item_code,
				product.get("id"),
				variant_id=saved.get("id"),
				sku=item_code,
				variant_of=template.name,
				inventory_item_id=saved.get("inventory_item_id"),
			)
	_log_push(cfg, template, product, "Created")
	return product


def _update_shopify_product(cfg: StoreConfig, mapping, item, budget=None):
	client = ShopifyClient(cfg, budget=budget)
	payload = {
		"product": {
			"id": mapping.product_id,
			"title": item.item_name or item.name,
			"body_html": item.description or item.item_name,
			"status": "draft" if item.disabled else "active",
		}
	}
	try:
		response = client.put(f"products/{mapping.product_id}.json", payload) or {}
	except ShopifyClientError as exc:
		if exc.status_code == 404:
			# Product deleted in Shopify: create a fresh one and remap.
			return _create_shopify_product(cfg, item, budget=budget)
		raise
	product = response.get("product") or {}
	_log_push(cfg, item, product, "Updated")
	return product


def _add_variant(cfg: StoreConfig, template_map, item, client):
	product = client.product(template_map.product_id)
	if not product:
		return None

	options = product.get("options") or []
	values = {}
	for row in item.attributes or []:
		values[row.attribute] = row.attribute_value

	variant = {
		"sku": item.name,
		"price": _price(item),
		"weight": flt(item.weight_per_unit or 0),
		"weight_unit": _shopify_weight_unit(item.weight_uom),
		"inventory_management": "shopify" if cint(item.is_stock_item) else None,
	}
	for index, option in enumerate(options[:3]):
		variant[f"option{index + 1}"] = values.get(option.get("name")) or "Default"

	# Shopify wants the full variant list on product update; append ours if it
	# is not there yet, otherwise update in place.
	variants = product.get("variants") or []
	existing = next((v for v in variants if v.get("sku") == item.name), None)
	if existing:
		existing.update(variant)
	else:
		variants.append(variant)

	response = client.put(f"products/{template_map.product_id}.json", {"product": {"id": template_map.product_id, "variants": variants}}) or {}
	product = response.get("product") or {}
	saved = next((v for v in product.get("variants") or [] if v.get("sku") == item.name), None)
	if saved:
		remember_item(
			cfg,
			item.name,
			template_map.product_id,
			variant_id=saved.get("id"),
			sku=item.name,
			variant_of=template_map.item_code,
			inventory_item_id=saved.get("inventory_item_id"),
		)
	_log_push(cfg, item, product, "Variant synced")
	return product


def _price(item) -> float:
	return money(item.get(ITEM_SELLING_RATE_FIELD) or item.get("standard_rate") or 0)


def _shopify_weight_unit(uom):
	for shopify_uom, erpnext_uom in WEIGHT_TO_ERPNEXT_UOM.items():
		if erpnext_uom == uom:
			return shopify_uom
	return None


def _log_push(cfg, item, product, action):
	try:
		frappe.get_doc(
			{
				"doctype": "Shopify Sync Run",
				"store": cfg.name,
				"mode": "Items",
				"trigger": "Hook",
				"status": "Success",
				"started_on": now(),
				"finished_on": now(),
				"items_pushed": 1,
				"note": f"{action}: {item.name} -> product {product.get('id')}",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		pass


# ---------------------------------------------------------------------------
# Webhook: products/update
# ---------------------------------------------------------------------------
def handle_webhook_product(payload, store=None, request_id=None):
	"""Refresh mappings for a changed product (never rewrites an item master)."""
	cfg = config(store) if store else _store_for_product(payload)
	if not cfg or not cfg.enabled or not cfg.pull_enabled or not cfg.pull_products:
		return {"skipped": "product pull disabled"}

	product_id = payload.get("id")
	if not product_id:
		return {"skipped": "no product id"}

	with Run(cfg.name, mode="Items", trigger="Webhook") as run:
		client = ShopifyClient(cfg)
		product = client.product(product_id)
		if not product:
			return {"skipped": "product not found"}
		company_cfg = cfg.default_company()
		created = make_items_from_product(cfg, product, company_cfg, run=run)
		run.bump("items_created", created)
	return {"product": product_id, "created": created}


def _store_for_product(payload):
	"""Best-effort store lookup when the callback did not carry one."""
	product_id = str(payload.get("id") or "")
	if product_id:
		store = frappe.db.get_value(ITEM_MAP_DOCTYPE, {"product_id": product_id}, "store")
		if store:
			return StoreConfig(store)
	return None
