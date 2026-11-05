"""Paid Shopify order -> Sales Invoice (+ optional Payment Entry).

The legacy connector created the invoice on `orders/paid` when the merchant
enabled it. This module keeps that behaviour but:
  * resolves the company (and its cash/bank account, series, cost center) from
    the store's company row;
  * is idempotent on `(store, shopify_order_id)`;
  * never bills an order that another process (PostEx / a manual invoice) has
    already billed;
  * claims the Payment Entry with a conditional UPDATE, so a crash or a second
    worker can never create two payments for one invoice.
"""

import frappe
from frappe.utils import getdate, nowdate

from .config import StoreConfig, config, cost_center_for, naming_series
from .constants import (
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	PAYMENT_ENTRY_FIELD,
	POSTING_ORDER_DATE,
	POSTING_PAID_DATE,
	STORE_FIELD,
)
from .order import find_invoice, find_sales_order, _refresh_existing_safe
from .payloads import invoice_may_be_created
from .utils import Run, claim_field, field_exists, log_error, now, site_datetime


def ensure_invoice(cfg: StoreConfig, so, order: dict, run: Run | None = None):
	if not cfg.enabled or not cfg.pull_enabled or not cfg.create_sales_invoice or cfg.dry_run:
		return None
	if so.docstatus != 1:
		return None

	order_id = str(order.get("id"))
	existing = find_invoice(cfg, order_id)
	if existing:
		# Resume an interrupted run: the invoice exists, maybe the payment does
		# not (a crash between the two used to leave it unpaid forever).
		_resume_payment(cfg, so, existing, run=run)
		return existing

	from frappe.utils.synchronization import LockTimeoutError, filelock

	try:
		with filelock(f"shopify-invoice-{cfg.name}-{order_id}", timeout=0):
			existing = find_invoice(cfg, order_id)
			if existing:
				_resume_payment(cfg, so, existing, run=run)
				return existing

			other = _existing_so_invoice(so)
			if not invoice_may_be_created(so.get("per_billed"), bool(other)):
				if other:
					# PostEx or a manual invoice already billed this order.
					# Creating another would double revenue and receivable.
					if run:
						run.note(f"{so.name} is already invoiced by {other}; Shopify invoice skipped")
					log_error(
						"Shopify: invoice skipped, order already billed",
						f"{cfg.name} / {so.name} is already invoiced by {other}.\n"
						"Set 'Create Sales Invoice when Paid' off for this store, or the "
						"invoice would be duplicated.",
						"Sales Order",
						so.name,
					)
				return other
			return _create_invoice(cfg, so, order, run=run)
	except LockTimeoutError:
		return None


def _existing_so_invoice(so):
	"""Any submitted, non-return invoice raised against this Sales Order."""
	rows = frappe.db.sql(
		"""
		select distinct si.name
		from `tabSales Invoice` si
		join `tabSales Invoice Item` sii on sii.parent = si.name
		where sii.sales_order = %s and si.docstatus = 1 and ifnull(si.is_return, 0) = 0
		order by si.creation desc
		limit 1
		""",
		so.name,
	)
	return rows[0][0] if rows else None


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
	parsed = site_datetime(value)
	return parsed.date() if parsed else getdate(nowdate())


# ---------------------------------------------------------------------------
# Payment Entry (idempotent, resumable)
# ---------------------------------------------------------------------------
def _resume_payment(cfg: StoreConfig, so, invoice_name, run=None):
	if not cfg.create_payment_entry or not invoice_name:
		return None
	try:
		company_cfg = cfg.company_config(so.company)
		si = frappe.get_doc("Sales Invoice", invoice_name)
		return _make_payment_entry(cfg, company_cfg, si, run=run)
	except Exception as exc:
		log_error(
			"Shopify: payment entry could not be resumed",
			f"{cfg.name} / {invoice_name}\n{exc}\n{frappe.get_traceback()}",
			"Sales Invoice",
			invoice_name,
		)
		return None


def _make_payment_entry(cfg, company_cfg, si, run=None):
	"""Record the money Shopify already collected, into the mapped bank/cash.

	Idempotent: an existing Payment Entry for the invoice (or a claimed link
	field) stops a second one, and a lost race cancels the one just created.
	"""
	if si.docstatus != 1 or si.grand_total <= 0:
		return None
	if si.outstanding_amount <= 0:
		return None
	if si.status in ("Paid", "Return"):
		return None

	existing = _payment_for_invoice(si)
	if existing:
		_claim_payment(si.name, existing)
		return existing

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

	# Conditional claim: if another worker already stored a different Payment
	# Entry for this invoice, ours is the duplicate and is cancelled.
	if not _claim_payment(si.name, payment.name):
		_rollback_duplicate_payment(payment)
		return _payment_for_invoice(si)

	if run:
		run.bump("payments_created")
	return payment.name


def _payment_for_invoice(si):
	"""A submitted Payment Entry already linked to this invoice, or None."""
	if field_exists("Sales Invoice", PAYMENT_ENTRY_FIELD):
		claimed = frappe.db.get_value("Sales Invoice", si.name, PAYMENT_ENTRY_FIELD)
		if claimed:
			docstatus = frappe.db.get_value("Payment Entry", claimed, "docstatus")
			if docstatus == 1:
				return claimed

	rows = frappe.db.sql(
		"""
		select pe.name
		from `tabPayment Entry` pe
		join `tabPayment Entry Reference` per on per.parent = pe.name
		where per.reference_doctype = 'Sales Invoice'
		  and per.reference_name = %s
		  and pe.docstatus = 1
		order by pe.creation desc
		limit 1
		""",
		si.name,
	)
	return rows[0][0] if rows else None


def _claim_payment(invoice_name, payment_name) -> bool:
	if not field_exists("Sales Invoice", PAYMENT_ENTRY_FIELD):
		# Field not migrated yet: the link table above is still the guard.
		return True
	return claim_field("Sales Invoice", invoice_name, PAYMENT_ENTRY_FIELD, payment_name)


def _rollback_duplicate_payment(payment):
	try:
		payment.reload()
		if payment.docstatus == 1:
			payment.flags.ignore_permissions = True
			payment.cancel()
			frappe.db.commit()
	except Exception:
		log_error(
			"Shopify: could not cancel duplicate payment entry",
			f"{payment.name}\n{frappe.get_traceback()}",
			"Payment Entry",
			payment.name,
		)


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
def handle_webhook_paid(payload, store=None, request_id=None):
	cfg = config(store)
	with Run(cfg.name, mode="Invoices", trigger="Webhook") as run:
		so = find_sales_order(cfg, payload.get("id"), payload.get("name"))
		if so:
			# Refresh first: a paid event also carries the final status/edits,
			# and `orders/updated` is not guaranteed to follow.
			_refresh_existing_safe(cfg, so, payload, run=run)
		else:
			from .order import import_order

			so, _result = import_order(cfg, payload, run=run, trigger="Webhook")
		if not so:
			return {"order": payload.get("name"), "result": "sales order not available"}

		so.reload()
		invoice = ensure_invoice(cfg, so, payload, run=run)
	return {
		"order": payload.get("name"),
		"sales_order": so.name,
		"invoice": invoice,
		"result": "created" if invoice else "nothing to do",
	}
