"""Shared helpers: logging, run records, atomic claims, dates, phones.

Everything here is deliberately Frappe-aware; the pure helpers live in
`payloads.py` and `routing.py` so they can be tested without a site.
"""

import json
import re

import frappe
from frappe.utils import cint, flt, get_datetime, now_datetime, nowdate

from .constants import SYNC_RUN_DOCTYPE, WEBHOOK_LOG_DOCTYPE

_PHONE_CLEAN = re.compile(r"[^\d+]")


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
def now():
	return now_datetime()


def today():
	return nowdate()


def as_datetime(value):
	"""Parse a Shopify timestamp / ERPNext value; None when empty."""
	if not value:
		return None
	try:
		return get_datetime(value)
	except Exception:
		return None


# ---------------------------------------------------------------------------
# Errors / logs
# ---------------------------------------------------------------------------
def log_error(title: str, message: str, reference_doctype=None, reference_name=None):
	"""One error line in Error Log, tied to the document when known."""
	frappe.log_error(
		title=(title or "Shopify Integration error")[:140],
		message=message or "",
		reference_doctype=reference_doctype,
		reference_name=reference_name,
	)


def dump_snippet(value, limit: int = 900) -> str:
	try:
		return json.dumps(value, default=str)[:limit]
	except Exception:
		return str(value)[:limit]


def normalize_phone(value) -> str:
	"""Digits only, keeping a leading `+`. Empty when there is no number."""
	if not value:
		return ""
	text = _PHONE_CLEAN.sub("", str(value))
	return text if len(text) >= 6 else ""


# ---------------------------------------------------------------------------
# Run records
# ---------------------------------------------------------------------------
class Run:
	"""A `Shopify Sync Run` row with counters and a failure table.

	The row is created when the run starts so a crashed worker still leaves
	evidence (the document is committed at `finish`; a hard crash leaves it
	Queued, which the digest reports as a stale run).
	"""

	COUNTERS = (
		"orders_seen",
		"orders_created",
		"orders_updated",
		"orders_skipped",
		"customers_created",
		"customers_updated",
		"items_created",
		"invoices_created",
		"delivery_notes_created",
		"payments_created",
		"cancelled",
		"inventory_pushed",
		"items_pushed",
		"requests_made",
	)

	def __init__(self, store: str, mode: str = "Orders", trigger: str = "Manual"):
		from .client import RequestBudget

		self.store = store
		self.mode = mode
		self.trigger = trigger
		self.counters = {name: 0 for name in self.COUNTERS}
		self.failures = []
		self.notes = []
		self.budget_exhausted = 0
		self.doc = None
		self._started_on = now()

		# One budget shared by every client call of this run, so a broad sync
		# stops cleanly instead of running for hours.
		limit = 800
		try:
			limit = cint(frappe.db.get_value("Shopify Store", store, "run_request_budget")) or 800
		except Exception:
			pass
		self.budget = RequestBudget(limit)

	def __enter__(self):
		self.start()
		return self

	def __exit__(self, exc_type, exc, tb):
		if exc_type:
			self.fail("run", str(exc))
			self.finish("Failed", note=f"{exc_type.__name__}: {exc}")
			return False
		self.finish("Success")
		return False

	def start(self):
		try:
			self.doc = frappe.get_doc(
				{
					"doctype": SYNC_RUN_DOCTYPE,
					"store": self.store,
					"mode": self.mode,
					"trigger": self.trigger,
					"started_on": self._started_on,
					"status": "Running",
				}
			)
			self.doc.insert(ignore_permissions=True)
			frappe.db.commit()
		except Exception:
			self.doc = None
			log_error("Shopify: could not create sync run", frappe.get_traceback())
		return self.doc

	def bump(self, key: str, amount: int = 1):
		if key in self.counters:
			self.counters[key] = cint(self.counters[key]) + cint(amount)

	def fail(self, step: str, error, doctype=None, name=None):
		self.failures.append(
			{
				"step": (step or "unknown")[:140],
				"reference_doctype": doctype,
				"reference_name": name,
				"error": (str(error) or "")[:2000],
			}
		)
		if len(self.failures) > 500:
			self.failures = self.failures[:500]

	def note(self, text: str):
		if text:
			self.notes.append(str(text))

	def finish(self, status: str = "Success", note: str | None = None):
		if note:
			self.notes.append(note)
		try:
			self.counters["requests_made"] = self.budget.spent
			if self.budget.exhausted():
				self.budget_exhausted = 1
		except Exception:
			pass
		if status == "Success" and self.failures:
			status = "Partial"
		summary = ", ".join(f"{value} {key.replace('_', ' ')}" for key, value in self.counters.items() if value)
		if self.failures:
			summary += f", {len(self.failures)} errors"
		if self.budget_exhausted:
			summary += ", request budget exhausted"
		if not summary:
			summary = "nothing to do"
		message = f"[{self.store}] {self.mode}: {summary}"
		if self.notes:
			message += " | " + "; ".join(self.notes[:5])

		frappe.db.set_value(
			"Shopify Store",
			self.store,
			{"last_run_result": message[:1000], "last_run_on": self._started_on},
			update_modified=False,
		)

		if not self.doc:
			return message
		try:
			self.doc.reload()
			self.doc.status = status
			self.doc.finished_on = now()
			for key, value in self.counters.items():
				setattr(self.doc, key, value)
			self.doc.budget_exhausted = self.budget_exhausted
			self.doc.note = "\n".join(self.notes)[:5000]
			self.doc.set("failures", [])
			for failure in self.failures:
				self.doc.append("failures", failure)
			self.doc.save(ignore_permissions=True)
			frappe.db.commit()
		except Exception:
			log_error("Shopify: could not finish sync run", frappe.get_traceback())
		return message


# ---------------------------------------------------------------------------
# Webhook log
# ---------------------------------------------------------------------------
def log_webhook(store: str, topic: str, payload, status: str, result: str = "", webhook_id: str = "", traceback: str = ""):
	try:
		doc = frappe.get_doc(
			{
				"doctype": WEBHOOK_LOG_DOCTYPE,
				"store": store,
				"topic": topic,
				"shopify_webhook_id": webhook_id,
				"shopify_order_id": str((payload or {}).get("id") or "") or None,
				"status": status,
				"result": (result or "")[:1000],
				"traceback": traceback,
				"request_data": dump_snippet(payload, 60000),
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name
	except Exception:
		log_error("Shopify: webhook log failed", frappe.get_traceback())
		return None


# ---------------------------------------------------------------------------
# Atomic field claim
# ---------------------------------------------------------------------------
def claim_field(doctype: str, name: str, fieldname: str, value: str, allow=None) -> bool:
	"""Store `value` in a link field only if nobody else owns it.

	`frappe.db.set_value` writes unconditionally, so two runs that both create a
	document can both "write then read back" their own name and both keep it.
	A conditional UPDATE is the real guard: only the first writer seeing an
	empty field stores its value, and the read-back tells the loser the truth.
	`allow` accepts one specific stale value (an interrupted run's dead claim).
	"""
	if not fieldname.isidentifier():
		raise ValueError(f"unsafe field name: {fieldname!r}")

	condition = f"ifnull(`{fieldname}`, '') = ''"
	params = [value, now(), name]
	if allow:
		condition = f"({condition} or `{fieldname}` = %s)"
		params.append(allow)

	frappe.db.sql(
		f"""
		update `tab{doctype}`
		set `{fieldname}` = %s, `modified` = %s
		where `name` = %s
		  and {condition}
		""",
		tuple(params),
	)
	frappe.db.commit()
	return frappe.db.get_value(doctype, name, fieldname) == value


def set_values(doctype: str, name: str, values: dict, commit: bool = False):
	if not values:
		return
	frappe.db.set_value(doctype, name, values, update_modified=False)
	if commit:
		frappe.db.commit()


# ---------------------------------------------------------------------------
# Frappe field helpers
# ---------------------------------------------------------------------------
def field_exists(doctype: str, fieldname: str) -> bool:
	try:
		return frappe.get_meta(doctype).has_field(fieldname)
	except Exception:
		return False


def has_any_field(doctype: str, *fieldnames) -> bool:
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return False
	return all(meta.has_field(field) for field in fieldnames)


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------
def money(value) -> float:
	return flt(value, 2)


def net_rate(amount, qty) -> float:
	qty = flt(qty)
	if qty <= 0:
		return 0.0
	return money(flt(amount) / qty)


def truthy(value) -> bool:
	return bool(cint(value))
