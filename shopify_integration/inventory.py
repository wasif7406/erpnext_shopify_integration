"""ERPNext stock -> Shopify inventory levels.

One row per store <-> item (`Shopify Item Map`) plus one row per ERPNext
warehouse <-> Shopify location (`Shopify Store Warehouse`). A store can be
linked to several companies, so the mapping rows carry `company` and only the
configured company's warehouses are pushed.

Counts are pushed only for items whose `Bin` changed since the mapping's
`inventory_synced_on`, so a store with thousands of items does not spend the
whole request budget every run.
"""

import frappe
from frappe.utils import cint, get_datetime, now_datetime, add_to_date

from .client import RequestBudget, ShopifyClient
from .config import StoreConfig
from .constants import ITEM_MAP_DOCTYPE, STORE_DOCTYPE
from .exceptions import ShopifyClientError
from .utils import Run, log_error, now, set_values

EPOCH = "1970-01-01 00:00:00"


def push_inventory(cfg: StoreConfig, run: Run, force: bool = False):
	"""Push every changed item/location pair for one store."""
	if not cfg.enabled or not cfg.push_enabled or not cfg.push_inventory or cfg.dry_run:
		return 0

	if not force and not _due(cfg):
		return 0

	client = ShopifyClient(cfg, budget=run.budget if run else RequestBudget(cfg.request_budget))
	pushed = 0
	checked = 0
	synced_maps = set()
	# Stamp with the run start, not the push time: one item can map to several
	# locations, and stamping after the first would hide it from the second.
	started = now()

	for company_cfg in cfg.companies:
		if not company_cfg.enabled:
			continue
		mappings = [
			row
			for row in cfg.store.warehouse_mapping or []
			if row.company == company_cfg.company and row.shopify_location_id not in (None, "")
		]
		for mapping in mappings:
			warehouses = _leaf_warehouses(mapping.warehouse)
			rows = _changed_items(cfg.name, warehouses)
			for row in rows:
				checked += 1
				try:
					available = cint(round(float(row.actual_qty or 0) - float(row.reserved_qty or 0)))
					inventory_item_id = row.inventory_item_id or _inventory_item_id(
						cfg, client, row.variant_id, row.item_code, mapping
					)
					if not inventory_item_id:
						continue
					client.post(
						"inventory_levels/set.json",
						{
							# Send the ids as strings: Shopify accepts them and a
							# 20-digit id would lose precision through cint().
							"location_id": str(mapping.shopify_location_id),
							"inventory_item_id": str(inventory_item_id),
							"available": available,
						},
					)
					frappe.db.set_value(
						ITEM_MAP_DOCTYPE,
						row.map_name,
						{"inventory_item_id": str(inventory_item_id)},
						update_modified=False,
					)
					synced_maps.add(row.map_name)
					pushed += 1
					if pushed % 25 == 0:
						frappe.db.commit()
				except ShopifyClientError as exc:
					log_error(
						"Shopify: inventory push failed",
						f"{cfg.name} / {row.item_code}\n{exc}",
					)
					if run:
						run.fail("inventory", exc, "Item", row.item_code)
				except Exception as exc:
					log_error(
						"Shopify: inventory push failed",
						f"{cfg.name} / {row.item_code}\n{frappe.get_traceback()}",
					)
					if run:
						run.fail("inventory", exc, "Item", row.item_code)

	for map_name in synced_maps:
		frappe.db.set_value(
			ITEM_MAP_DOCTYPE, map_name, "inventory_synced_on", started, update_modified=False
		)
	frappe.db.commit()
	set_values(STORE_DOCTYPE, cfg.name, {"last_inventory_push_on": now()}, commit=True)
	if run:
		run.bump("inventory_pushed", pushed)
		run.note(f"inventory checked {checked}")
	return pushed


def _due(cfg: StoreConfig) -> bool:
	minutes = cint(cfg.store.inventory_sync_frequency) or 60
	last = get_datetime(cfg.store.last_inventory_push_on) if cfg.store.last_inventory_push_on else None
	if not last or last.year <= 1970:
		return True
	return now_datetime() >= add_to_date(last, minutes=minutes)


def _leaf_warehouses(warehouse):
	"""A leaf warehouse only; group warehouses are expanded to their leaves."""
	if not warehouse:
		return []
	meta = frappe.db.get_value("Warehouse", warehouse, ["is_group"], as_dict=True)
	if not meta:
		return []
	if not meta.is_group:
		return [warehouse]
	from frappe.utils.nestedset import get_descendants_of

	return list(get_descendants_of("Warehouse", warehouse)) + [warehouse]


def _changed_items(store: str, warehouses):
	if not warehouses:
		return []
	placeholders = ", ".join(["%s"] * len(warehouses))
	return frappe.db.sql(
		f"""
		select m.name as map_name, m.item_code, m.variant_id, m.inventory_item_id,
		       sum(b.actual_qty) as actual_qty,
		       sum(b.reserved_qty) as reserved_qty
		from `tab{ITEM_MAP_DOCTYPE}` m
		join `tabBin` b on b.item_code = m.item_code
		where m.store = %s
		  and m.item_code is not null and m.item_code != ''
		  and b.warehouse in ({placeholders})
		group by m.name, m.item_code, m.variant_id, m.inventory_item_id, m.inventory_synced_on
		having max(b.modified) > ifnull(m.inventory_synced_on, '1970-01-01 00:00:00')
		   and max(b.modified) > %s
		limit 5000
		""",
		tuple([store] + list(warehouses) + [EPOCH]),
		as_dict=True,
	)


def _inventory_item_id(cfg, client, variant_id, item_code, mapping):
	"""Fetch (once) and cache Shopify's inventory_item_id for a variant."""
	if not variant_id:
		return None
	try:
		variant = client.variant(variant_id)
	except ShopifyClientError:
		return None
	inventory_item_id = variant.get("inventory_item_id")
	if inventory_item_id:
		name = frappe.db.get_value(
			ITEM_MAP_DOCTYPE,
			{"store": cfg.name, "item_code": item_code, "variant_id": str(variant_id)},
			"name",
		)
		if name:
			frappe.db.set_value(
				ITEM_MAP_DOCTYPE, name, "inventory_item_id", str(inventory_item_id), update_modified=False
			)
	return inventory_item_id


# ---------------------------------------------------------------------------
# Locations helper (used by the "Fetch Shopify Locations" button)
# ---------------------------------------------------------------------------
def fetch_locations(store: str):
	"""Read the shop's locations; does not write the mapping table."""
	cfg = StoreConfig(store)
	client = ShopifyClient(cfg)
	return [
		{
			"id": str(location.get("id")),
			"name": location.get("name"),
			"active": bool(location.get("active")),
			"city": location.get("city"),
			"country": location.get("country_name") or location.get("country_code"),
		}
		for location in client.locations()
	]


def probe_inventory(store: str):
	"""Dry check used by `api.test_connection`: how many items would push."""
	cfg = StoreConfig(store)
	result = {}
	for company_cfg in cfg.companies:
		mappings = [
			row
			for row in cfg.store.warehouse_mapping or []
			if row.company == company_cfg.company
		]
		result[company_cfg.company] = {
			"locations": len(mappings),
			"changed_items": sum(
				len(_changed_items(cfg.name, _leaf_warehouses(row.warehouse))) for row in mappings
			),
		}
	return result
