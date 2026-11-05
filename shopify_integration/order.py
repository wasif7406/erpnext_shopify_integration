"""Orders: Shopify -> Sales Order, plus status/cancellation handling.

One Shopify store may serve one or several ERPNext companies (N:M). The
company is resolved per order by `config.resolve_company` (routing rules, then
the store's default company), so a shared store never posts one company's
revenue into another's ledger.

Import is idempotent on `(store, shopify_order_id)`. The `custom_shopify_store`
link records ownership; a legacy row (created by the old connector or by the
historical CSV import) is adopted by the first store that sees it instead of
being imported twice.

Two rules matter for reliability:

* The "pull cutoff" only stops *new* orders. An order that already exists is
  always refreshed, even if it was created before the cutoff, otherwise its
  status would freeze forever.
* A failure is never swallowed: the order is written to `Shopify Import Retry`
  and replayed by a cron job with backoff (see `retry.py`).
"""

import datetime

import frappe
from frappe.utils import flt, get_datetime, get_system_timezone, now_datetime, nowdate

from . import retry
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
from .exceptions import (
	ShopifyBudgetExhausted,
	ShopifyConfigError,
	ShopifyUnmappedProduct,
)
from .payloads import is_cancelled, is_paid, order_items_changed, shopify_time
from .product import ensure_items
from .taxes import get_dummy_price_list, get_dummy_tax_category, get_order_taxes
from .utils import Run, log_error, money, now, set_values, site_datetime


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------
def find_sales_order(cfg: StoreConfig, order_id, order_number=None):
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
		if _adopt_legacy_row(name, cfg, order_id, order_number):
			return frappe.get_doc("Sales Order", name)
		return None

	# Rows created by the historical CSV import carry only `po_no` (the Shopify
	# order name) and no Shopify id. Matching them here is what stops a
	# backfill from duplicating ~1,600 orders.
	number = str(order_number or "").strip()
	if number:
		rows = frappe.get_all(
			"Sales Order",
			filters={
				"po_no": number,
				ORDER_ID_FIELD: ("is", "not set"),
				STORE_FIELD: ("is", "not set"),
			},
			fields=["name", "shopify_order_number"],
			order_by="creation desc",
			limit=2,
		)
		if len(rows) == 1:
			row_number = (rows[0].get("shopify_order_number") or "").strip()
			if not row_number or row_number == number:
				if _adopt_legacy_row(rows[0].name, cfg, order_id, number):
					return frappe.get_doc("Sales Order", rows[0].name)
				return None
		if len(rows) > 1:
			log_error(
				"Shopify: several orders share one po_no",
				f"store={cfg.name} number={number}; not adopting any of them",
			)
	return None


def _adopt_legacy_row(name, cfg: StoreConfig, order_id, order_number=None) -> bool:
	"""Claim an unowned row once (conditional UPDATE, never overwrite a store)."""
	from .utils import claim_field

	if not claim_field("Sales Order", name, STORE_FIELD, cfg.name):
		return False
	values = {ORDER_ID_FIELD: str(order_id)}
	if order_number and not frappe.db.get_value("Sales Order", name, ORDER_NUMBER_FIELD):
		values[ORDER_NUMBER_FIELD] = str(order_number)
	frappe.db.set_value("Sales Order", name, values, update_modified=False)
	frappe.db.commit()
	return True


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
	"""Delivery Notes for one order, scoped to this store (or legacy rows)."""
	names = []
	for store_filter in (cfg.name, ("is", "not set")):
		names.extend(
			frappe.get_all(
				"Delivery Note",
				filters={
					ORDER_ID_FIELD: str(order_id),
					STORE_FIELD: store_filter,
					"docstatus": ("!=", 2),
				},
				pluck="name",
				order_by="creation asc",
			)
		)
	return names


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
def import_order(cfg: StoreConfig, order: dict, run: Run | None = None, trigger: str = "Pull", force: bool = False):
	"""Create (or refresh) the Sales Order for one Shopify order payload.

	`force` bypasses the pull cutoff, so a missed order can be imported from the
	retry queue or from `api.import_order`.
	"""
	if not order or not order.get("id"):
		return None, "no order id"
	if not cfg.enabled or not cfg.pull_enabled:
		return None, "store pull disabled"
	if not cfg.pull_orders:
		return None, "order pull disabled"

	order_id = str(order["id"])
	order_number = str(order.get("name") or "")

	# Existing orders are ALWAYS refreshed, even when they predate the pull
	# cutoff: the cutoff exists to stop a backfill, not to freeze status.
	existing = find_sales_order(cfg, order_id, order_number)
	if existing:
		if cfg.dry_run:
			if run:
				run.bump("orders_skipped")
			return existing, "dry run"
		_refresh_existing_safe(cfg, existing, order, run=run)
		return existing, "exists"

	# Hard cutoff for *new* orders only.
	if not force and cfg.is_historical(order):
		if run:
			run.bump("orders_skipped")
		return None, "before pull cutoff"

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
			existing = find_sales_order(cfg, order_id, order_number)
			if existing:
				if run:
					run.bump("orders_updated")
				_refresh_existing_safe(cfg, existing, order, run=run)
				return existing, "exists"
			try:
				so, result = _create_sales_order(cfg, order, run=run)
				if not getattr(so.flags, "shopify_downstream_failed", False):
					retry.resolve(cfg.name, order_id, getattr(so, "name", None))
				return so, result
			except ShopifyUnmappedProduct as exc:
				# A test/unlisted product: skip the whole order, create nothing.
				# Recorded so the merchant sees it and so it imports itself once
				# an Item Map is added (retried once a day).
				frappe.db.rollback()
				retry.remember(cfg.name, order, exc, skipped=True)
				if run:
					run.bump("orders_skipped")
					run.note(f"skipped {order.get('name')}: {exc}")
				return None, str(exc)
			except ShopifyBudgetExhausted:
				frappe.db.rollback()
				raise
			except Exception as exc:
				frappe.db.rollback()
				retry.remember(cfg.name, order, exc)
				log_error(
					"Shopify: order import failed",
					f"{cfg.name} / {order.get('name')}\n{frappe.get_traceback()}",
				)
				if run:
					run.fail("order", exc)
				return None, f"error: {exc}"
	except LockTimeoutError:
		if run:
			run.bump("orders_skipped")
		return None, "order is being imported by another worker"


def _refresh_existing_safe(cfg, so, order, run=None):
	if run:
		run.bump("orders_updated")
	try:
		refresh_existing(cfg, so, order, run=run)
		if not getattr(so.flags, "shopify_downstream_failed", False):
			retry.resolve(cfg.name, order.get("id"), so.name)
	except ShopifyUnmappedProduct as exc:
		retry.remember(cfg.name, order, exc, skipped=True, sales_order=so.name)
		if run:
			run.note(f"{so.name}: {exc}")
	except ShopifyBudgetExhausted:
		frappe.db.rollback()
		raise
	except Exception as exc:
		frappe.db.rollback()
		retry.remember(cfg.name, order, exc, sales_order=so.name)
		log_error(
			"Shopify: order refresh failed",
			f"{cfg.name} / {so.name}\n{frappe.get_traceback()}",
			"Sales Order",
			so.name,
		)
		if run:
			run.fail("refresh", exc, "Sales Order", so.name)


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

	_warn_total_mismatch(so, order, run=run)

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
			_fail(run, "invoice", exc, so, cfg, order)

	if cfg.create_delivery_note and order.get("fulfillments"):
		try:
			from .fulfillment import ensure_delivery_notes

			ensure_delivery_notes(cfg, so, order, run=run)
		except Exception as exc:
			_fail(run, "delivery note", exc, so, cfg, order)


def _should_invoice(order) -> bool:
	return is_paid(order)


def refresh_existing(cfg: StoreConfig, so, order, run=None):
	"""Refresh status, edited lines, refunds and cancelled fulfilments."""
	values = {
		ORDER_STATUS_FIELD: _status_text(order),
		ORDER_NUMBER_FIELD: str(order.get("name") or ""),
		"custom_shopify_synced_on": now(),
	}
	if not so.get(STORE_FIELD):
		values[STORE_FIELD] = cfg.name
	set_values("Sales Order", so.name, values)
	so.reload()

	if so.docstatus != 1:
		return

	if is_cancelled(order):
		# Controlled by the store setting (default on). A cancelled Shopify
		# order cancels its ERPNext Sales Order only when the merchant enabled
		# it; otherwise the status field alone is stamped above.
		if cfg.cancel_sales_order:
			cancel_sales_order(cfg, so, order, run=run)
		return

	_sync_order_changes(cfg, so, order, run=run)
	_note_refunds(cfg, so, order, run=run)
	_sync_cancelled_fulfilments(cfg, so, order, run=run)
	_after_import(cfg, so, order, run=run)


# ---------------------------------------------------------------------------
# Edits / refunds / cancelled fulfilments
# ---------------------------------------------------------------------------
def _sync_order_changes(cfg: StoreConfig, so, order, run=None):
	"""Apply a Shopify order edit to an order nobody has delivered/billed yet."""
	if flt(so.per_delivered) > 0 or flt(so.per_billed) > 0:
		# Already executed: never rewrite quantities under a delivery/invoice.
		_warn_total_mismatch(so, order, run=run)
		return

	company_cfg = cfg.company_config(so.company)
	try:
		items = ensure_items(cfg, order, budget=_budget(run), run=run)
	except ShopifyUnmappedProduct as exc:
		# The order can still be invoiced/delivered with its existing lines;
		# only the edit cannot be applied. Record it and carry on.
		retry.remember(cfg.name, order, exc, skipped=True, sales_order=so.name)
		if run:
			run.note(f"{so.name}: edit skipped, {exc}")
		_warn_total_mismatch(so, order, run=run)
		return
	cost_center = cost_center_for(cfg, company_cfg)
	for item in items:
		item["cost_center"] = cost_center
	taxes = get_order_taxes(cfg, company_cfg, order, items, cost_center=cost_center)

	current = [(row.item_code, row.qty, row.rate) for row in so.items]
	incoming = [(row["item_code"], row["qty"], row["rate"]) for row in items]
	if not order_items_changed(current, incoming):
		_warn_total_mismatch(so, order, run=run)
		return

	so.set("items", items)
	so.set("taxes", taxes)
	# ERPNext's own "Update Items" flow uses this flag on submitted orders.
	so.flags.ignore_validate_update_after_submit = True
	so.flags.ignore_permissions = True
	so.save(ignore_permissions=True)
	frappe.db.commit()
	if run:
		run.note(f"updated {so.name} from a Shopify edit")
	log_error(
		"Shopify: order edited after import",
		f"{cfg.name} / {so.name}: line items were rewritten from Shopify "
		f"(nothing delivered or billed yet).",
		"Sales Order",
		so.name,
	)
	_warn_total_mismatch(so, order, run=run)


def _note_refunds(cfg: StoreConfig, so, order, run=None):
	"""Record Shopify refunds on the order; never post to the ledger.

	Returns/COD are settled by the courier process (PostEx), so an automatic
	credit note here would double-count. The comment is the audit trail.
	"""
	refunds = order.get("refunds") or []
	if not refunds:
		return
	amount = 0.0
	for refund in refunds:
		for transaction in refund.get("transactions") or []:
			amount += abs(flt(transaction.get("amount")))
	_comment_once(
		so,
		"Shopify refund recorded",
		f"Shopify refund recorded: {money(amount)} "
		f"({order.get('financial_status') or 'refund'}). Not posted to the ledger — "
		"returns and COD settlements are handled by the courier process.",
	)


def _sync_cancelled_fulfilments(cfg: StoreConfig, so, order, run=None):
	"""Cancel the Delivery Note of a fulfillment Shopify has cancelled."""
	from .fulfillment import find_delivery_note

	for fulfillment in order.get("fulfillments") or []:
		if (fulfillment.get("status") or "").lower() != "cancelled":
			continue
		name = find_delivery_note(cfg, fulfillment.get("id"))
		if not name:
			continue
		dn = frappe.get_doc("Delivery Note", name)
		if dn.docstatus != 1:
			continue
		try:
			dn.flags.ignore_permissions = True
			dn.cancel()
			frappe.db.commit()
			if run:
				run.note(f"cancelled {dn.name}: Shopify fulfilment was cancelled")
		except Exception:
			frappe.db.rollback()
			log_error(
				"Shopify: could not cancel Delivery Note",
				f"{cfg.name} / {dn.name} / fulfilment {fulfillment.get('id')}\n"
				f"{frappe.get_traceback()}",
				"Delivery Note",
				dn.name,
			)


def _comment_once(doc, marker: str, text: str):
	try:
		if frappe.db.exists(
			"Comment",
			{
				"reference_doctype": doc.doctype,
				"reference_name": doc.name,
				"content": ("like", f"%{marker}%"),
			},
		):
			return
		doc.add_comment(text=text)
		frappe.db.commit()
	except Exception:
		pass


def _warn_total_mismatch(so, order, run=None):
	"""The invoice must equal what Shopify charged; log it once per order."""
	expected_total = flt(order.get("current_total_price") or order.get("total_price"))
	if not expected_total:
		return
	if abs(flt(so.grand_total) - expected_total) <= 0.05:
		return
	message = (
		f"{so.name} / Shopify {order.get('name')}: ERPNext {so.grand_total} vs "
		f"Shopify {expected_total}"
	)
	if frappe.db.exists(
		"Comment",
		{
			"reference_doctype": "Sales Order",
			"reference_name": so.name,
			"content": ("like", "%Shopify total mismatch%"),
		},
	):
		return
	log_error("Shopify: order total mismatch", message, "Sales Order", so.name)
	_comment_once(so, "Shopify total mismatch", f"Shopify total mismatch: {message}")
	if run:
		run.note(f"total mismatch {so.name}: {so.grand_total} vs {expected_total}")


# ---------------------------------------------------------------------------
# Webhook handlers / poll
# ---------------------------------------------------------------------------
def handle_webhook_order(payload, store=None, request_id=None):
	cfg = config(store)
	with Run(cfg.name, mode="Orders", trigger="Webhook") as run:
		so, result = import_order(cfg, payload, run=run, trigger="Webhook")
	return {"order": payload.get("name"), "sales_order": getattr(so, "name", None), "result": result}


def handle_webhook_cancelled(payload, store=None, request_id=None):
	cfg = config(store)
	with Run(cfg.name, mode="Cancellations", trigger="Webhook") as run:
		so = find_sales_order(cfg, payload.get("id"), payload.get("name"))
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
	# Only a poll that ran to completion may move the cursor. Any other exit
	# (401, 5xx after retries, timeout, DB error) leaves it untouched so the
	# next tick re-reads the same window instead of skipping it.
	completed = False
	poll_params = {"status": "any", "updated_at_min": _shopify_time(since)}
	if cfg.pull_orders_since:
		# Let Shopify do the filtering: no new historical order is returned.
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
				retry.remember(cfg.name, order, exc)
		completed = True
	except ShopifyBudgetExhausted:
		# Do NOT advance the cursor: orders after the budget cut-off would be
		# skipped forever. The next tick re-reads from the same point (imports
		# are idempotent).
		if run:
			run.budget_exhausted = 1
	except Exception as exc:
		if run:
			run.fail("poll", exc)
		log_error("Shopify: poll failed", f"{cfg.name}\n{frappe.get_traceback()}")
		raise
	finally:
		if completed:
			set_values(STORE_DOCTYPE, cfg.name, {"last_order_sync_on": now()}, commit=True)
	return count


def sync_old_orders(cfg: StoreConfig, from_datetime, to_datetime, run: Run):
	"""Backfill by created_at range (the old connector's 'Sync Old Orders')."""
	client = ShopifyClient(cfg, budget=_budget(run))
	count = 0
	created = 0
	completed = False
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
				retry.remember(cfg.name, order, exc)
		completed = True
	except ShopifyBudgetExhausted:
		# Keep the flag set: the next run replays the range (imports are
		# idempotent) instead of silently losing the rest of the backfill.
		run.budget_exhausted = 1
		run.note("request budget exhausted; the range will resume on the next run")
	except Exception as exc:
		run.fail("backfill", exc)
		log_error("Shopify: old-order backfill failed", f"{cfg.name}\n{frappe.get_traceback()}")
		raise
	finally:
		# Never move the poll cursor from a backfill: it would skip orders that
		# changed between the backfill and the next poll. Only a completed run
		# clears the flag (a crash resumes instead of losing the range).
		if completed:
			set_values(STORE_DOCTYPE, cfg.name, {"sync_old_orders": 0}, commit=True)
	return {"orders": count, "created": created, "completed": completed}


def import_order_by_id(cfg: StoreConfig, order_id, run: Run | None = None, force: bool = False):
	client = ShopifyClient(cfg, budget=_budget(run))
	order = client.order(order_id)
	if not order:
		raise ShopifyConfigError(f"Shopify order {order_id} not found in {cfg.name}.")
	return import_order(cfg, order, run=run, trigger="Manual" if not force else "Retry", force=force)


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
	"""ERPNext (site-local) datetime -> UTC ISO-8601 with a `Z` suffix."""
	return shopify_time(value, get_system_timezone())


def _budget(run):
	return run.budget if run and getattr(run, "budget", None) else None


def _fail(run, step, exc, so, cfg, order):
	frappe.db.rollback()
	so.flags.shopify_downstream_failed = True
	log_error(f"Shopify: {step} failed", f"{so.name}\n{frappe.get_traceback()}", "Sales Order", so.name)
	# The Sales Order exists; a retry only needs to re-run the downstream step,
	# which `refresh_existing` does. Queue it instead of waiting for the poll.
	retry.remember(cfg.name, order, f"{step}: {exc}", sales_order=so.name)
	if run:
		run.fail(step, exc, "Sales Order", so.name)
