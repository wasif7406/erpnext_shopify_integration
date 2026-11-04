"""Shopify fulfillment -> ERPNext Delivery Note.

Only created when the store's `Create Delivery Notes` toggle is on, exactly like
the legacy connector. One Delivery Note per Shopify fulfillment, deduplicated on
`(store, shopify_fulfillment_id)`, with the warehouse resolved from the Shopify
location mapping (a store can ship from different warehouses per company).
"""

import frappe
from frappe.utils import getdate, nowdate

from .client import ShopifyClient
from .config import StoreConfig, config, naming_series, warehouse_for
from .constants import (
	FULFILLMENT_ID_FIELD,
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	STORE_FIELD,
)
from .exceptions import ShopifyConfigError
from .order import find_sales_order
from .payloads import normalize_datetime
from .product import get_item_code
from .utils import Run, log_error, now


def find_delivery_note(cfg: StoreConfig, fulfillment_id):
	if not fulfillment_id:
		return None
	for store_filter in (cfg.name, ("is", "not set")):
		name = frappe.db.get_value(
			"Delivery Note",
			{FULFILLMENT_ID_FIELD: str(fulfillment_id), STORE_FIELD: store_filter, "docstatus": ("!=", 2)},
			"name",
			order_by="creation desc",
		)
		if name:
			return name
	return None


def ensure_delivery_notes(cfg: StoreConfig, so, order: dict, run: Run | None = None):
	if not cfg.enabled or not cfg.pull_enabled or not cfg.create_delivery_note or cfg.dry_run:
		return []
	if so.docstatus != 1:
		return []

	created = []
	for fulfillment in order.get("fulfillments") or []:
		if (fulfillment.get("status") or "").lower() == "cancelled":
			continue
		name = ensure_delivery_note(cfg, so, order, fulfillment, run=run)
		if name:
			created.append(name)
	return created


def ensure_delivery_note(cfg: StoreConfig, so, order: dict, fulfillment: dict, run=None):
	fulfillment_id = fulfillment.get("id")
	if not fulfillment_id:
		return None
	if find_delivery_note(cfg, fulfillment_id):
		return None

	from frappe.utils.synchronization import LockTimeoutError, filelock

	try:
		with filelock(f"shopify-dn-{cfg.name}-{fulfillment_id}", timeout=0):
			if find_delivery_note(cfg, fulfillment_id):
				return None
			return _create_delivery_note(cfg, so, order, fulfillment, run=run)
	except LockTimeoutError:
		return None


def _create_delivery_note(cfg: StoreConfig, so, order, fulfillment, run=None):
	from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

	company_cfg = cfg.company_config(so.company)
	location_id = fulfillment.get("location_id")
	warehouse = warehouse_for(cfg, company_cfg, location_id)

	try:
		dn = make_delivery_note(so.name, ignore_permissions=True)
	except Exception as exc:
		# Nothing left to deliver (already covered by another DN) is not an error.
		log_error(
			"Shopify: delivery note could not be created",
			f"{cfg.name} / {so.name} / fulfillment {fulfillment.get('id')}\n{exc}",
			"Sales Order",
			so.name,
		)
		return None

	dn.set(ORDER_ID_FIELD, str(order.get("id")))
	dn.set(ORDER_NUMBER_FIELD, str(order.get("name") or ""))
	dn.set(ORDER_STATUS_FIELD, (order.get("financial_status") or "").strip())
	dn.set(FULFILLMENT_ID_FIELD, str(fulfillment.get("id")))
	dn.set(STORE_FIELD, cfg.name)
	dn.naming_series = naming_series(company_cfg, "Delivery Note")
	dn.set_posting_time = 1
	dn.posting_date = _posting_date(fulfillment)
	dn.flags.ignore_mandatory = True
	dn.flags.ignore_permissions = True
	# This DN came *from* Shopify; never push it back as a fulfillment.
	dn.flags.from_shopify = True

	dn.items = _fulfillment_items(cfg, dn.items, fulfillment.get("line_items") or [], warehouse)

	if not dn.items:
		return None

	try:
		dn.insert(ignore_permissions=True, ignore_mandatory=True)
		dn.submit()
	except Exception:
		frappe.db.rollback()
		raise

	frappe.db.commit()
	if run:
		run.bump("delivery_notes_created")

	if order.get("note"):
		dn.add_comment(text=f"Shopify order note: {order.get('note')}")
	return dn.name


def _posting_date(fulfillment):
	parsed = normalize_datetime(fulfillment.get("created_at"))
	return parsed.date() if parsed else getdate(nowdate())


def _fulfillment_items(cfg: StoreConfig, dn_items, fulfillment_items, warehouse):
	"""Keep only what this fulfillment actually shipped, at its location."""
	if not fulfillment_items:
		for item in dn_items:
			item.warehouse = warehouse
		return dn_items

	remaining = list(fulfillment_items)
	result = []
	for dn_item in dn_items:
		match = None
		for line in remaining:
			if get_item_code(cfg, line) == dn_item.item_code:
				match = line
				break
		if not match:
			continue
		remaining.remove(match)
		dn_item.qty = match.get("quantity") or dn_item.qty
		dn_item.warehouse = warehouse
		if not dn_item.qty:
			continue
		result.append(dn_item)
	return result


# ---------------------------------------------------------------------------
# Push: ERPNext Delivery Note -> Shopify fulfillment (opt-in)
# ---------------------------------------------------------------------------
def push_delivery_note(doc, method=None):
	"""Mark the Shopify order fulfilled when a Delivery Note is submitted.

	Off by default (`Push Fulfillment from Delivery Note`). It is deliberately
	defensive: a Shopify error is logged and never blocks the Delivery Note,
	because the ERPNext stock movement is the source of truth.
	"""
	if getattr(doc.flags, "from_shopify", False) or doc.get(FULFILLMENT_ID_FIELD):
		return
	if doc.docstatus != 1:
		return

	sales_order = _sales_order_of(doc)
	if not sales_order:
		return

	so = frappe.get_doc("Sales Order", sales_order)
	store_name = so.get(STORE_FIELD) or doc.get(STORE_FIELD)
	if not store_name:
		return

	cfg = StoreConfig(store_name)
	if not cfg.enabled or not cfg.push_enabled or not cfg.push_fulfillment or cfg.dry_run:
		return
	if not so.get(ORDER_ID_FIELD):
		return

	from frappe.utils.synchronization import LockTimeoutError, filelock

	try:
		with filelock(f"shopify-push-dn-{doc.name}", timeout=0):
			_push_delivery_note(cfg, so, doc)
	except LockTimeoutError:
		return
	except Exception:
		frappe.db.rollback()
		log_error(
			"Shopify: could not push fulfillment",
			f"{doc.name} / {so.name}\n{frappe.get_traceback()}",
			"Delivery Note",
			doc.name,
		)


def _push_delivery_note(cfg, so, dn):
	client = ShopifyClient(cfg)
	order = client.order(so.get(ORDER_ID_FIELD))
	if not order:
		raise ShopifyConfigError(f"Shopify order {so.get(ORDER_ID_FIELD)} not found.")
	if order.get("cancelled_at"):
		return

	order_lines = order.get("line_items") or []
	line_items = []
	for dn_item in dn.items:
		line_id, so_item = _line_id_for(cfg, so, dn_item, order_lines)
		if not line_id:
			continue
		line_items.append({"id": line_id, "quantity": dn_item.qty})

	if not line_items:
		return

	fulfillment = {"notify_customer": False, "line_items": line_items}
	location_id = _location_for(cfg, so, dn)
	if location_id:
		fulfillment["location_id"] = str(location_id)
	tracking_numbers = [
		value
		for value in (dn.get("tracking_number"), dn.get("tracking_no"), dn.get("lr_no"))
		if value
	]
	if tracking_numbers:
		fulfillment["tracking_numbers"] = [str(value) for value in tracking_numbers]

	response = client.post(f"orders/{so.get(ORDER_ID_FIELD)}/fulfillments.json", {"fulfillment": fulfillment}) or {}
	saved = response.get("fulfillment") or {}
	if not saved.get("id"):
		return

	frappe.db.set_value(
		"Delivery Note",
		dn.name,
		{
			FULFILLMENT_ID_FIELD: str(saved["id"]),
			STORE_FIELD: cfg.name,
			ORDER_ID_FIELD: str(so.get(ORDER_ID_FIELD)),
			ORDER_NUMBER_FIELD: so.get(ORDER_NUMBER_FIELD) or "",
		},
		update_modified=False,
	)
	frappe.db.commit()
	_log_push_run(cfg, dn, saved)


def _sales_order_of(dn):
	for item in dn.items:
		if item.get("against_sales_order"):
			return item.get("against_sales_order")
		if item.get("so_detail"):
			name = frappe.db.get_value("Sales Order Item", item.get("so_detail"), "parent")
			if name:
				return name
	return None


def _line_id_for(cfg, so, dn_item, order_lines):
	so_item = None
	if dn_item.get("so_detail"):
		so_item = frappe.db.get_value(
			"Sales Order Item",
			dn_item.get("so_detail"),
			["name", "item_code", "shopify_line_item_id"],
			as_dict=True,
		)
	if not so_item:
		so_item = next((row for row in so.items if row.item_code == dn_item.item_code), None)

	if so_item:
		line_id = so_item.get("shopify_line_item_id")
		if line_id:
			return str(line_id), so_item

	item_code = dn_item.item_code
	for line in order_lines:
		if line.get("sku") == item_code:
			return str(line.get("id")), so_item
		mapped = get_item_code(cfg, line)
		if mapped == item_code:
			return str(line.get("id")), so_item
	return None, so_item


def _location_for(cfg, so, dn):
	warehouses = {item.warehouse for item in dn.items if item.get("warehouse")}
	for row in cfg.store.warehouse_mapping or []:
		if row.company != so.company:
			continue
		if row.warehouse in warehouses:
			return row.shopify_location_id
	rows = [row for row in cfg.store.warehouse_mapping or [] if row.company == so.company]
	if len(rows) == 1:
		return rows[0].shopify_location_id
	return None


def _log_push_run(cfg, dn, fulfillment):
	try:
		frappe.get_doc(
			{
				"doctype": "Shopify Sync Run",
				"store": cfg.name,
				"mode": "Delivery Notes",
				"trigger": "Hook",
				"status": "Success",
				"started_on": now(),
				"finished_on": now(),
				"note": f"Pushed fulfillment {fulfillment.get('id')} for {dn.name}",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		pass


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
def handle_webhook_fulfilled(payload, store=None, request_id=None):
	cfg = config(store)
	if cfg.is_historical(payload):
		return {"order": payload.get("name"), "result": "before pull cutoff"}
	with Run(cfg.name, mode="Delivery Notes", trigger="Webhook") as run:
		so = find_sales_order(cfg, payload.get("id"))
		if not so:
			from .order import import_order

			so, _result = import_order(cfg, payload, run=run, trigger="Webhook")
		if not so:
			return {"order": payload.get("name"), "result": "sales order not available"}

		so.reload()
		created = ensure_delivery_notes(cfg, so, payload, run=run)
	return {"order": payload.get("name"), "sales_order": so.name, "delivery_notes": created}
