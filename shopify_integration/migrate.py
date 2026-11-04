"""One-way migration from the legacy `ecommerce_integrations` connector.

It is deliberately **non-destructive**: the old app is never modified, and this
module only creates the new store document plus the mapping rows needed to keep
existing links. Both sync directions stay OFF after migration — the merchant
turns them on when the old connector has been disabled.

Run with:

    bench --site <site> execute shopify_integration.migrate.import_legacy_settings
    bench --site <site> execute shopify_integration.migrate.import_legacy_settings --kwargs "{'force': 1}"
"""

import frappe
from frappe.utils import cint

from .constants import (
	ITEM_MAP_DOCTYPE,
	CUSTOMER_MAP_DOCTYPE,
	LEGACY_ITEM_DOCTYPE,
	LEGACY_SETTING_DOCTYPE,
	STORE_DOCTYPE,
)
from .config import normalize_shop_domain
from .utils import log_error


def import_legacy_settings(force: int = 0):
	if not frappe.db.exists("DocType", LEGACY_SETTING_DOCTYPE):
		return {"skipped": f"{LEGACY_SETTING_DOCTYPE} does not exist on this site"}

	setting = frappe.get_doc(LEGACY_SETTING_DOCTYPE)
	domain = normalize_shop_domain(getattr(setting, "shopify_url", ""))
	if not domain:
		return {"skipped": "legacy Shopify Setting has no shop URL"}
	if not getattr(setting, "company", None):
		return {"skipped": "legacy Shopify Setting has no Company selected"}

	existing = frappe.db.get_value(STORE_DOCTYPE, {"shop_domain": domain}, "name")
	if existing and not cint(force):
		created = False
		store_name = existing
	else:
		store_name = existing or _create_store(setting, domain)
		_update_store(store_name, setting)
		created = not existing

	backfill = _backfill_links(store_name, domain)
	return {"store": store_name, "created": created, "backfill": backfill}


def status():
	if not frappe.db.exists("DocType", LEGACY_SETTING_DOCTYPE):
		return {"legacy_setting": False}
	domain = normalize_shop_domain(getattr(frappe.get_doc(LEGACY_SETTING_DOCTYPE), "shopify_url", ""))
	store = frappe.db.get_value(STORE_DOCTYPE, {"shop_domain": domain}, "name")
	return {
		"legacy_setting": True,
		"shop_domain": domain,
		"store": store,
		"legacy_items": frappe.db.count(LEGACY_ITEM_DOCTYPE, {"integration": "shopify"})
		if frappe.db.exists("DocType", LEGACY_ITEM_DOCTYPE)
		else 0,
		"item_maps": frappe.db.count(ITEM_MAP_DOCTYPE, {"store": store}) if store else 0,
		"customer_maps": frappe.db.count(CUSTOMER_MAP_DOCTYPE, {"store": store}) if store else 0,
		"orders_without_store": frappe.db.sql(
			"select count(*) from `tabSales Order` where ifnull(shopify_order_id,'') != '' and ifnull(custom_shopify_store,'') = ''"
		)[0][0],
	}


# ---------------------------------------------------------------------------
# Store creation / update
# ---------------------------------------------------------------------------
def _create_store(setting, domain):
	store_name = domain.replace(".myshopify.com", "")[:140] or domain
	name = store_name
	index = 2
	while frappe.db.exists(STORE_DOCTYPE, name):
		name = f"{store_name}-{index}"
		index += 1

	doc = frappe.get_doc(
		{
			"doctype": STORE_DOCTYPE,
			"store_name": name,
			"shop_domain": domain,
			"enabled": 1,
			"authentication_method": getattr(setting, "authentication_method", "Static Token") or "Static Token",
			"api_version": "2025-10",
			"enable_shopify_to_erpnext": 0,
			"enable_erpnext_to_shopify": 0,
			"companies": [_company_row(setting)],
			"taxes": _tax_rows(setting),
			"warehouse_mapping": _warehouse_rows(setting),
			"push_items": cint(getattr(setting, "upload_erpnext_items", 0)),
			"push_item_updates": cint(getattr(setting, "update_shopify_item_on_update", 0)),
			"push_variants": cint(getattr(setting, "upload_variants_as_items", 0)),
			"push_new_item_as_active": cint(getattr(setting, "sync_new_item_as_active", 0)),
			"push_inventory": cint(getattr(setting, "update_erpnext_stock_levels_to_shopify", 0)),
			"inventory_sync_frequency": str(getattr(setting, "inventory_sync_frequency", "60") or "60"),
			"add_shipping_as_item": cint(getattr(setting, "add_shipping_as_item", 0)),
			"shipping_item": getattr(setting, "shipping_item", None) or None,
			"consolidate_taxes": cint(getattr(setting, "consolidate_taxes", 1)),
			"order_sync_lookback_days": 3,
			"request_delay_seconds": 0.5,
			"run_request_budget": 800,
			"notes": "Imported from ecommerce_integrations. Both sync directions are OFF until enabled here.",
			"sync_old_orders": 0,
		}
	)
	_copy_credentials(doc, setting)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _update_store(store_name, setting):
	doc = frappe.get_doc(STORE_DOCTYPE, store_name)
	_copy_credentials(doc, setting)
	doc.enable_shopify_to_erpnext = 0
	doc.enable_erpnext_to_shopify = 0
	doc.companies = []
	doc.append("companies", _company_row(setting))
	doc.taxes = []
	for row in _tax_rows(setting):
		doc.append("taxes", row)
	doc.warehouse_mapping = []
	for row in _warehouse_rows(setting):
		doc.append("warehouse_mapping", row)
	doc.save(ignore_permissions=True)
	frappe.db.commit()


def _copy_credentials(doc, setting):
	method = getattr(setting, "authentication_method", "Static Token") or "Static Token"
	doc.authentication_method = method
	if method == "Static Token":
		doc.access_token = setting.get_password("password", raise_exception=False) or ""
		doc.shared_secret = getattr(setting, "shared_secret", "") or ""
	else:
		doc.client_id = getattr(setting, "client_id", "") or ""
		doc.client_secret = setting.get_password("client_secret", raise_exception=False) or ""
		doc.oauth_access_token = setting.get_password("oauth_access_token", raise_exception=False) or ""
		doc.token_expires_at = getattr(setting, "token_expires_at", None)


def _company_row(setting):
	return {
		"company": getattr(setting, "company", None),
		"is_default": 1,
		"enabled": 1,
		"route_field": "Any",
		"route_value": "",
		"customer_group": getattr(setting, "customer_group", None),
		"default_customer": getattr(setting, "default_customer", None),
		"default_warehouse": getattr(setting, "warehouse", None),
		"cost_center": getattr(setting, "cost_center", None),
		"cash_bank_account": getattr(setting, "cash_bank_account", None),
		"sales_order_series": getattr(setting, "sales_order_series", None),
		"delivery_note_series": getattr(setting, "delivery_note_series", None),
		"sales_invoice_series": getattr(setting, "sales_invoice_series", None),
		"default_sales_tax_account": getattr(setting, "default_sales_tax_account", None),
		"default_shipping_charges_account": getattr(setting, "default_shipping_charges_account", None),
		"create_payment_entry": 1,
		"invoice_posting_date_basis": "Order Date",
	}


def _tax_rows(setting):
	company = getattr(setting, "company", None)
	return [
		{
			"company": company,
			"shopify_tax": row.shopify_tax,
			"tax_account": row.tax_account,
			"tax_description": row.tax_description,
		}
		for row in (getattr(setting, "taxes", None) or [])
		if row.shopify_tax and row.tax_account
	]


def _warehouse_rows(setting):
	company = getattr(setting, "company", None)
	rows = []
	for row in getattr(setting, "shopify_warehouse_mapping", None) or []:
		warehouse = getattr(row, "erpnext_warehouse", None) or getattr(row, "warehouse", None)
		if row.shopify_location_id and warehouse:
			rows.append(
				{
					"company": company,
					"shopify_location_id": str(row.shopify_location_id),
					"warehouse": warehouse,
				}
			)
	return rows


# ---------------------------------------------------------------------------
# Backfill links
# ---------------------------------------------------------------------------
def _backfill_links(store_name, domain):
	result = {
		"orders": _backfill_store_field("Sales Order", store_name),
		"invoices": _backfill_store_field("Sales Invoice", store_name),
		"delivery_notes": _backfill_store_field("Delivery Note", store_name),
		"customer_maps": _backfill_customers(store_name),
		"item_maps": _backfill_items(store_name),
	}
	return result


def _backfill_store_field(doctype, store_name):
	"""Attach legacy rows (id present, store empty) to the migrated store."""
	if not frappe.db.has_column(doctype, "custom_shopify_store") or not frappe.db.has_column(
		doctype, "shopify_order_id"
	):
		return 0
	frappe.db.sql(
		f"""
		update `tab{doctype}`
		set custom_shopify_store = %s
		where ifnull(shopify_order_id, '') != ''
		  and ifnull(custom_shopify_store, '') = ''
		""",
		(store_name,),
	)
	frappe.db.commit()
	return frappe.db.sql(
		f"select count(*) from `tab{doctype}` where custom_shopify_store = %s",
		(store_name,),
	)[0][0]


def _backfill_customers(store_name):
	if not frappe.db.has_column("Customer", "shopify_customer_id"):
		return 0
	rows = frappe.db.sql(
		"""
		select name, shopify_customer_id from `tabCustomer`
		where ifnull(shopify_customer_id, '') != ''
		""",
		as_dict=True,
	)
	created = 0
	for row in rows:
		if frappe.db.exists(
			CUSTOMER_MAP_DOCTYPE,
			{"store": store_name, "shopify_customer_id": str(row.shopify_customer_id)},
		):
			continue
		try:
			frappe.get_doc(
				{
					"doctype": CUSTOMER_MAP_DOCTYPE,
					"store": store_name,
					"shopify_customer_id": str(row.shopify_customer_id),
					"customer": row.name,
				}
			).insert(ignore_permissions=True, ignore_mandatory=True)
			created += 1
		except Exception:
			log_error(
				"Shopify: customer map backfill failed",
				f"{row.name}\n{frappe.get_traceback()}",
			)
	frappe.db.commit()
	return created


def _backfill_items(store_name):
	if not frappe.db.exists("DocType", LEGACY_ITEM_DOCTYPE):
		return 0
	rows = frappe.db.sql(
		"""
		select erpnext_item_code, integration_item_code, variant_id, sku, has_variants, variant_of
		from `tabEcommerce Item`
		where integration = 'shopify'
		""",
		as_dict=True,
	)
	created = 0
	for row in rows:
		product_id = str(row.integration_item_code or "")
		variant_id = str(row.variant_id or "")
		if not product_id or not row.erpnext_item_code:
			continue
		if frappe.db.sql(
			"""
			select name from `tabShopify Item Map`
			where store = %s and product_id = %s and ifnull(variant_id, '') = %s
			limit 1
			""",
			(store_name, product_id, variant_id),
		):
			continue
		try:
			frappe.get_doc(
				{
					"doctype": ITEM_MAP_DOCTYPE,
					"store": store_name,
					"item_code": row.erpnext_item_code,
					"product_id": product_id,
					"variant_id": variant_id,
					"sku": row.sku or "",
					"has_variants": cint(row.has_variants),
					"variant_of": row.variant_of or "",
				}
			).insert(ignore_permissions=True, ignore_mandatory=True)
			created += 1
		except Exception:
			log_error("Shopify: item map backfill failed", f"{row.erpnext_item_code}\n{frappe.get_traceback()}")
	frappe.db.commit()
	return created
