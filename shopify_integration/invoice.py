"""Paid Shopify order -> Sales Invoice (+ optional Payment Entry).

The legacy connector created the invoice on `orders/paid` when the merchant
enabled it. This module keeps that behaviour but:
  * resolves the company (and its cash/bank account, series, cost center) from
    the store's company row;
  * is idempotent on `(store, shopify_order_id)`;
  * never creates a second invoice for the same order, even if the webhook and
    the poll race.
"""

import frappe
from frappe.utils import getdate, nowdate

from .config import StoreConfig, config, cost_center_for, naming_series
from .constants import (
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	POSTING_ORDER_DATE,
	POSTING_PAID_DATE,
	STORE_FIELD,
)
from .order import find_invoice, find_sales_order
from .payloads import normalize_datetime
from .utils import Run, log_error, now


def ensure_invoice(cfg: StoreConfig, so, order: dict, run: Run | None = None):
	if not cfg.enabled or not cfg.pull_enabled or not cfg.create_sales_invoice or cfg.dry_run:
		return None
	if so.docstatus != 1:
		return None

	order_id = str(order.get("id"))
	existing = find_invoice(cfg, order_id)
	if existing:
		return existing

	from frappe.utils.synchronization import LockTimeoutError, filelock

	try:
		with filelock(f"shopify-invoice-{cfg.name}-{order_id}", timeout=0):
			existing = find_invoice(cfg, order_id)
			if existing:
				return existing
			return _create_invoice(cfg, so, order, run=run)
	except LockTimeoutError:
		return None


def _create_invoice(cfg: StoreConfig, so, order, run=None):
	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

	company_cfg = cfg.company_config(so.company)
	posting_date = _posting_date(company_cfg, order)
	cost_center = cost_center_for(cfg, company_cfg)

	si = make_sales_invoice(so.name, ignore_permissions=True)
	si.set(ORDER_ID_FIELD, str(order.get("id")))
	si.set(ORDER_NUMBER_FIELD, str(order.get("name") or ""))
	si.set(ORDER_STATUS_FIELD, (order.get("financial_status") or "").strip())
	si.set(STORE_FIELD, cfg.name)
	si.naming_series = naming_series(company_cfg, "Sales Invoice")
	si.set_posting_time = 1
	si.posting_date = posting_date
	si.due_date = posting_date
	si.flags.ignore_mandatory = True
	si.flags.ignore_permissions = True

	for item in si.items:
		if cost_center:
			item.cost_center = cost_center

	try:
		si.insert(ignore_permissions=True, ignore_mandatory=True)
		si.submit()
	except Exception:
		frappe.db.rollback()
		raise

	frappe.db.commit()
	if run:
		run.bump("invoices_created")

	frappe.db.set_value(
		"Sales Order", so.name, "custom_shopify_synced_on", now(), update_modified=False
	)

	if company_cfg.create_payment_entry:
		_make_payment_entry(cfg, company_cfg, si, run=run)

	if order.get("note"):
		si.add_comment(text=f"Shopify order note: {order.get('note')}")

	return si.name


def _posting_date(company_cfg, order):
	basis = company_cfg.invoice_posting_basis
	if basis == POSTING_PAID_DATE:
		value = order.get("processed_at") or order.get("closed_at") or order.get("created_at")
	elif basis == POSTING_ORDER_DATE:
		value = order.get("created_at")
	else:
		return getdate(nowdate())
	parsed = normalize_datetime(value)
	return parsed.date() if parsed else getdate(nowdate())


def _make_payment_entry(cfg, company_cfg, si, run=None):
	"""Record the money Shopify already collected, into the mapped bank/cash."""
	if si.docstatus != 1 or si.grand_total <= 0:
		return None
	if si.outstanding_amount <= 0:
		return None
	if si.status in ("Paid", "Return"):
		return None

	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	try:
		payment = get_payment_entry(
			"Sales Invoice",
			si.name,
			bank_account=company_cfg.cash_bank_account or None,
		)
	except Exception as exc:
		log_error(
			"Shopify: payment entry could not be created",
			f"{cfg.name} / {si.name}\n{exc}\n{frappe.get_traceback()}",
			"Sales Invoice",
			si.name,
		)
		return None

	payment.flags.ignore_mandatory = True
	payment.reference_no = si.name
	payment.reference_date = si.posting_date
	payment.posting_date = si.posting_date
	try:
		payment.insert(ignore_permissions=True)
		payment.submit()
		frappe.db.commit()
	except Exception as exc:
		frappe.db.rollback()
		log_error(
			"Shopify: payment entry submit failed",
			f"{cfg.name} / {si.name}\n{exc}\n{frappe.get_traceback()}",
			"Sales Invoice",
			si.name,
		)
		return None

	if run:
		run.bump("payments_created")
	return payment.name


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
def handle_webhook_paid(payload, store=None, request_id=None):
	cfg = config(store)
	if cfg.is_historical(payload):
		return {"order": payload.get("name"), "result": "before pull cutoff"}
	with Run(cfg.name, mode="Invoices", trigger="Webhook") as run:
		so = find_sales_order(cfg, payload.get("id"))
		if not so:
			# `orders/paid` can arrive before `orders/create` is processed.
			from .order import import_order

			so, _result = import_order(cfg, payload, run=run, trigger="Webhook")
		if not so:
			return {"order": payload.get("name"), "result": "sales order not available"}

		# `find_sales_order` may return a freshly adopted row; reload before use.
		so.reload()
		invoice = ensure_invoice(cfg, so, payload, run=run)
	return {
		"order": payload.get("name"),
		"sales_order": so.name,
		"invoice": invoice,
		"result": "created" if invoice else "nothing to do",
	}
