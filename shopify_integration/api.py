"""Whitelisted endpoints used by buttons, desk calls and bench execute."""

import json

import frappe
from frappe import _
from frappe.utils import cint

from . import constants
from .client import ShopifyClient
from .config import StoreConfig, legacy_store_name
from .constants import WEBHOOK_TOPICS
from .doctype.shopify_store.shopify_store import build_webhook_url
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
	for company_row in store_doc.companies or []:
		if not company_row.company or not company_row.default_warehouse:
			continue
		for location in locations:
			exists = any(
				row.company == company_row.company
				and str(row.shopify_location_id) == str(location["id"])
				for row in store_doc.warehouse_mapping or []
			)
			if exists:
				continue
			store_doc.append(
				"warehouse_mapping",
				{
					"company": company_row.company,
					"shopify_location_id": str(location["id"]),
					"warehouse": company_row.default_warehouse,
				},
			)
			added += 1

	if added:
		store_doc.save(ignore_permissions=True)
		frappe.db.commit()
	return {"locations": locations, "added": added}


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
	return run_locked(kind, store, fn)


@frappe.whitelist()
def import_order(store, order_id):
	"""Pull one order by id (the escape hatch for a missed webhook)."""
	_check_store_permission(store)
	from .order import import_order_by_id
	from .tasks import run_locked
	from .utils import Run

	def worker(store_name):
		cfg = StoreConfig(store_name)
		with Run(store_name, mode="Orders", trigger="Manual") as run:
			so, result = import_order_by_id(cfg, order_id, run=run)
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
# Helpers
# ---------------------------------------------------------------------------
def _check_store_permission(store, write=True):
	if write and "System Manager" not in frappe.get_roles():
		frappe.throw(_("Only System Manager can change this Shopify Store."), frappe.PermissionError)
	if not frappe.has_permission("Shopify Store", "write" if write else "read", doc=store):
		frappe.throw(_("Not permitted for this Shopify Store."), frappe.PermissionError)
