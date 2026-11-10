"""Whitelisted endpoints used by buttons, desk calls and bench execute."""

import json

import frappe
from frappe import _
from frappe.utils import cint

from . import constants, picker
from .client import RequestBudget, ShopifyClient
from .config import StoreConfig, legacy_store_name
from .constants import WEBHOOK_TOPICS
from .shopify_integration.doctype.shopify_store.shopify_store import build_webhook_url
from .exceptions import ShopifyIntegrationError
from .utils import log_error, now as now_dt

WRITE_ROLES = ("System Manager",)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
@frappe.whitelist()
def health():
	return {
		"app": constants.APP,
		"build": constants.BUILD,
		"stores": frappe.db.count("Shopify Store"),
		"enabled_stores": frappe.db.count("Shopify Store", {"enabled": 1}),
	}


@frappe.whitelist()
def test_connection(store):
	_check_store_permission(store)
	cfg = StoreConfig(store)
	client = ShopifyClient(cfg)
	try:
		shop = client.shop()
	except ShopifyIntegrationError as exc:
		frappe.db.set_value(
			"Shopify Store",
			store,
			{"last_connection_on": now_dt(), "last_connection_status": f"Failed: {exc}"},
			update_modified=False,
		)
		frappe.throw(str(exc))

	result = {
		"shop": shop.get("name") or shop.get("myshopify_domain"),
		"domain": shop.get("myshopify_domain"),
		"currency": shop.get("currency"),
		"country": shop.get("country_name"),
		"plan": shop.get("plan_display_name"),
	}
	frappe.db.set_value(
		"Shopify Store",
		store,
		{"last_connection_on": now_dt(), "last_connection_status": json.dumps(result, default=str)[:1000]},
		update_modified=False,
	)
	return result


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------
@frappe.whitelist()
def register_webhooks(store):
	_check_store_permission(store)
	store_doc = frappe.get_doc("Shopify Store", store)
	address = build_webhook_url(store_doc)
	if not address:
		frappe.throw(_("Save the store before registering webhooks (the webhook key is missing)."))

	cfg = StoreConfig(store)
	client = ShopifyClient(cfg)

	existing = (client.get("webhooks.json") or {}).get("webhooks") or []
	prefix = f"/api/method/{constants.CALLBACK_METHOD}/{store}/"
	for webhook in existing:
		if address in (webhook.get("address"),) or prefix in (webhook.get("address") or ""):
			try:
				client.delete(f"webhooks/{webhook['id']}.json")
			except ShopifyIntegrationError as exc:
				log_error("Shopify: could not remove old webhook", f"{webhook.get('id')}\n{exc}")

	rows = []
	for topic in WEBHOOK_TOPICS:
		try:
			response = client.post(
				"webhooks.json",
				{"webhook": {"topic": topic, "address": address, "format": "json"}},
			)
		except ShopifyIntegrationError as exc:
			log_error("Shopify: webhook registration failed", f"{store} / {topic}\n{exc}")
			continue
		webhook = (response or {}).get("webhook") or {}
		if webhook.get("id"):
			rows.append(
				{
					"topic": topic,
					"webhook_id": str(webhook["id"]),
					"address": address,
					"active": 1,
				}
			)

	store_doc.reload()
	store_doc.set("webhooks", [])
	for row in rows:
		store_doc.append("webhooks", row)
	store_doc.flags.ignore_validate = True
	store_doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"address": address, "registered": len(rows), "topics": [row["topic"] for row in rows]}


@frappe.whitelist()
def webhook_url(store):
	_check_store_permission(store, write=False)
	return build_webhook_url(frappe.get_doc("Shopify Store", store))


# ---------------------------------------------------------------------------
# Locations / warehouse mapping
# ---------------------------------------------------------------------------
@frappe.whitelist()
def fetch_locations(store):
	_check_store_permission(store, write=False)
	from .inventory import fetch_locations as _fetch

	return _fetch(store)


@frappe.whitelist()
def provision_warehouse_mapping(store):
	"""Add a mapping row per (company, Shopify location) using the default warehouse."""
	_check_store_permission(store)
	from .inventory import fetch_locations as _fetch

	store_doc = frappe.get_doc("Shopify Store", store)
	locations = _fetch(store)
	if not locations:
		frappe.throw(_("Shopify returned no locations for this store."))

	added = 0
	skipped = 0
	for company_row in store_doc.companies or []:
		if not company_row.company or not company_row.default_warehouse:
			continue
		used_warehouses = {
			row.warehouse
			for row in store_doc.warehouse_mapping or []
			if row.company == company_row.company
		}
		for location in locations:
			exists = any(
				row.company == company_row.company
				and str(row.shopify_location_id) == str(location["id"])
				for row in store_doc.warehouse_mapping or []
			)
			if exists:
				continue
			# One warehouse cannot represent two locations: the same stock would
			# be advertised at both. Skip and let the user map a real one.
			if company_row.default_warehouse in used_warehouses:
				skipped += 1
				continue
			store_doc.append(
				"warehouse_mapping",
				{
					"company": company_row.company,
					"shopify_location_id": str(location["id"]),
					"warehouse": company_row.default_warehouse,
				},
			)
			used_warehouses.add(company_row.default_warehouse)
			added += 1

	if added:
		store_doc.save(ignore_permissions=True)
		frappe.db.commit()
	return {"locations": locations, "added": added, "skipped": skipped}


# ---------------------------------------------------------------------------
# Manual runs
# ---------------------------------------------------------------------------
@frappe.whitelist()
def sync_now(store, mode="orders", background=0):
	"""Run one store's sync on demand (orders / inventory / old orders)."""
	_check_store_permission(store)
	from .tasks import push_inventory_locked, poll_store_locked, run_locked

	mode = (mode or "orders").lower()
	if mode in ("orders", "poll"):
		fn, kind, queued = poll_store_locked, "manual-poll", "shopify_integration.tasks.manual_poll"
	elif mode in ("inventory", "stock"):
		fn, kind, queued = push_inventory_locked, "manual-inventory", "shopify_integration.tasks.manual_inventory"
	else:
		frappe.throw(_("Unknown sync mode {0}.").format(mode))

	if cint(background):
		frappe.enqueue(
			queued,
			queue="long",
			timeout=1800,
			is_async=True,
			store=store,
		)
		return {"queued": True, "store": store, "mode": mode}
	if mode in ("inventory", "stock"):
		return run_locked(kind, store, fn, force=True)
	return run_locked(kind, store, fn)


@frappe.whitelist()
def import_order(store, order_id, force=1):
	"""Pull one order by id (the escape hatch for a missed webhook).

	`force` defaults to 1: a human asked for this order, so the pull cutoff must
	not silently refuse it. The retry queue passes force=1 for the same reason.
	"""
	_check_store_permission(store)
	from .order import import_order_by_id
	from .tasks import run_locked
	from .utils import Run

	def worker(store_name):
		cfg = StoreConfig(store_name)
		with Run(store_name, mode="Orders", trigger="Manual") as run:
			so, result = import_order_by_id(cfg, order_id, run=run, force=cint(force))
		return {"sales_order": getattr(so, "name", None), "result": result}

	return run_locked("manual-order", store, worker)


@frappe.whitelist()
def retry_import(store, order_id):
	"""Re-run one queued import failure immediately (used by the report)."""
	_check_store_permission(store)
	from .order import import_order_by_id
	from .retry import resolve
	from .tasks import run_locked
	from .utils import Run

	def worker(store_name):
		from .config import StoreConfig

		cfg = StoreConfig(store_name)
		with Run(store_name, mode="Orders", trigger="Retry") as run:
			so, result = import_order_by_id(cfg, order_id, run=run, force=True)
			if so:
				resolve(store_name, order_id, so.name)
		return {"sales_order": getattr(so, "name", None), "result": result}

	return run_locked("manual-order", store, worker)


@frappe.whitelist()
def sync_old_orders(store, from_datetime, to_datetime):
	"""Import a created_at range now, instead of waiting for the scheduler."""
	_check_store_permission(store)
	from .order import sync_old_orders as _sync
	from .tasks import run_locked
	from .utils import Run

	def worker(store_name):
		cfg = StoreConfig(store_name)
		with Run(store_name, mode="Old Orders", trigger="Manual") as run:
			return _sync(cfg, from_datetime, to_datetime, run)

	return run_locked("manual-old-orders", store, worker)


@frappe.whitelist()
def push_inventory(store):
	_check_store_permission(store)
	from .inventory import push_inventory as _push
	from .tasks import run_locked
	from .utils import Run

	def worker(store_name):
		cfg = StoreConfig(store_name)
		with Run(store_name, mode="Inventory", trigger="Manual") as run:
			return {"pushed": _push(cfg, run, force=True)}

	return run_locked("manual-inventory", store, worker)


@frappe.whitelist()
def push_item(item_code, store=None):
	"""Push one ERPNext item to a store (used by the Item form / bench)."""
	if not frappe.has_permission("Item", "read", doc=item_code):
		frappe.throw(_("Not permitted to read this Item."), frappe.PermissionError)
	from .config import StoreConfig
	from .product import push_item as _push

	item = frappe.get_doc("Item", item_code)
	stores = [StoreConfig(store)] if store else [
		StoreConfig(row.name)
		for row in frappe.get_all(
			"Shopify Store",
			filters={"enabled": 1, "enable_erpnext_to_shopify": 1, "push_items": 1},
			fields=["name"],
		)
	]
	results = {}
	for cfg in stores:
		product = _push(cfg, item)
		results[cfg.name] = (product or {}).get("id")
	return results


@frappe.whitelist()
def push_customer(customer, store=None):
	if not frappe.has_permission("Customer", "read", doc=customer):
		frappe.throw(_("Not permitted to read this Customer."), frappe.PermissionError)
	from .config import StoreConfig
	from .customer import push_customer as _push

	store = store or legacy_store_name()
	if not store:
		frappe.throw(_("No Shopify Store configured."))
	return _push(frappe.get_doc("Customer", customer), StoreConfig(store))


# ---------------------------------------------------------------------------
# Webhook replay
# ---------------------------------------------------------------------------
@frappe.whitelist()
def replay_webhook(log_name):
	"""Re-run a failed webhook delivery from its stored payload."""
	if "System Manager" not in frappe.get_roles():
		frappe.throw(_("Only System Manager can replay webhooks."), frappe.PermissionError)
	log = frappe.get_doc("Shopify Webhook Log", log_name)
	if not log.request_data:
		frappe.throw(_("This log has no stored payload."))
	from .constants import WEBHOOK_HANDLERS
	from .webhook import run_handler

	handler = WEBHOOK_HANDLERS.get(log.topic)
	if not handler:
		frappe.throw(_("No handler for topic {0}.").format(log.topic))

	payload = json.loads(log.request_data)
	frappe.db.set_value("Shopify Webhook Log", log_name, {"status": "Running", "traceback": ""})
	return run_handler(handler, payload, log.store, log_name)


# ---------------------------------------------------------------------------
# Catalogue picker (the "browse Shopify products" dialogs)
# ---------------------------------------------------------------------------
CATALOGUE_TTL_SECONDS = 600
CATALOGUE_BUDGET = 200


@frappe.whitelist()
def picker_stores():
	"""Enabled stores, for the picker's store selector."""
	_check_picker_permission()
	return frappe.get_all(
		constants.STORE_DOCTYPE,
		filters={"enabled": 1},
		fields=["name", "store_name", "shop_domain"],
		order_by="creation asc",
	)


@frappe.whitelist()
def search_catalogue(store=None, term=None, mode="products", status=None, refresh=0, limit=None):
	"""Products (or flattened variants) for the desk picker dialogs.

	Shopify's REST API cannot search products by text, so the catalogue is
	downloaded once per store and cached for `CATALOGUE_TTL_SECONDS`; the term
	is then matched in memory. `refresh=1` re-reads it from Shopify.
	"""
	_check_picker_permission()
	store_name = _resolve_store(store)
	cfg = StoreConfig(store_name)

	payload = _catalogue(cfg, status=status, refresh=cint(refresh))
	if (mode or "products").strip().lower() == "variants":
		matches = picker.search_variants(payload["products"], term)
	else:
		matches = picker.search_products(payload["products"], term)
	rows, total = picker.clamp(matches, limit)

	return {
		"store": store_name,
		"mode": mode,
		"rows": rows,
		"total_matches": total,
		"fetched": len(payload["products"]),
		"truncated": payload["truncated"],
		"cached_on": payload["cached_on"],
	}


@frappe.whitelist()
def map_item(
	item_code,
	store=None,
	product_id=None,
	variant_id=None,
	sku=None,
	inventory_item_id=None,
	has_variants=0,
	force=0,
):
	"""Create/refresh the Shopify Item Map row for an ERPNext item.

	Used by the Item form and by the picker on `Shopify Item Map`. Never
	silently re-points a product/variant that another item already owns.
	"""
	if not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item {0} does not exist.").format(item_code))
	if not frappe.has_permission(constants.ITEM_MAP_DOCTYPE, "write"):
		frappe.throw(_("Not permitted to change Shopify Item Map rows."), frappe.PermissionError)

	store_name = _resolve_store(store)
	cfg = StoreConfig(store_name)
	product_id = str(product_id or "").strip()
	variant_id = str(variant_id or "").strip()
	if not product_id:
		frappe.throw(_("A Shopify Product ID is required."))

	owner = _mapping_for(cfg.name, product_id, variant_id)
	if owner and owner[1] != item_code and not cint(force):
		frappe.throw(
			_("Shopify product {0}{1} is already mapped to Item {2}. Open that mapping, or confirm "
			  "to move it to {3}.").format(
				product_id,
				" / variant {0}".format(variant_id) if variant_id else "",
				owner[1],
				item_code,
			)
		)

	# Keep the template link of an existing row: the picker knows the product and
	# variant ids, but not which ERPNext template the variant belongs to.
	existing_variant_of = None
	if owner:
		existing_variant_of = frappe.db.get_value(constants.ITEM_MAP_DOCTYPE, owner[0], "variant_of")

	from .product import remember_item

	doc = remember_item(
		cfg,
		item_code,
		product_id,
		variant_id=variant_id,
		sku=sku or None,
		has_variants=cint(has_variants),
		variant_of=existing_variant_of,
		inventory_item_id=inventory_item_id or None,
	)
	if not doc:
		frappe.throw(_("Could not store the mapping for {0}.").format(item_code))
	frappe.db.commit()
	return {
		"name": doc.name,
		"item_code": doc.item_code,
		"store": doc.store,
		"product_id": doc.product_id,
		"variant_id": doc.variant_id,
		"sku": doc.sku,
	}


@frappe.whitelist()
def item_mappings(item_code):
	"""Every store mapping of one item (shown on the Item form)."""
	if not frappe.has_permission(constants.ITEM_MAP_DOCTYPE, "read"):
		frappe.throw(_("Not permitted to read Shopify Item Map rows."), frappe.PermissionError)
	return frappe.get_all(
		constants.ITEM_MAP_DOCTYPE,
		filters={"item_code": item_code},
		fields=["name", "store", "product_id", "variant_id", "sku", "has_variants", "item_synced_on"],
		order_by="creation asc",
	)


def _catalogue(cfg: StoreConfig, status=None, refresh=False):
	"""Normalised product list for a store, cached per status filter."""
	status = (status or "").strip().lower()
	if status in ("", "all", "any"):
		status = ""

	key = "shopify_integration:catalogue:{0}:{1}".format(cfg.name, status or "any")
	cache = _cache_backend()
	if cache and not cint(refresh):
		try:
			cached = cache.get_value(key)
			if cached:
				return cached
		except Exception:
			pass

	client = ShopifyClient(cfg, budget=RequestBudget(min(cfg.request_budget, CATALOGUE_BUDGET)))
	params = {"fields": picker.FIELDS}
	if status:
		params["status"] = status

	products = []
	truncated = False
	for raw in client.paginate("products.json", "products", params=params):
		products.append(picker.normalize_product(raw))
		if len(products) >= picker.MAX_PRODUCTS:
			truncated = True
			break

	payload = {
		"products": picker.sort_products(products),
		"truncated": truncated,
		"cached_on": now_dt(),
	}
	if cache:
		try:
			cache.set_value(key, payload, expires_in_sec=CATALOGUE_TTL_SECONDS)
		except Exception:
			pass
	return payload


def _cache_backend():
	"""`frappe.cache` is callable on older versions and an object on newer ones."""
	try:
		cache = frappe.cache
		return cache() if callable(cache) else cache
	except Exception:
		return None


def _resolve_store(store=None) -> str:
	store = (store or "").strip()
	if store:
		if not frappe.db.exists(constants.STORE_DOCTYPE, store):
			frappe.throw(_("Shopify Store {0} does not exist.").format(store))
		return store
	rows = frappe.get_all(
		constants.STORE_DOCTYPE,
		filters={"enabled": 1},
		pluck="name",
		order_by="creation asc",
		limit=1,
	)
	if not rows:
		frappe.throw(_("No enabled Shopify Store exists. Create one first."))
	return rows[0]


def _mapping_for(store, product_id, variant_id=None):
	"""(map name, item_code) already holding this product/variant, or None."""
	rows = frappe.db.sql(
		"select name, item_code from `tabShopify Item Map` where store=%s and product_id=%s "
		"and ifnull(variant_id, '')=%s limit 1",
		(store, str(product_id), str(variant_id or "")),
	)
	return rows[0] if rows else None


def _check_picker_permission():
	"""Reading the catalogue is allowed to anyone who may read item mappings."""
	if "System Manager" in frappe.get_roles():
		return
	if frappe.has_permission(constants.ITEM_MAP_DOCTYPE, "read"):
		return
	frappe.throw(_("Not permitted to browse the Shopify catalogue."), frappe.PermissionError)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _check_store_permission(store, write=True):
	if write and "System Manager" not in frappe.get_roles():
		frappe.throw(_("Only System Manager can change this Shopify Store."), frappe.PermissionError)
	if not frappe.has_permission("Shopify Store", "write" if write else "read", doc=store):
		frappe.throw(_("Not permitted for this Shopify Store."), frappe.PermissionError)
