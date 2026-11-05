"""Order-import retry queue.

Every order that could not be imported (transient API error, missing tax
mapping, unmapped product, contention) is recorded in `Shopify Import Retry`.
A cron job replays the due rows with exponential backoff, so a failure no
longer disappears with the 60-minute poll overlap.

Two kinds of rows:

* `Pending`  - an error; retried 5, 10, 20 ... minutes apart (6 attempts).
* `Skipped`  - the order is fine but a product has no explicit Item Map. The
  merchant does not want every Shopify product auto-created, so the order is
  retried once a day: adding the mapping makes it import itself.
* `Resolved` - a later run imported it (webhook, poll or retry).
* `Failed`   - attempts exhausted; a human has to look at it.
"""

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from .constants import (
	MAX_IMPORT_ATTEMPTS,
	RETRY_DOCTYPE,
	STORE_DOCTYPE,
)
from .payloads import retry_delay_minutes


def _order_id(order) -> str:
	if isinstance(order, dict):
		return str(order.get("id") or "")
	return str(order or "")


def remember(store, order, error, skipped: bool = False, sales_order: str | None = None):
	"""Create or bump the retry row for one order. Never raises."""
	order_id = _order_id(order)
	if not order_id:
		return None
	number = (order.get("name") if isinstance(order, dict) else None) or ""

	try:
		name = frappe.db.get_value(
			RETRY_DOCTYPE, {"store": store, "shopify_order_id": order_id}, "name"
		)
		if name:
			doc = frappe.get_doc(RETRY_DOCTYPE, name)
			if doc.status == "Resolved":
				# It failed again after a successful import: reopen the row.
				doc.status = "Pending"
				doc.attempts = 0
				doc.resolved_on = None
		else:
			doc = frappe.get_doc(
				{
					"doctype": RETRY_DOCTYPE,
					"store": store,
					"shopify_order_id": order_id,
					"order_number": number,
				}
			)

		doc.attempts = cint(doc.attempts) + 1
		doc.order_number = number or doc.order_number
		doc.reason = ("unmapped product: " if skipped else "") + (str(error) or "")[:500]
		doc.last_error = (str(error) or "")[:5000]
		doc.sales_order = sales_order or doc.sales_order
		doc.status = "Skipped" if skipped else "Pending"
		if doc.attempts >= MAX_IMPORT_ATTEMPTS:
			doc.status = "Failed"
			doc.next_retry_on = None
		else:
			doc.next_retry_on = add_to_date(
				now_datetime(), minutes=retry_delay_minutes(doc.attempts, skipped=skipped)
			)
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		return doc.name
	except Exception:
		from .utils import log_error

		log_error(
			"Shopify: could not record import retry",
			f"store={store} order={order_id}\n{frappe.get_traceback()}",
		)
		return None


def resolve(store, order, sales_order: str | None = None):
	"""Mark the order as imported; called after any successful import."""
	order_id = _order_id(order)
	if not order_id:
		return
	try:
		name = frappe.db.get_value(
			RETRY_DOCTYPE, {"store": store, "shopify_order_id": order_id}, "name"
		)
		if not name:
			return
		doc = frappe.get_doc(RETRY_DOCTYPE, name)
		if doc.status == "Resolved":
			return
		doc.status = "Resolved"
		doc.resolved_on = now_datetime()
		doc.next_retry_on = None
		doc.sales_order = sales_order or doc.sales_order
		doc.save(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		pass


def due_rows(limit: int = 50):
	now = now_datetime()
	return frappe.get_all(
		RETRY_DOCTYPE,
		filters={
			"status": ("in", ["Pending", "Skipped"]),
			"next_retry_on": ("<=", now),
		},
		fields=["name", "store", "shopify_order_id", "order_number", "attempts", "status"],
		order_by="next_retry_on asc",
		limit=limit,
	)


def retry_due(limit: int = 50):
	"""Replay due rows; one store lock per store so it never overlaps a poll."""
	from .order import import_order_by_id
	from .tasks import run_locked
	from .utils import Run, log_error

	done = 0
	for row in due_rows(limit):
		store = row.store
		if not frappe.db.exists(STORE_DOCTYPE, store):
			continue
		# Do not spin on a store whose pull direction is off: the row waits
		# until it is re-enabled instead of being retried every ten minutes.
		flags = frappe.db.get_value(
			STORE_DOCTYPE,
			store,
			["enabled", "enable_shopify_to_erpnext", "pull_orders"],
			as_dict=True,
		) or {}
		if not (
			flags.get("enabled")
			and flags.get("enable_shopify_to_erpnext")
			and flags.get("pull_orders")
		):
			continue
		try:
			# Same per-store lock as the poll and the manual sync: a retry runs
			# as a normal poll-equivalent job, never concurrently with one.
			def worker(store_name, _row=row):
				from .config import StoreConfig

				cfg = StoreConfig(store_name)
				with Run(store_name, mode="Orders", trigger="Retry") as run:
					so, result = import_order_by_id(
						cfg, _row.shopify_order_id, run=run, force=True
					)
					run.note(f"retry {_row.shopify_order_id}: {result}")
					return so, result

			result = run_locked("manual-poll", store, worker)
			# `run_locked` returns {"skipped": ...} when the store is already
			# being synced; that is not a failure, the next tick retries.
			if isinstance(result, tuple) and result and result[0]:
				done += 1
		except Exception as exc:
			# The failure happened before `import_order` could record it (e.g.
			# the order no longer exists in Shopify): count the attempt so the
			# row cannot spin every ten minutes forever.
			remember(store, row.shopify_order_id, exc)
			log_error(
				"Shopify: retry run failed",
				f"{row.name} / {row.shopify_order_id}\n{frappe.get_traceback()}",
			)
	return done
