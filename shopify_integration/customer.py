"""Customers, addresses and contacts pulled from Shopify orders.

A `Customer` is global in ERPNext, so one Shopify customer can serve several
companies; the store-specific link is `Shopify Customer Map` (one row per
store), which also lets two stores with colliding customer ids coexist. The
legacy `Customer.shopify_customer_id` field is kept up to date for reports but
is only trusted for the store that predates this app.
"""

import frappe
from frappe import _
from frappe.utils import cint

from .config import StoreConfig, legacy_store_name
from .constants import ADDRESS_ID_FIELD, CUSTOMER_ID_FIELD, CUSTOMER_MAP_DOCTYPE
from .utils import normalize_phone


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------
def find_customer(cfg: StoreConfig, shopify_customer_id, payload: dict | None = None):
	if shopify_customer_id:
		name = frappe.db.get_value(
			CUSTOMER_MAP_DOCTYPE,
			{"store": cfg.name, "shopify_customer_id": str(shopify_customer_id)},
			"customer",
		)
		if name:
			return frappe.get_doc("Customer", name)

		# Backward compatibility with rows created by the legacy connector.
		if legacy_store_name() == cfg.name:
			name = frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: str(shopify_customer_id)}, "name")
			if name:
				_remember_customer(cfg, shopify_customer_id, name)
				return frappe.get_doc("Customer", name)

	# The historical import created ~1,100 customers without a Shopify id, so a
	# Shopify id alone is not enough: match the same person by email, then
	# phone, then (only when there is no email/phone) an exact name.
	if payload:
		return _find_by_identity(cfg, shopify_customer_id, payload)
	return None


def _find_by_identity(cfg: StoreConfig, shopify_customer_id, payload: dict):
	email = (payload.get("email") or "").strip().lower()
	phone = normalize_phone(payload.get("phone") or (payload.get("default_address") or {}).get("phone"))
	name = " ".join(
		part for part in ((payload.get("first_name") or "").strip(), (payload.get("last_name") or "").strip()) if part
	).strip()

	candidate = None
	if email:
		candidate = frappe.db.get_value("Customer", {"email_id": email}, "name", order_by="creation asc")
		if not candidate:
			candidate = _customer_from_contact_email(email)

	if not candidate and phone:
		last10 = phone[-10:]
		rows = frappe.db.sql(
			"""
			select name from `tabCustomer`
			where right(regexp_replace(ifnull(mobile_no, ''), '[^0-9]', ''), 10) = %s
			order by creation asc
			limit 1
			""",
			(last10,),
		)
		candidate = rows[0][0] if rows else None
		if not candidate:
			candidate = _customer_from_contact_phone(last10)

	if not candidate and name and not email and not phone:
		rows = frappe.get_all("Customer", filters={"customer_name": name}, pluck="name", limit=2)
		if len(rows) == 1:
			candidate = rows[0]

	if not candidate:
		return None

	_remember_customer(cfg, shopify_customer_id, candidate) if shopify_customer_id else None
	if shopify_customer_id:
		current = frappe.db.get_value("Customer", candidate, CUSTOMER_ID_FIELD)
		if not current:
			frappe.db.set_value(
				"Customer", candidate, CUSTOMER_ID_FIELD, str(shopify_customer_id), update_modified=False
			)
	return frappe.get_doc("Customer", candidate)


def _customer_from_contact_email(email: str):
	rows = frappe.db.sql(
		"""
		select dl.link_name
		from `tabContact Email` ce
		join `tabDynamic Link` dl on dl.parent = ce.parent and dl.parenttype = 'Contact'
		where ce.email_id = %s and dl.link_doctype = 'Customer'
		order by dl.creation asc
		limit 1
		""",
		(email,),
	)
	return rows[0][0] if rows else None


def _customer_from_contact_phone(last10: str):
	rows = frappe.db.sql(
		"""
		select dl.link_name
		from `tabContact Phone` cp
		join `tabDynamic Link` dl on dl.parent = cp.parent and dl.parenttype = 'Contact'
		where right(regexp_replace(ifnull(cp.phone, ''), '[^0-9]', ''), 10) = %s
		  and dl.link_doctype = 'Customer'
		order by dl.creation asc
		limit 1
		""",
		(last10,),
	)
	return rows[0][0] if rows else None


def _remember_customer(cfg: StoreConfig, shopify_customer_id, customer):
	existing = frappe.db.get_value(
		CUSTOMER_MAP_DOCTYPE,
		{"store": cfg.name, "shopify_customer_id": str(shopify_customer_id)},
		"name",
	)
	if existing:
		return
	frappe.get_doc(
		{
			"doctype": CUSTOMER_MAP_DOCTYPE,
			"store": cfg.name,
			"shopify_customer_id": str(shopify_customer_id),
			"customer": customer,
		}
	).insert(ignore_permissions=True, ignore_mandatory=True)


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------
def ensure_customer(cfg: StoreConfig, company_cfg, order: dict, run=None):
	"""Return the ERPNext Customer for a Shopify order, creating it if needed."""
	customer_payload = order.get("customer") or {}
	shopify_customer_id = customer_payload.get("id") or _fallback_customer_id(order)

	# Two different orders of the same customer can be imported concurrently;
	# one short lock per customer avoids duplicate Customer masters.
	if shopify_customer_id:
		from frappe.utils.synchronization import LockTimeoutError, filelock

		try:
			with filelock(f"shopify-customer-{cfg.name}-{shopify_customer_id}", timeout=10):
				return _ensure_customer_locked(cfg, company_cfg, order, customer_payload, shopify_customer_id, run)
		except LockTimeoutError:
			pass

	return _ensure_customer_locked(cfg, company_cfg, order, customer_payload, shopify_customer_id, run)


def _ensure_customer_locked(cfg, company_cfg, order, customer_payload, shopify_customer_id, run=None):
	existing = find_customer(cfg, shopify_customer_id, payload=customer_payload)

	if existing:
		if cfg.pull_customers:
			update_addresses(cfg, existing, order)
		return existing

	if not shopify_customer_id:
		# Guest checkout: use the configured default customer, else a per-store guest.
		if company_cfg.default_customer:
			return frappe.get_doc("Customer", company_cfg.default_customer)
		shopify_customer_id = f"guest-{order.get('id')}"

	customer = create_customer(cfg, company_cfg, customer_payload, order, shopify_customer_id)
	if run:
		run.bump("customers_created")
	_remember_customer(cfg, shopify_customer_id, customer.name)

	if cfg.pull_customers:
		update_addresses(cfg, customer, order)
		ensure_contact(cfg, customer, customer_payload)
	return customer


def _fallback_customer_id(order):
	email = ((order.get("customer") or {}).get("email")) or order.get("email")
	phone = ((order.get("customer") or {}).get("phone")) or order.get("phone")
	return f"email:{email}" if email else (f"phone:{normalize_phone(phone)}" if phone else None)


def create_customer(cfg: StoreConfig, company_cfg, payload, order, shopify_customer_id):
	customer_name = " ".join(
		part for part in ((payload.get("first_name") or "").strip(), (payload.get("last_name") or "").strip()) if part
	).strip()
	if not customer_name:
		customer_name = payload.get("email") or order.get("email") or f"Shopify Customer {shopify_customer_id}"
	customer_name = _unique_customer_name(customer_name, shopify_customer_id)

	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": customer_name[:140],
			"customer_type": "Individual",
			"customer_group": _customer_group(company_cfg.customer_group),
			"territory": _root_territory(),
			CUSTOMER_ID_FIELD: str(shopify_customer_id),
			"email_id": payload.get("email") or order.get("email"),
			"mobile_no": normalize_phone(payload.get("phone") or order.get("phone")),
		}
	)
	customer.flags.ignore_mandatory = True
	customer.insert(ignore_permissions=True, ignore_mandatory=True)
	return customer


def _unique_customer_name(name, shopify_customer_id):
	if not frappe.db.exists("Customer", {"customer_name": name}):
		return name
	# Same person can legitimately be several Shopify customers (different
	# stores); keep the names distinct instead of failing the order.
	suffix = f" ({shopify_customer_id})"
	return (name[: 140 - len(suffix)] + suffix).strip()


def _customer_group(preferred=None):
	if preferred and frappe.db.exists("Customer Group", preferred):
		return preferred
	return "Individual" if frappe.db.exists("Customer Group", "Individual") else _root_of("Customer Group")


def _root_territory():
	return _root_of("Territory")


def _root_of(doctype):
	try:
		from frappe.utils.nestedset import get_root_of

		return get_root_of(doctype)
	except Exception:
		return None


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------
def update_addresses(cfg: StoreConfig, customer, order: dict):
	shipping = order.get("shipping_address") or {}
	billing = order.get("billing_address") or shipping

	if billing:
		upsert_address(cfg, customer, billing, "Billing", order)
	if shipping:
		upsert_address(cfg, customer, shipping, "Shipping", order)


def upsert_address(cfg: StoreConfig, customer, address_payload, address_type, order):
	if not address_payload:
		return None

	address_id = address_payload.get("id")
	existing = None
	if address_id:
		name = frappe.db.get_value("Address", {ADDRESS_ID_FIELD: str(address_id)}, "name")
		# Address ids are store-scoped; only reuse an address that belongs to
		# this customer, otherwise two stores could rewrite each other's record.
		if name and _address_linked_to(name, customer.name):
			existing = frappe.get_doc("Address", name)

	values = _address_values(address_payload, customer, address_type, order)
	if existing:
		for key, value in values.items():
			if key in ("address_title", "address_type"):
				continue
			setattr(existing, key, value)
		existing.flags.ignore_mandatory = True
		existing.save(ignore_permissions=True)
		return existing

	address = frappe.get_doc(
		{
			"doctype": "Address",
			**values,
			ADDRESS_ID_FIELD: str(address_id or ""),
			"links": [{"link_doctype": "Customer", "link_name": customer.name}],
		}
	)
	address.flags.ignore_mandatory = True
	address.insert(ignore_permissions=True, ignore_mandatory=True)
	return address


def _address_values(payload, customer, address_type, order):
	country = _country(payload.get("country") or payload.get("country_code"))
	phone = normalize_phone(payload.get("phone") or (order.get("customer") or {}).get("phone") or order.get("phone"))
	return {
		"address_title": customer.customer_name or customer.name,
		"address_type": address_type,
		"address_line1": payload.get("address1") or payload.get("address2") or "Address",
		"address_line2": payload.get("address2"),
		"city": payload.get("city") or "Unknown",
		"state": payload.get("province") or payload.get("province_code"),
		"pincode": payload.get("zip"),
		"country": country,
		"email_id": (order.get("customer") or {}).get("email") or order.get("email"),
		"phone": phone,
		"is_shipping_address": 1 if address_type == "Shipping" else 0,
		"is_primary_address": 1 if address_type == "Billing" else 0,
	}


def _address_linked_to(address: str, customer: str) -> bool:
	return bool(
		frappe.db.sql(
			"""
			select 1 from `tabDynamic Link`
			where parent = %s and parenttype = 'Address'
			  and link_doctype = 'Customer' and link_name = %s
			limit 1
			""",
			(address, customer),
		)
	)


def _country(value):
	value = (value or "").strip()
	if value and frappe.db.exists("Country", value):
		return value
	# Shopify may send a two-letter code.
	by_code = frappe.db.get_value("Country", {"code": value.upper()}, "name") if value else None
	if by_code:
		return by_code

	default = "Pakistan"
	for doctype, fieldname in (
		("Global Defaults", "country"),
		("Global Defaults", "default_country"),
		("System Settings", "country"),
	):
		try:
			default = frappe.db.get_single_value(doctype, fieldname) or default
		except Exception:
			continue
	return default if frappe.db.exists("Country", default) else None


# ---------------------------------------------------------------------------
# Contact
# ---------------------------------------------------------------------------
def ensure_contact(cfg: StoreConfig, customer, payload: dict):
	email = (payload.get("email") or "").strip()
	phone = normalize_phone(payload.get("phone"))
	if not email and not phone:
		return None

	if _has_contact(customer.name):
		return None

	contact = frappe.get_doc(
		{
			"doctype": "Contact",
			"first_name": (payload.get("first_name") or customer.customer_name or "Shopify")[:140],
			"last_name": payload.get("last_name") or "",
			"status": "Passive",
			"unsubscribed": 0 if cint(payload.get("accepts_marketing")) else 1,
			"links": [{"link_doctype": "Customer", "link_name": customer.name}],
		}
	)
	if email:
		contact.append("email_ids", {"email_id": email, "is_primary": 1})
	if phone:
		contact.append("phone_nos", {"phone": phone, "is_primary_phone": 1})
	contact.flags.ignore_mandatory = True
	contact.insert(ignore_permissions=True, ignore_mandatory=True)
	return contact


def _has_contact(customer: str) -> bool:
	rows = frappe.db.sql(
		"""
		select 1 from `tabDynamic Link`
		where link_doctype = 'Customer' and link_name = %s and parenttype = 'Contact'
		limit 1
		""",
		(customer,),
	)
	return bool(rows)


# ---------------------------------------------------------------------------
# Push (optional)
# ---------------------------------------------------------------------------
def push_customer(customer, cfg: StoreConfig | None = None):
	"""Create/update the Shopify customer for an ERPNext customer.

	Off by default and intentionally minimal: the connector's job is orders; a
	customer pushed from ERPNext is useful only when an order is created in
	ERPNext first. It is exposed through `api.push_customer`.
	"""
	from .client import ShopifyClient

	cfg = cfg or None
	if not cfg:
		from .config import config

		cfg = config()
	if not cfg.push_enabled:
		frappe.throw(_("ERPNext -> Shopify sync is disabled for this store."))

	client = ShopifyClient(cfg)
	payload = {
		"customer": {
			"first_name": (customer.customer_name or customer.name)[:100],
			"email": customer.email_id,
			"phone": customer.mobile_no,
		}
	}
	response = client.post("customers.json", payload) or {}
	shopify_customer = response.get("customer") or {}
	if shopify_customer.get("id"):
		_remember_customer(cfg, shopify_customer["id"], customer.name)
		frappe.db.set_value(
			"Customer", customer.name, CUSTOMER_ID_FIELD, str(shopify_customer["id"]), update_modified=False
		)
	return shopify_customer
