"""Guest webhook endpoint.

    /api/method/shopify_integration.webhook.receive/<store>/<webhook_key>

Shopify cannot log in as an ERPNext user, so the endpoint is guest-callable and
authenticated by the per-store HMAC signature (`X-Shopify-Hmac-Sha256`), exactly
like Shopify intends. The unguessable `webhook_key` path segment is an extra
defence and also selects the store when the shop-domain header is absent.

The webhook itself creates **no documents**: it verifies, logs, and enqueues a
handler. That keeps one writer for stock/revenue and makes retries safe.
"""

import hmac
import json

import frappe
from frappe.utils import add_to_date, cint, get_datetime

from .config import find_store_by_domain, get_store
from .constants import WEBHOOK_HANDLERS, WEBHOOK_LOG_DOCTYPE
from .oauth import webhook_secret
from .payloads import valid_shopify_signature
from .utils import log_error, log_webhook, now

@frappe.whitelist(allow_guest=True)  # nosemgrep: guest endpoint authenticated by HMAC
def receive(store=None, key=None):
	store_doc, error = _resolve_store(store, key)
	if error:
		return _respond(False, error, status_code=404)
	raw_body = _raw_body()
	topic = _header("X-Shopify-Topic") or _form("topic") or ""
	webhook_id = _header("X-Shopify-Webhook-Id") or ""
	hmac_header = _header("X-Shopify-Hmac-Sha256") or ""

	secret = webhook_secret(store_doc)
	if not secret:
		log_error(
			"Shopify webhook rejected: no secret",
			f"store={store_doc.name} topic={topic}",
		)
		return _respond(False, "webhook secret is not configured", status_code=403)

	if not _valid_signature(raw_body, hmac_header, secret):
		log_error(
			"Shopify webhook rejected: bad signature",
			f"store={store_doc.name} topic={topic} remote={getattr(frappe.local.request, 'remote_addr', '?')}",
		)
		return _respond(False, "invalid signature", status_code=401)

	try:
		payload = json.loads(raw_body) if raw_body else dict(frappe.form_dict or {})
	except ValueError:
		return _respond(False, "invalid JSON", status_code=400)

	if webhook_id:
		duplicate = frappe.db.get_value(
			WEBHOOK_LOG_DOCTYPE,
			{"shopify_webhook_id": webhook_id, "status": ("in", ["Queued", "Running", "Success"])},
			["name", "modified", "status"],
			as_dict=True,
			order_by="creation desc",
		)
		if duplicate:
			# Shopify retries when it does not see a 200; duplicate deliveries
			# are normal. A Queued/Running row older than 10 minutes means the
			# worker died, so let the retry through instead of suppressing it.
			stale = get_datetime(duplicate.modified) < add_to_date(now(), minutes=-10)
			if duplicate.status == "Success" or not stale:
				return _respond(True, "duplicate delivery ignored")

	if not cint(store_doc.enabled):
		log_webhook(store_doc.name, topic, payload, status="Ignored", result="store disabled", webhook_id=webhook_id)
		return _respond(True, "store disabled")

	handler = WEBHOOK_HANDLERS.get(topic)
	if not handler:
		log_webhook(
			store_doc.name, topic, payload, status="Ignored", result=f"unhandled topic {topic}", webhook_id=webhook_id
		)
		return _respond(True, f"topic {topic} is not handled")

	log_name = log_webhook(
		store_doc.name,
		topic,
		payload,
		status="Queued",
		result=handler,
		webhook_id=webhook_id,
	)
	frappe.db.set_value("Shopify Store", store_doc.name, "last_webhook_on", now(), update_modified=False)
	frappe.db.commit()

	frappe.enqueue(
		"shopify_integration.webhook.run_handler",
		queue="short",
		timeout=900,
		is_async=True,
		enqueue_after_commit=True,
		job_name=f"shopify-webhook-{webhook_id or log_name}",
		handler_path=handler,
		payload=payload,
		store=store_doc.name,
		request_id=log_name,
	)

	return _respond(True, "queued", log_name=log_name)


# ---------------------------------------------------------------------------
# Execution wrapper used by the enqueue
# ---------------------------------------------------------------------------
def run_handler(handler_path: str, payload: dict, store: str, request_id: str | None = None):
	"""Run a handler and mark the webhook log; used by tests / manual replay."""
	# The webhook is enqueued from a guest request. Background jobs inherit that
	# user, and ERPNext's item permission check then rejects the Sales Order.
	# The connector always writes as Administrator, like the legacy app did.
	frappe.set_user("Administrator")
	_bump_attempts(request_id)
	handler = frappe.get_attr(handler_path)
	try:
		result = handler(payload=payload, store=store, request_id=request_id)
	except Exception:
		_mark_log(request_id, "Error", frappe.get_traceback())
		raise
	_mark_log(request_id, "Success", json.dumps(result, default=str)[:1000])
	return result


def _bump_attempts(request_id):
	if not request_id or not frappe.db.exists(WEBHOOK_LOG_DOCTYPE, request_id):
		return
	try:
		attempts = cint(frappe.db.get_value(WEBHOOK_LOG_DOCTYPE, request_id, "attempts")) + 1
		frappe.db.set_value(
			WEBHOOK_LOG_DOCTYPE, request_id, "attempts", attempts, update_modified=False
		)
		frappe.db.commit()
	except Exception:
		pass


def retry_failed(limit: int = 20):
	"""Replay webhook deliveries that failed or whose worker died.

	Shopify considers a delivery done once it receives a 200, so a failed async
	job must be replayed here or the event is lost. `attempts` is capped so a
	permanently broken payload cannot loop forever.
	"""
	from frappe.utils import add_to_date

	from .constants import MAX_WEBHOOK_ATTEMPTS, WEBHOOK_STALE_MINUTES

	cutoff = add_to_date(now(), minutes=-WEBHOOK_STALE_MINUTES)
	filters = [
		["status", "=", "Error"],
		["attempts", "<", MAX_WEBHOOK_ATTEMPTS],
	]
	rows = frappe.get_all(
		WEBHOOK_LOG_DOCTYPE,
		filters=filters,
		fields=["name", "store", "topic", "request_data", "attempts"],
		order_by="creation asc",
		limit=limit,
	)
	stale = frappe.get_all(
		WEBHOOK_LOG_DOCTYPE,
		filters={
			"status": ("in", ["Queued", "Running"]),
			"modified": ("<", cutoff),
			"attempts": ("<", MAX_WEBHOOK_ATTEMPTS),
		},
		fields=["name", "store", "topic", "request_data", "attempts"],
		order_by="creation asc",
		limit=limit,
	)
	replayed = 0
	for row in rows + stale:
		handler_path = WEBHOOK_HANDLERS.get(row.topic)
		if not handler_path:
			continue
		try:
			payload = json.loads(row.request_data) if row.request_data else None
		except ValueError:
			payload = None
		if not payload:
			continue
		try:
			run_handler(handler_path, payload, row.store, row.name)
			replayed += 1
		except Exception:
			log_error(
				"Shopify: webhook replay failed",
				f"{row.name} / {row.topic}\n{frappe.get_traceback()}",
			)
	return replayed


def _mark_log(request_id, status, result):
	if not request_id or not frappe.db.exists(WEBHOOK_LOG_DOCTYPE, request_id):
		return
	frappe.db.set_value(
		WEBHOOK_LOG_DOCTYPE,
		request_id,
		{"status": status, "result": (result or "")[:1000], "traceback": frappe.get_traceback() if status == "Error" else ""},
		update_modified=False,
	)
	frappe.db.commit()


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------
def _resolve_store(value, key):
	"""Find the store from the path segment, then from the shop-domain header.

	The per-store `webhook_key` path segment is verified whenever the store is
	identified from the path: it is the extra defence the URL advertises. HMAC
	over the raw body remains the real authentication.
	"""
	if value:
		if frappe.db.exists("Shopify Store", value):
			return _check_key(get_store(value), key)
		# Some shops put the domain in the path instead of the store name.
		by_domain = find_store_by_domain(value)
		if by_domain:
			return _check_key(by_domain, key)

	domain = _header("X-Shopify-Shop-Domain") or _form("shop_domain")
	if domain:
		store_doc = find_store_by_domain(domain)
		if store_doc:
			return _check_key(store_doc, key)

	return None, "unknown store"


def _check_key(store_doc, key):
	expected = store_doc.get("webhook_key") or ""
	if not expected:
		# The store was saved without a key (old data); HMAC still applies.
		return store_doc, None
	if not key or not hmac.compare_digest(str(expected), str(key)):
		return None, "invalid webhook key"
	return store_doc, None


def _raw_body():
	try:
		data = frappe.request.get_data()
	except Exception:
		data = b""
	if isinstance(data, str):
		data = data.encode()
	if data:
		return data
	# Fallback for proxies that hand the body to Frappe's form parser.
	try:
		return json.dumps(dict(frappe.form_dict or {})).encode()
	except Exception:
		return b""


def _header(name):
	try:
		return frappe.get_request_header(name)
	except Exception:
		return None


def _form(name):
	return (frappe.form_dict or {}).get(name)


def _respond(ok, message, log_name=None, status_code=None):
	if status_code:
		frappe.local.response["http_status_code"] = status_code
	return {"ok": bool(ok), "message": message, "log": log_name}


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------
def valid_signature(body: bytes, signature: str, secret: str) -> bool:
	return valid_shopify_signature(body, signature, secret)


def _valid_signature(body: bytes, signature: str, secret: str) -> bool:
	return valid_shopify_signature(body, signature, secret)
