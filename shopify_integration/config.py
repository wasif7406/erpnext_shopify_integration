"""Resolution of a Shopify Store, its companies and its per-company defaults.

Every flow goes through here. No account, warehouse or naming series is ever
hard-coded: the `Shopify Store Company` / `Shopify Store Tax` /
`Shopify Store Warehouse` child tables are the only source.
"""

import frappe
from frappe.utils import cint

from .constants import (
	AUTH_METHOD_OAUTH,
	AUTH_METHOD_STATIC,
	MAX_REQUESTS_HARD_CEILING,
	POSTING_DATE_BASES,
	POSTING_ORDER_DATE,
	ROUTE_ANY,
	STORE_DOCTYPE,
)
from .exceptions import ShopifyAmbiguousCompany, ShopifyConfigError
from .routing import AmbiguousCompany, UnroutableOrder, resolve_company


# ---------------------------------------------------------------------------
# Store lookup
# ---------------------------------------------------------------------------
def get_store(store: str) -> "frappe.Document":
	if not store:
		raise ShopifyConfigError("No Shopify Store given.")
	if not frappe.db.exists(STORE_DOCTYPE, store):
		raise ShopifyConfigError(f"Shopify Store {store} does not exist.")
	return frappe.get_doc(STORE_DOCTYPE, store)


def all_stores(only_enabled: bool = True) -> list:
	filters = {"enabled": 1} if only_enabled else {}
	return frappe.get_all(
		STORE_DOCTYPE,
		filters=filters,
		fields=["name", "store_name", "shop_domain", "enabled", "enable_shopify_to_erpnext", "enable_erpnext_to_shopify"],
		order_by="creation asc",
	)


def stores_for_company(company: str) -> list:
	"""Every enabled store that serves this company (N stores -> 1 company)."""
	rows = frappe.get_all(
		"Shopify Store Company",
		filters={"parenttype": STORE_DOCTYPE, "company": company},
		fields=["parent"],
	)
	names = [row.parent for row in rows]
	if not names:
		return []
	return frappe.get_all(
		STORE_DOCTYPE,
		filters={"name": ("in", names), "enabled": 1},
		fields=["name", "store_name", "shop_domain"],
		order_by="creation asc",
	)


def find_store_by_domain(shop_domain: str):
	domain = normalize_shop_domain(shop_domain)
	if not domain:
		return None
	rows = frappe.get_all(STORE_DOCTYPE, filters={"shop_domain": domain}, pluck="name", limit=1)
	return get_store(rows[0]) if rows else None


def legacy_store_name():
	"""The store that owns rows created by the old connector.

	The migration always creates (or reuses) a store for the legacy
	`Shopify Setting`, so the first store by creation is that one. Existing rows
	without `custom_shopify_store` are backfilled with it.
	"""
	rows = frappe.get_all(STORE_DOCTYPE, pluck="name", order_by="creation asc", limit=1)
	return rows[0] if rows else None


def normalize_shop_domain(value) -> str:
	"""`https://Store.myshopify.com/` -> `store.myshopify.com`."""
	text = (value or "").strip().lower()
	if not text:
		return ""
	text = text.replace("https://", "").replace("http://", "")
	text = text.split("/")[0].split("?")[0]
	return text


# ---------------------------------------------------------------------------
# Store config
# ---------------------------------------------------------------------------
class CompanyConfig:
	"""One company served by a store, resolved to concrete ERPNext defaults."""

	def __init__(self, store, row):
		self.store = store
		self.row = row
		self.company = row.company
		self.is_default = bool(cint(row.is_default))
		self.enabled = bool(cint(row.enabled))
		self.route_field = row.route_field or ROUTE_ANY
		self.route_value = row.route_value or ""

		self.customer_group = row.customer_group or None
		self.default_customer = row.default_customer or None
		self.default_warehouse = row.default_warehouse or None
		self.cost_center = row.cost_center or None
		self.cash_bank_account = row.cash_bank_account or None
		self.create_payment_entry = bool(cint(row.create_payment_entry))

		self.sales_order_series = row.sales_order_series or None
		self.delivery_note_series = row.delivery_note_series or None
		self.sales_invoice_series = row.sales_invoice_series or None

		self.default_sales_tax_account = row.default_sales_tax_account or None
		self.default_shipping_charges_account = row.default_shipping_charges_account or None

		self.invoice_posting_basis = row.invoice_posting_date_basis or POSTING_ORDER_DATE
		if self.invoice_posting_basis not in POSTING_DATE_BASES:
			self.invoice_posting_basis = POSTING_ORDER_DATE

		self.company_abbr = frappe.get_cached_value("Company", self.company, "abbr")
		self.company_currency = frappe.get_cached_value("Company", self.company, "default_currency") or "PKR"

	def __repr__(self):
		return f"<ShopifyCompany {self.company} store={self.store.name}>"

	def as_dict(self) -> dict:
		return {
			"company": self.company,
			"is_default": self.is_default,
			"route_field": self.route_field,
			"route_value": self.route_value,
			"customer_group": self.customer_group,
			"default_warehouse": self.default_warehouse,
			"cost_center": self.cost_center,
			"cash_bank_account": self.cash_bank_account,
			"sales_order_series": self.sales_order_series,
			"delivery_note_series": self.delivery_note_series,
			"sales_invoice_series": self.sales_invoice_series,
		}


class StoreConfig:
	"""A Shopify Store with its direction toggles and company rows resolved."""

	def __init__(self, store):
		if isinstance(store, str):
			store = get_store(store)
		self.store = store
		self.name = store.name
		self.enabled = bool(cint(store.enabled))
		self.dry_run = bool(cint(store.dry_run))
		self.pull_enabled = bool(cint(store.enable_shopify_to_erpnext))
		self.push_enabled = bool(cint(store.enable_erpnext_to_shopify))

		self.pull_customers = bool(cint(store.pull_customers))
		self.pull_products = bool(cint(store.pull_products))
		self.pull_orders = bool(cint(store.pull_orders))
		self.create_sales_invoice = bool(cint(store.create_sales_invoice))
		self.create_delivery_note = bool(cint(store.create_delivery_note))
		self.cancel_sales_order = bool(cint(store.cancel_sales_order))

		self.push_items = bool(cint(store.push_items))
		self.push_item_updates = bool(cint(store.push_item_updates))
		self.push_variants = bool(cint(store.push_variants))
		self.push_inventory = bool(cint(store.push_inventory))
		self.push_fulfillment = bool(cint(store.push_fulfillment))

		self.api_version = (store.api_version or "").strip() or "2025-10"
		self.shop_domain = normalize_shop_domain(store.shop_domain)
		self.request_delay = max(0.25, float(store.request_delay_seconds or 0.5))
		self.request_budget = min(
			max(10, cint(store.run_request_budget) or 800),
			MAX_REQUESTS_HARD_CEILING,
		)

		self.companies = []
		for row in store.companies or []:
			if row.company:
				self.companies.append(CompanyConfig(store, row))
		self._by_company = {cfg.company: cfg for cfg in self.companies}

	def __repr__(self):
		return f"<ShopifyStore {self.name} {self.shop_domain}>"

	def company_config(self, company: str) -> CompanyConfig:
		cfg = self._by_company.get(company)
		if not cfg:
			raise ShopifyConfigError(f"Shopify Store {self.name} is not configured for company {company}.")
		return cfg

	def resolve_company(self, order: dict) -> CompanyConfig:
		rows = [
			{
				"company": cfg.company,
				"enabled": 1 if cfg.enabled else 0,
				"is_default": 1 if cfg.is_default else 0,
				"route_field": cfg.route_field,
				"route_value": cfg.route_value,
			}
			for cfg in self.companies
		]
		try:
			row = resolve_company(rows, order)
		except AmbiguousCompany as exc:
			raise ShopifyAmbiguousCompany(f"{self.name}: {exc}") from exc
		except UnroutableOrder as exc:
			raise ShopifyConfigError(f"{self.name}: {exc}") from exc
		return self.company_config(row["company"])

	def default_company(self) -> CompanyConfig:
		defaults = [cfg for cfg in self.companies if cfg.enabled and cfg.is_default]
		if len(defaults) == 1:
			return defaults[0]
		if len(self.companies) == 1:
			return self.companies[0]
		raise ShopifyConfigError(
			f"Shopify Store {self.name} serves several companies and none is flagged as default."
		)


# ---------------------------------------------------------------------------
# Resolution for documents / jobs
# ---------------------------------------------------------------------------
def config(store=None) -> StoreConfig:
	if store:
		return StoreConfig(store)
	# No store given: a document without a store uses the default/first store,
	# which is enough for push jobs. Pull jobs always pass a store explicitly.
	name = legacy_store_name()
	if not name:
		raise ShopifyConfigError("No Shopify Store exists. Create one first.")
	return StoreConfig(name)


def config_for_sales_order(so) -> StoreConfig | None:
	store = getattr(so, "custom_shopify_store", None) if so else None
	if not store:
		store = legacy_store_name()
	if not store:
		return None
	return StoreConfig(store)


def company_config_for_store(store, company: str) -> CompanyConfig:
	return config(store).company_config(company)


# ---------------------------------------------------------------------------
# Defaults used by the mappers
# ---------------------------------------------------------------------------
def warehouse_for(cfg: StoreConfig, company_cfg: CompanyConfig, location_id=None) -> str:
	"""Shopify location -> ERPNext warehouse, then the company default."""
	if location_id not in (None, ""):
		for row in cfg.store.warehouse_mapping or []:
			if row.company == company_cfg.company and str(row.shopify_location_id or "") == str(location_id):
				if row.warehouse:
					return row.warehouse

	if company_cfg.default_warehouse:
		return company_cfg.default_warehouse

	# Global ERPNext default, but only when it belongs to this company.
	default_warehouse = frappe.db.get_single_value("Stock Settings", "default_warehouse")
	if default_warehouse and frappe.get_cached_value("Warehouse", default_warehouse, "company") == company_cfg.company:
		return default_warehouse

	raise ShopifyConfigError(
		f"Shopify Store {cfg.name}: no warehouse for company {company_cfg.company}"
		+ (f" / location {location_id}" if location_id else "")
		+ ". Set a Default Warehouse on the company row or map the Shopify location."
	)


def tax_account_for(cfg: StoreConfig, company_cfg: CompanyConfig, tax_title, charge_type: str):
	"""`Shopify Tax Account` row -> ERPNext account, then the company default.

	`charge_type` is `sales_tax` or `shipping`; the defaults live on the
	`Shopify Store Company` row so two companies with different charts of
	accounts never share a tax account.
	"""
	title = (tax_title or "").strip().lower()
	for row in cfg.store.taxes or []:
		if row.company != company_cfg.company:
			continue
		if (row.shopify_tax or "").strip().lower() == title and row.tax_account:
			return row.tax_account

	fallback = (
		company_cfg.default_shipping_charges_account
		if charge_type == "shipping"
		else company_cfg.default_sales_tax_account
	)
	if fallback:
		return fallback

	raise ShopifyConfigError(
		f"Shopify Store {cfg.name}: tax '{tax_title}' ({charge_type}) is not mapped for company "
		f"{company_cfg.company}. Add a row to Shopify Tax Account or set the default."
	)


def cost_center_for(cfg: StoreConfig, company_cfg: CompanyConfig):
	if company_cfg.cost_center:
		return company_cfg.cost_center
	return frappe.get_cached_value("Company", company_cfg.company, "cost_center") or None


def naming_series(company_cfg: CompanyConfig, doctype: str):
	value = {
		"Sales Order": company_cfg.sales_order_series,
		"Delivery Note": company_cfg.delivery_note_series,
		"Sales Invoice": company_cfg.sales_invoice_series,
	}.get(doctype)
	return value or None


# ---------------------------------------------------------------------------
# Validation (the Shopify Store controller calls this)
# ---------------------------------------------------------------------------
def validate_store(doc):
	doc.shop_domain = normalize_shop_domain(doc.shop_domain)
	if not doc.shop_domain:
		frappe.throw("Shop Domain is required.")

	duplicate = frappe.db.get_value(
		STORE_DOCTYPE,
		{"shop_domain": doc.shop_domain, "name": ("!=", doc.name or "")},
		"name",
	)
	if duplicate:
		frappe.throw(
			f"Shop {doc.shop_domain} is already used by Shopify Store {duplicate}. "
			"One store document owns one shop domain (multi-company is configured inside it)."
		)

	if doc.authentication_method == AUTH_METHOD_STATIC:
		if not doc.get_password("access_token", raise_exception=False):
			frappe.throw("An Access Token is required for Static Token authentication.")
	elif doc.authentication_method == AUTH_METHOD_OAUTH:
		if not doc.client_id or not doc.get_password("client_secret", raise_exception=False):
			frappe.throw("Client ID and Client Secret are required for OAuth 2.0 authentication.")

	if not doc.authentication_method:
		doc.authentication_method = AUTH_METHOD_STATIC
	if not doc.webhook_key:
		doc.webhook_key = frappe.generate_hash(length=32)

	_validate_companies(doc)
	_validate_taxes(doc)
	_validate_warehouses(doc)


def _validate_companies(doc):
	if not doc.companies:
		frappe.throw("Add at least one company to this store.")

	seen_companies = set()
	seen_routes = set()
	defaults = 0

	for row in doc.companies:
		if not row.company:
			frappe.throw("Every company row needs a Company.")
		if not row.route_field:
			row.route_field = ROUTE_ANY
		if row.company in seen_companies:
			frappe.throw(f"Company {row.company} appears more than once.")
		seen_companies.add(row.company)

		if row.is_default:
			defaults += 1
		if row.route_field == ROUTE_ANY:
			if not row.is_default and len(doc.companies) > 1:
				frappe.throw(
					f"Company {row.company}: routing 'Any' means 'use this company when nothing else "
					"matches', so the same row must also be the default."
				)
			if len(doc.companies) == 1:
				row.is_default = 1
			row.route_value = ""
		else:
			if not (row.route_value or "").strip():
				frappe.throw(f"Company {row.company}: set a routing value for '{row.route_field}'.")
			key = (row.route_field, row.route_value.strip().lower())
			if key in seen_routes:
				frappe.throw(
					f"Routing rule {row.route_field} = {row.route_value} is used twice; "
					"an order would match two companies."
				)
			seen_routes.add(key)

		if len(doc.companies) > 1 and not row.is_default:
			# allowed: an order must then match this company's routing rule
			pass

		_validate_company_defaults(doc, row)

	# A one-company store is trivially the default, even if the user never
	# ticked the box (the common 1 store / 1 company setup).
	if len(doc.companies) == 1 and defaults == 0:
		doc.companies[0].is_default = 1
		defaults = 1

	if len(doc.companies) > 1 and defaults != 1:
		frappe.throw(
			"A store that serves several companies needs exactly one of them flagged as Default "
			f"(found {defaults})."
		)


def _validate_company_defaults(doc, row):
	company = row.company

	if row.customer_group:
		_assert_exists("Customer Group", row.customer_group)
	if doc.shipping_item and not frappe.db.exists("Item", doc.shipping_item):
		frappe.throw(f"Shipping Item {doc.shipping_item} does not exist.")
	if row.default_customer:
		_assert_exists("Customer", row.default_customer)
	if row.default_warehouse:
		meta = frappe.db.get_value("Warehouse", row.default_warehouse, ["company", "is_group"], as_dict=True)
		if not meta or meta.company != company:
			frappe.throw(f"Warehouse {row.default_warehouse} does not belong to company {company}.")
		if meta.is_group:
			frappe.throw(f"Warehouse {row.default_warehouse} is a group; select a leaf warehouse.")
	if row.cost_center:
		meta = frappe.db.get_value("Cost Center", row.cost_center, ["company", "is_group"], as_dict=True)
		if not meta or meta.company != company:
			frappe.throw(f"Cost Center {row.cost_center} does not belong to company {company}.")
	if row.cash_bank_account:
		meta = frappe.db.get_value("Account", row.cash_bank_account, ["company", "is_group", "account_type"], as_dict=True)
		if not meta or meta.company != company:
			frappe.throw(f"Account {row.cash_bank_account} does not belong to company {company}.")
		if meta.is_group:
			frappe.throw(f"Account {row.cash_bank_account} is a group account; select a leaf account.")
	if row.default_sales_tax_account:
		_validate_account_company(row.default_sales_tax_account, company)
	if row.default_shipping_charges_account:
		_validate_account_company(row.default_shipping_charges_account, company)


def _validate_taxes(doc):
	seen = set()
	for row in doc.taxes or []:
		if not row.company:
			frappe.throw("Every tax mapping row needs a Company.")
		if not (row.shopify_tax or "").strip():
			frappe.throw("Every tax mapping row needs a Shopify Tax title.")
		key = (row.company, row.shopify_tax.strip().lower())
		if key in seen:
			frappe.throw(f"Tax '{row.shopify_tax}' is mapped twice for company {row.company}.")
		seen.add(key)
		if not row.tax_account:
			frappe.throw(f"Tax '{row.shopify_tax}' needs an ERPNext account.")
		_validate_account_company(row.tax_account, row.company)


def _validate_warehouses(doc):
	seen = set()
	for row in doc.warehouse_mapping or []:
		if not row.company:
			frappe.throw("Every warehouse mapping row needs a Company.")
		if row.shopify_location_id in (None, ""):
			frappe.throw("A Shopify Location ID is required for every warehouse mapping row.")
		key = (row.company, str(row.shopify_location_id))
		if key in seen:
			frappe.throw(
				f"Location {row.shopify_location_id} is mapped twice for company {row.company}."
			)
		seen.add(key)
		meta = frappe.db.get_value("Warehouse", row.warehouse, ["company", "is_group"], as_dict=True)
		if not meta or meta.company != row.company:
			frappe.throw(f"Warehouse {row.warehouse} does not belong to company {row.company}.")
		if meta.is_group:
			frappe.throw(f"Warehouse {row.warehouse} is a group; select a leaf warehouse.")


def _validate_account_company(account, company):
	if not account:
		return
	meta = frappe.db.get_value("Account", account, ["company", "is_group"], as_dict=True)
	if not meta:
		frappe.throw(f"Account {account} does not exist.")
	if meta.company != company:
		frappe.throw(f"Account {account} belongs to {meta.company}, not {company}.")
	if meta.is_group:
		frappe.throw(f"Account {account} is a group account; select a leaf account.")


def _assert_exists(doctype, name):
	if not frappe.db.exists(doctype, name):
		frappe.throw(f"{doctype} {name} does not exist.")


def ensure_single_default(doc):
	"""Only one default company row per store."""
	defaults = [row for row in doc.companies or [] if cint(row.is_default)]
	if len(defaults) <= 1:
		return
	for row in defaults[1:]:
		row.is_default = 0
