"""Scheduled entry points and the per-store lock helper.

Each cron tick only enqueues one short job per store; the lock and the heavy
lifting live in the enqueued function. A slow Shopify API for store A therefore
never delays store B, and a manual "Sync now" can never overlap a cron run of
the same store.
"""

import html

import frappe
from frappe.utils import add_days, add_to_date, cint, get_datetime, now_datetime

from .constants import STORE_DOCTYPE
from .utils import log_error


# ---------------------------------------------------------------------------
# Cron entry points
# ---------------------------------------------------------------------------
def poll_all_stores():
	# Enqueue the *locked* wrapper, not the raw worker: cron and a manual
	# "Poll Orders Now" must never process the same store concurrently.
	_enqueue_for_stores(
		"shopify_integration.tasks.manual_poll",
		"shopify-poll",
		filters={"enabled": 1, "enable_shopify_to_erpnext": 1, "pull_orders": 1},
	)


def push_inventory_all_stores():
	_enqueue_for_stores(
		"shopify_integration.tasks.scheduled_inventory",
		"shopify-inventory",
		filters={"enabled": 1, "enable_erpnext_to_shopify": 1, "push_inventory": 1},
	)


def old_order_sync():
	_enqueue_for_stores(
		"shopify_integration.tasks.manual_old_orders",
		"shopify-old-orders",
		filters={"enabled": 1, "enable_shopify_to_erpnext": 1, "sync_old_orders": 1},
	)


def webhook_watchdog():
	"""Alert when a store that pulls orders receives no webhook for 24 hours."""
	cutoff = add_to_date(now_datetime(), hours=-24)
	rows = frappe.get_all(
		STORE_DOCTYPE,
		filters={"enabled": 1, "enable_shopify_to_erpnext": 1, "pull_orders": 1},
		fields=["name", "last_webhook_on", "creation"],
	)
	for row in rows:
		if row.creation and get_datetime(row.creation) > get_datetime(cutoff):
			continue  # too new to judge
		last = get_datetime(row.last_webhook_on) if row.last_webhook_on else None
		if last and last > get_datetime(cutoff):
			continue
		try:
			received = frappe.db.count(
				"Shopify Webhook Log",
				{"store": row.name, "creation": (">=", cutoff)},
			)
			if received:
				continue
			message = (
				f"Shopify Store {row.name} received no webhook call in the last 24 hours "
				"while order pull is enabled.\n\n"
				"Open the store and press 'Register Webhooks' (or check the webhook address "
				"in Shopify > Settings > Notifications). Until it is fixed, orders still "
				"arrive through the 15-minute poll, but paid/fulfilled events are delayed."
			)
			frappe.log_error(title="Shopify webhook silent for 24h", message=message)
			_notify(f"Shopify webhook silent — {row.name}", message)
		except Exception:
			log_error("Shopify webhook watchdog failed", f"{row.name}\n{frappe.get_traceback()}")


def daily_digest():
	for row in frappe.get_all(STORE_DOCTYPE, filters={"enabled": 1}, pluck="name"):
		try:
			_digest_store(row)
		except Exception:
			log_error("Shopify daily digest failed", f"{row}\n{frappe.get_traceback()}")


def prune_logs(days: int = 90):
	cutoff = add_days(now_datetime().date(), -days)
	for doctype in ("Shopify Sync Run", "Shopify Webhook Log"):
		if not frappe.db.exists("DocType", doctype):
			continue
		try:
			names = frappe.get_all(
				doctype, filters={"creation": ("<", cutoff)}, pluck="name", limit=5000
			)
			for name in names:
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
			if names:
				frappe.db.commit()
		except Exception:
			log_error("Shopify log pruning failed", f"{doctype}\n{frappe.get_traceback()}")


# ---------------------------------------------------------------------------
# Manual entry points (enqueued by api.sync_now with background=1)
# ---------------------------------------------------------------------------
def manual_poll(store):
	return run_locked("manual-poll", store, poll_store_locked)


def manual_inventory(store):
	# The button/API is an explicit "do it now", so bypass the frequency gate.
	return run_locked("manual-inventory", store, push_inventory_locked, force=True)


def scheduled_inventory(store):
	return run_locked("inventory", store, push_inventory_locked, force=False)


def manual_old_orders(store):
	return run_locked("manual-old-orders", store, old_orders_locked)


# ---------------------------------------------------------------------------
# Locked workers
# ---------------------------------------------------------------------------
def poll_store_locked(store):
	from .config import StoreConfig
	from .exceptions import ShopifyBudgetExhausted
	from .order import poll_orders
	from .utils import Run

	cfg = StoreConfig(store)
	if not cfg.enabled or not cfg.pull_enabled or not cfg.pull_orders:
		return {"skipped": "pull disabled"}

	with Run(store, mode="Orders", trigger="Scheduler") as run:
		try:
			count = poll_orders(cfg, run)
			run.note(f"polled {count} updated orders")
		except ShopifyBudgetExhausted:
			run.budget_exhausted = 1
			run.note("request budget exhausted")
	return {"store": store, "seen": run.counters["orders_seen"]}


def push_inventory_locked(store, force=False):
	from .config import StoreConfig
	from .inventory import push_inventory
	from .utils import Run

	cfg = StoreConfig(store)
	if not cfg.enabled or not cfg.push_enabled or not cfg.push_inventory:
		return {"skipped": "inventory push disabled"}

	with Run(store, mode="Inventory", trigger="Scheduler") as run:
		pushed = push_inventory(cfg, run, force=force)
	return {"store": store, "pushed": pushed}


def old_orders_locked(store):
	from .config import StoreConfig
	from .exceptions import ShopifyBudgetExhausted
	from .order import sync_old_orders
	from .utils import Run

	cfg = StoreConfig(store)
	if not cfg.store.sync_old_orders:
		return {"skipped": "old order sync not requested"}
	if not cfg.store.old_orders_from or not cfg.store.old_orders_to:
		run = Run(store, mode="Old Orders", trigger="Scheduler")
		run.start()
		run.finish("Skipped", note="old_orders_from / old_orders_to are empty")
		return {"skipped": "date range missing"}

	with Run(store, mode="Old Orders", trigger="Scheduler") as run:
		try:
			result = sync_old_orders(cfg, cfg.store.old_orders_from, cfg.store.old_orders_to, run)
			run.note(f"old orders {result}")
		except ShopifyBudgetExhausted:
			run.budget_exhausted = 1
			run.note("request budget exhausted; range not fully imported")
	return {"store": store}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _enqueue_for_stores(method, job_prefix, filters):
	for row in frappe.get_all(STORE_DOCTYPE, filters=filters, pluck="name"):
		try:
			frappe.enqueue(
				method,
				queue="long",
				timeout=1800,
				is_async=True,
				# Unique per tick: a stale job_id must never suppress the next
				# tick. Overlap is handled by run_locked(), not by the job name.
				job_name=f"{job_prefix}-{row}-{now_datetime():%Y%m%d%H%M%S}",
				store=row,
			)
		except Exception:
			log_error("Shopify enqueue failed", f"{method} / {row}\n{frappe.get_traceback()}")


def run_locked(kind: str, store: str, fn, **kwargs):
	"""Run `fn(store, **kwargs)` under a per-store, per-kind file lock."""
	from frappe.utils.synchronization import filelock

	try:
		from frappe.exceptions import LockTimeoutError
	except ImportError:  # very old Frappe
		from frappe.utils.synchronization import LockTimeoutError  # type: ignore

	try:
		with filelock(f"shopify-{kind}-{store}", timeout=0):
			return fn(store, **kwargs)
	except LockTimeoutError:
		return {"skipped": f"{kind} already running for {store}"}


def _digest_store(store):
	cutoff = add_to_date(now_datetime(), hours=-24)
	cfg = frappe.get_doc(STORE_DOCTYPE, store)

	counts = {
		"orders": frappe.db.count(
			"Sales Order", {"custom_shopify_store": store, "creation": (">=", cutoff)}
		),
		"invoices": frappe.db.count(
			"Sales Invoice", {"custom_shopify_store": store, "creation": (">=", cutoff), "docstatus": 1}
		),
		"delivery notes": frappe.db.count(
			"Delivery Note", {"custom_shopify_store": store, "creation": (">=", cutoff), "docstatus": 1}
		),
		"webhooks": frappe.db.count("Shopify Webhook Log", {"store": store, "creation": (">=", cutoff)}),
		"errors": frappe.db.count(
			"Shopify Sync Run", {"store": store, "creation": (">=", cutoff), "status": "Failed"}
		),
	}
	unmapped = frappe.db.sql(
		"""
		select count(*) from `tabShopify Item Map`
		where store = %s and (item_code is null or item_code = '')
		""",
		(store,),
	)
	lines = [
		f"Shopify Store: {store} ({cfg.shop_domain})",
		f"Enabled: {'yes' if cint(cfg.enabled) else 'no'} · "
		f"Shopify -> ERPNext: {'on' if cint(cfg.enable_shopify_to_erpnext) else 'off'} · "
		f"ERPNext -> Shopify: {'on' if cint(cfg.enable_erpnext_to_shopify) else 'off'}",
		"",
		"Last 24 hours:",
	] + [f"  {name}: {value}" for name, value in counts.items()]
	lines += [
		f"  unmapped catalogue rows: {unmapped[0][0] if unmapped else 0}",
		f"Last order poll: {cfg.last_order_sync_on or 'never'}",
		f"Last webhook: {cfg.last_webhook_on or 'never'}",
		f"Last run: {cfg.last_run_result or 'never'}",
	]

	body = "\n".join(lines)
	message = "<pre>" + html.escape(body) + "</pre>"
	_notify(f"Shopify daily digest — {store}", message)

	if cfg.digest_recipients:
		try:
			frappe.sendmail(
				recipients=[r.strip() for r in cfg.digest_recipients.split(",") if r.strip()],
				subject=f"Shopify daily digest — {store}",
				message=message,
				now=False,
			)
		except Exception:
			log_error("Shopify digest email failed", f"{store}\n{frappe.get_traceback()}")


def _notify(subject, message):
	try:
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"subject": subject[:140],
				"email_content": message,
				"for_user": "Administrator",
				"type": "Alert",
			}
		).insert(ignore_permissions=True)
	except Exception:
		pass
