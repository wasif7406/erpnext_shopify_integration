"""Orders: Shopify -> Sales Order, plus status/cancellation handling.

One Shopify store may serve one or several ERPNext companies (N:M). The
company is resolved per order by `config.resolve_company` (routing rules, then
the store's default company), so a shared store never posts one company's
revenue into another's ledger.

Import is idempotent on `(store, shopify_order_id)`. The `custom_shopify_store`
link records ownership; a legacy row (created by the old connector) has the id
but no store and is adopted by the first store that sees it.
"""

import datetime

import frappe
from frappe.utils import flt, get_datetime, now_datetime, nowdate

from .client import ShopifyClient
from .config import StoreConfig, config, cost_center_for, naming_series
from .constants import (
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	POLL_OVERLAP_MINUTES,
	STORE_FIELD,
	STORE_DOCTYPE,
)
from .customer import ensure_customer, find_customer
from .exceptions import ShopifyBudgetExhausted, ShopifyConfigError, ShopifyUnmappedProduct
from .payloads import is_cancelled, is_paid
from .product import ensure_items
from .taxes import get_dummy_price_list, get_dummy_tax_category, get_order_taxes
from .utils import Run, log_error, now, set_values, site_datetime


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------
def find_sales_order(cfg: StoreConfig, order_id):
	order_id = str(order_id)
	name = frappe.db.get_value(
		"Sales Order", {ORDER_ID_FIELD: order_id, STORE_FIELD: cfg.name}, "name", order_by="creation desc"
	)
	if name:
		return frappe.get_doc("Sales Order", name)

	# Legacy / pre-app row: adopt it for this store instead of importing twice.
	name = frappe.db.get_value(
		"Sales Order",
		{ORDER_ID_FIELD: order_id, STORE_FIELD: ("is", "not set")},
		"name",
		order_by="creation desc",
	)
	if name:
		frappe.db.set_value("Sales Order", name, STORE_FIELD, cfg.name, update_modified=False)
		return frappe.get_doc("Sales Order", name)
	return None


def find_invoice(cfg: StoreConfig, order_id, exclude_returns: bool = True):
	for store_filter in (cfg.name, ("is", "not set")):
		rows = frappe.get_all(
			"Sales Invoice",
			filters={ORDER_ID_FIELD: str(order_id), STORE_FIELD: store_filter, "docstatus": ("!=", 2)},
			fields=["name", "is_return"],
			order_by="creation desc",
		)
		for row in rows:
			if exclude_returns and row.is_return:
				continue
			if not frappe.db.get_value("Sales Invoice", row.name, STORE_FIELD):
				frappe.db.set_value("Sales Invoice", row.name, STORE_FIELD, cfg.name, update_modified=False)
			return row.name
	return None


def find_delivery_notes(cfg: StoreConfig, order_id):
	return frappe.get_all(
		"Delivery Note",
		filters={ORDER_ID_FIELD: str(order_id), "docstatus": ("!=", 2)},
		pluck="name",
		order_by="creation asc",
	)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
def import_order(cfg: StoreConfig, order: dict, run: Run | None = None, trigger: str = "Pull"):
	"""Create (or refresh) the Sales Order for one Shopify order payload."""
	if not order or not order.get("id"):
		return None, "no order id"
	if not cfg.enabled or not cfg.pull_enabled:
		return None, "store pull disabled"
	if not cfg.pull_orders:
		return None, "order pull disabled"

	order_id = str(order["id"])

	# Hard cutoff first: a historical order is neither imported nor refreshed,
	# even if a late webhook or an updated_at poll still returns it.
	if cfg.is_historical(order):
		if run:
			run.bump("orders_skipped")
		return None, "before pull cutoff"

	existing = find_sales_order(cfg, order_id)
	if existing:
		if run:
			run.bump("orders_updated")
		refresh_existing(cfg, existing, order, run=run)
		return existing, "exists"

	if is_cancelled(order):
		if run:
			run.bump("orders_skipped")
		return None, "cancelled before import"

	if cfg.dry_run:
		if run:
			run.bump("orders_skipped")
			run.note(f"dry run: would import {order.get('name')}")
		return None, "dry run"

	from frappe.utils.synchronization import LockTimeoutError, filelock

	try:
		with filelock(f"shopify-order-{cfg.name}-{order_id}", timeout=0):
			# Re-check inside the lock: a webhook and the poll can race.
			existing = find_sales_order(cfg, order_id)
			if existing:
				if run:
					run.bump("orders_updated")
				refresh_existing(cfg, existing, order, run=run)
				return existing, "exists"
			try:
				return _create_sales_order(cfg, order, run=run)
			except ShopifyUnmappedProduct as exc:
				# A test/unlisted product: skip the whole order, create nothing.
				frappe.db.rollback()
				if run:
					run.bump("orders_skipped")
					run.note(f"skipped {order.get('name')}: {exc}")
				return None, str(exc)
	except LockTimeoutError:
		if run:
			run.bump("orders_skipped")
		return None, "order is being imported by another worker"


def _create_sales_order(cfg: StoreConfig, order: dict, run=None):
	company_cfg = cfg.resolve_company(order)
	order_id = str(order["id"])

	customer = _customer_for_order(cfg, company_cfg, order, run=run)
	items = ensure_items(cfg, order, budget=_budget(run), run=run)
	taxes = get_order_taxes(
		cfg,
		company_cfg,
		order,
		items,
		cost_center=cost_center_for(cfg, company_cfg),
	)
	cost_center = cost_center_for(cfg, company_cfg)
	for item in items:
		item["cost_center"] = cost_center

	created = site_datetime(order.get("created_at"))
	transaction_date = created.date() if created else nowdate()

	so = frappe.get_doc(
		{
			"doctype": "Sales Order",
			"naming_series": naming_series(company_cfg, "Sales Order"),
			"customer": customer.name,
			"customer_name": customer.customer_name,
			"company": company_cfg.company,
			"transaction_date": transaction_date,
			"delivery_date": transaction_date,
			"selling_price_list": get_dummy_price_list(),
			"ignore_pricing_rule": 1,
			"tax_category": get_dummy_tax_category(),
			"items": items,
			"taxes": taxes,
			ORDER_ID_FIELD: order_id,
			ORDER_NUMBER_FIELD: str(order.get("name") or ""),
			ORDER_STATUS_FIELD: _status_text(order),
			STORE_FIELD: cfg.name,
			"custom_shopify_synced_on": now(),
			"contact_email": (order.get("customer") or {}).get("email") or order.get("email"),
			"contact_phone": (order.get("customer") or {}).get("phone") or order.get("phone"),
			"customer_address": _address_name(customer, order, "Billing"),
			"shipping_address_name": _address_name(customer, order, "Shipping"),
			"shipping_address": _shipping_text(order),
		}
	)
	_apply_currency(so, order, company_cfg)
	so.flags.ignore_mandatory = True
	so.flags.ignore_permissions = True

	try:
		so.insert(ignore_permissions=True, ignore_mandatory=True)
		so.submit()
	except Exception:
		frappe.db.rollback()
		raise

	frappe.db.commit()

	# The invoice/receivable must equal what Shopify charged. A mismatch means
	# a tax/shipping/discount mapping is wrong; it is logged, not deleted.
	expected_total = flt(order.get("current_total_price") or order.get("total_price"))
	if expected_total and abs(flt(so.grand_total) - expected_total) > 0.05:
		log_error(
			"Shopify: order total mismatch",
			f"{so.name} / Shopify {order.get('name')}: ERPNext {so.grand_total} vs "
			f"Shopify {expected_total}",
			"Sales Order",
			so.name,
		)
		if run:
			run.note(f"total mismatch {so.name}: {so.grand_total} vs {expected_total}")

	if order.get("note"):
		so.add_comment(text=f"Shopify order note: {order.get('note')}")
	if run:
		run.bump("orders_created")

	_after_import(cfg, so, order, run=run)
	return so, "created"


def _after_import(cfg: StoreConfig, so, order, run=None):
	"""Run the opt-in downstream steps; each one is individually idempotent."""
	if cfg.create_sales_invoice and _should_invoice(order):
		try:
			from .invoice import ensure_invoice

			ensure_invoice(cfg, so, order, run=run)
		except Exception as exc:
			_fail(run, "invoice", exc, so)

	if cfg.create_delivery_note and order.get("fulfillments"):
		try:
			from .fulfillment import ensure_delivery_notes

			ensure_delivery_notes(cfg, so, order, run=run)
		except Exception as exc:
			_fail(run, "delivery note", exc, so)

	if is_cancelled(order):
		cancel_sales_order(cfg, so, order, run=run)


def _should_invoice(order) -> bool:
	return is_paid(order)


def refresh_existing(cfg: StoreConfig, so, order, run=None):
	"""Refresh status and catch up on paid/fulfilled events that arrived late."""
	values = {
		ORDER_STATUS_FIELD: _status_text(order),
		ORDER_NUMBER_FIELD: str(order.get("name") or ""),
		"custom_shopify_synced_on": now(),
	}
	if not so.get(STORE_FIELD):
		values[STORE_FIELD] = cfg.name
	set_values("Sales Order", so.name, values)
	so.reload()

	if is_cancelled(order) and cfg.cancel_sales_order:
		cancel_sales_order(cfg, so, order, run=run)
		return

	if so.docstatus == 1:
		_after_import(cfg, so, order, run=run)


# ---------------------------------------------------------------------------
# Webhook handlers / poll
# ---------------------------------------------------------------------------
def handle_webhook_order(payload, store=None, request_id=None):
	cfg = config(store)
	if cfg.is_historical(payload):
		return {"order": payload.get("name"), "result": "before pull cutoff"}
	with Run(cfg.name, mode="Orders", trigger="Webhook") as run:
		so, result = import_order(cfg, payload, run=run, trigger="Webhook")
	return {"order": payload.get("name"), "sales_order": getattr(so, "name", None), "result": result}


def handle_webhook_cancelled(payload, store=None, request_id=None):
	cfg = config(store)
	if cfg.is_historical(payload):
		return {"order": payload.get("name"), "result": "before pull cutoff"}
	with Run(cfg.name, mode="Cancellations", trigger="Webhook") as run:
		so = find_sales_order(cfg, payload.get("id"))
		if not so:
			return {"order": payload.get("name"), "result": "no sales order"}
		if cfg.cancel_sales_order:
			cancel_sales_order(cfg, so, payload, run=run)
			return {"order": payload.get("name"), "sales_order": so.name, "result": "cancelled"}
		set_values("Sales Order", so.name, {ORDER_STATUS_FIELD: _status_text(payload, cancelled=True)})
		return {"order": payload.get("name"), "sales_order": so.name, "result": "status only"}


def poll_orders(cfg: StoreConfig, run: Run, lookback_days: int | None = None):
	"""Catch orders whose webhook was missed (or that predate the app)."""
	since = get_datetime(cfg.store.last_order_sync_on) if cfg.store.last_order_sync_on else None
	if since:
		since = since - datetime.timedelta(minutes=POLL_OVERLAP_MINUTES)
	else:
		days = lookback_days if lookback_days is not None else int(cfg.store.order_sync_lookback_days or 3)
		since = now_datetime() - datetime.timedelta(days=max(1, days))

	client = ShopifyClient(cfg, budget=_budget(run))
	count = 0
	completed = True
	poll_params = {"status": "any", "updated_at_min": _shopify_time(since)}
	if cfg.pull_orders_since:
		# Let Shopify do the filtering: no historical order is even returned.
		poll_params["created_at_min"] = _shopify_time(get_datetime(cfg.pull_orders_since))
	try:
		for order in client.paginate("orders.json", "orders", params=poll_params):
			count += 1
			if run:
				run.bump("orders_seen")
			try:
				import_order(cfg, order, run=run, trigger="Scheduler")
			except ShopifyBudgetExhausted:
				raise
			except Exception as exc:
				run.fail("order", exc)
	except ShopifyBudgetExhausted:
		# Do NOT advance the cursor: orders after the budget cut-off would be
		# skipped forever. The next tick re-reads from the same point (imports
		# are idempotent).
		completed = False
		if run:
			run.budget_exhausted = 1
	finally:
		if completed:
			set_values(STORE_DOCTYPE, cfg.name, {"last_order_sync_on": now()}, commit=True)
	return count


def sync_old_orders(cfg: StoreConfig, from_datetime, to_datetime, run: Run):
	"""Backfill by created_at range (the old connector's 'Sync Old Orders')."""
	client = ShopifyClient(cfg, budget=_budget(run))
	count = 0
	created = 0
	completed = True
	try:
		for order in client.paginate(
			"orders.json",
			"orders",
			params={
				"status": "any",
				"created_at_min": _shopify_time(get_datetime(from_datetime)),
				"created_at_max": _shopify_time(get_datetime(to_datetime)),
			},
		):
			count += 1
			run.bump("orders_seen")
			try:
				so, result = import_order(cfg, order, run=run, trigger="Old Orders")
				if result == "created":
					created += 1
					frappe.db.commit()
			except ShopifyBudgetExhausted:
				raise
			except Exception as exc:
				run.fail("order", exc)
	except ShopifyBudgetExhausted:
		# Keep the flag set: the next run replays the range (imports are
		# idempotent) instead of silently losing the rest of the backfill.
		completed = False
		run.budget_exhausted = 1
		run.note("request budget exhausted; the range will resume on the next run")
	finally:
		# Never move the poll cursor from a backfill: it would skip orders that
		# changed between the backfill and the next poll.
		if completed:
			set_values(STORE_DOCTYPE, cfg.name, {"sync_old_orders": 0}, commit=True)
	return {"orders": count, "created": created, "completed": completed}


def import_order_by_id(cfg: StoreConfig, order_id, run: Run | None = None):
	client = ShopifyClient(cfg, budget=_budget(run))
	order = client.order(order_id)
	if not order:
		raise ShopifyConfigError(f"Shopify order {order_id} not found in {cfg.name}.")
	return import_order(cfg, order, run=run, trigger="Manual")


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------
def cancel_sales_order(cfg: StoreConfig, so, order, run=None):
	"""Cancel the Sales Order when no invoice or delivery note exists yet."""
	invoice = find_invoice(cfg, order.get("id"))
	delivery_notes = find_delivery_notes(cfg, order.get("id"))
	if invoice or delivery_notes:
		set_values("Sales Order", so.name, {ORDER_STATUS_FIELD: _status_text(order, cancelled=True)})
		if run:
			run.note(f"{so.name} kept: invoice/delivery note already exists")
		return False

	if so.docstatus == 2:
		return True

	so.reload()
	if so.docstatus == 1:
		try:
			so.flags.ignore_permissions = True
			so.cancel()
		except Exception:
			# A Closed order or a linked document can block the cancel; the
			# Shopify status is still recorded so the merchant sees it.
			frappe.db.rollback()
			log_error(
				"Shopify: could not cancel Sales Order",
				f"{so.name}\n{frappe.get_traceback()}",
				"Sales Order",
				so.name,
			)
			set_values("Sales Order", so.name, {ORDER_STATUS_FIELD: _status_text(order, cancelled=True)})
			return False
		frappe.db.set_value(
			"Sales Order",
			so.name,
			{ORDER_STATUS_FIELD: _status_text(order, cancelled=True), STORE_FIELD: cfg.name},
			update_modified=False,
		)
		frappe.db.commit()
		if run:
			run.bump("cancelled")
		return True
	return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _customer_for_order(cfg: StoreConfig, company_cfg, order, run=None):
	if cfg.pull_customers:
		return ensure_customer(cfg, company_cfg, order, run=run)

	payload = order.get("customer") or {}
	customer = find_customer(cfg, payload.get("id"), payload=payload)
	if customer:
		return customer
	if company_cfg.default_customer:
		return frappe.get_doc("Customer", company_cfg.default_customer)
	raise ShopifyConfigError(
		f"Shopify Store {cfg.name}: order {order.get('name')} needs a customer, "
		"'Pull Customers' is off and no Default Customer is configured for "
		f"company {company_cfg.company}."
	)


def _status_text(order, cancelled: bool = False) -> str:
	if cancelled or order.get("cancelled_at"):
		return "cancelled"
	return (order.get("financial_status") or "").strip() or (order.get("fulfillment_status") or "").strip()


def _address_name(customer, order, address_type):
	payload = (
		order.get("shipping_address")
		if address_type == "Shipping"
		else (order.get("billing_address") or order.get("shipping_address"))
	) or {}
	address_id = payload.get("id")
	if address_id:
		name = frappe.db.get_value("Address", {"shopify_address_id": str(address_id)}, "name")
		if name:
			return name
	# Fall back to the newest address of that type linked to the customer.
	rows = frappe.db.sql(
		"""
		select a.name
		from `tabAddress` a
		join `tabDynamic Link` dl on dl.parent = a.name and dl.parenttype = 'Address'
		where dl.link_doctype = 'Customer' and dl.link_name = %s and a.address_type = %s
		order by a.creation desc
		limit 1
		""",
		(customer.name, address_type),
	)
	return rows[0][0] if rows else None


def _shipping_text(order):
	address = order.get("shipping_address") or {}
	if not address:
		return None
	parts = [
		address.get("address1"),
		address.get("address2"),
		address.get("city"),
		address.get("province"),
		address.get("zip"),
		address.get("country"),
	]
	return ", ".join(part for part in parts if part)


def _apply_currency(so, order, company_cfg):
	order_currency = (order.get("currency") or company_cfg.company_currency or "").upper()
	company_currency = (company_cfg.company_currency or "").upper()
	if not order_currency or order_currency == company_currency:
		return

	from erpnext.setup.utils import get_exchange_rate

	date = (order.get("created_at") or "")[:10] or None
	try:
		rate = get_exchange_rate(order_currency, company_currency, date)
	except Exception as exc:
		raise ShopifyConfigError(
			f"No exchange rate {order_currency} -> {company_currency} for {date}: {exc}"
		) from exc
	if not flt(rate):
		raise ShopifyConfigError(
			f"No usable exchange rate {order_currency} -> {company_currency} for {date}."
		)
	so.currency = order_currency
	so.conversion_rate = flt(rate)
	so.price_list_currency = order_currency
	so.plc_conversion_rate = so.conversion_rate


def _shopify_time(value) -> str:
	"""ERPNext datetime -> the ISO-8601 form Shopify's list filters expect."""
	parsed = get_datetime(value)
	return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _budget(run):
	return run.budget if run and getattr(run, "budget", None) else None


def _fail(run, step, exc, so):
	frappe.db.rollback()
	log_error(f"Shopify: {step} failed", f"{so.name}\n{frappe.get_traceback()}", "Sales Order", so.name)
	if run:
		run.fail(step, exc, "Sales Order", so.name)
