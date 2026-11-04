"""Custom fields the integration needs on ERPNext documents.

They are created from code (not shipped as fixtures) so installing the app on
an existing site never rewrites labels, positions or list settings the merchant
already customised. `ensure_custom_fields()` is idempotent and only creates what
is missing.

The fieldnames are the legacy `ecommerce_integrations` ones on purpose: an
existing site keeps every link, saved filter and report, and `migrate.py` can
attach its old rows to a store without a rename. Only `custom_shopify_store`
and `custom_shopify_synced_on` are new.
"""

import frappe

from .constants import (
	ADDRESS_ID_FIELD,
	CUSTOMER_ID_FIELD,
	FULFILLMENT_ID_FIELD,
	ITEM_DISCOUNT_FIELD,
	ITEM_LINE_ID_FIELD,
	ITEM_SELLING_RATE_FIELD,
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	STORE_FIELD,
)

SALES_ORDER_FIELDS = [
	{
		"fieldname": ORDER_ID_FIELD,
		"label": "Shopify Order Id",
		"fieldtype": "Small Text",
		"insert_after": "title",
		"read_only": 1,
		"no_copy": 1,
		"search_index": 1,
	},
	{
		"fieldname": ORDER_NUMBER_FIELD,
		"label": "Shopify Order Number",
		"fieldtype": "Small Text",
		"insert_after": ORDER_ID_FIELD,
		"read_only": 1,
		"no_copy": 1,
		"in_standard_filter": 1,
	},
	{
		"fieldname": ORDER_STATUS_FIELD,
		"label": "Shopify Order Status",
		"fieldtype": "Small Text",
		"insert_after": ORDER_NUMBER_FIELD,
		"read_only": 1,
		"no_copy": 1,
	},
	{
		"fieldname": STORE_FIELD,
		"label": "Shopify Store",
		"fieldtype": "Link",
		"options": "Shopify Store",
		"insert_after": ORDER_STATUS_FIELD,
		"read_only": 1,
		"no_copy": 1,
		"in_standard_filter": 1,
		"description": "Which Shopify store imported this order. A site can run several stores, and one store can serve several companies.",
	},
	{
		"fieldname": "custom_shopify_synced_on",
		"label": "Shopify Last Synced On",
		"fieldtype": "Datetime",
		"insert_after": STORE_FIELD,
		"read_only": 1,
		"no_copy": 1,
	},
]

SALES_INVOICE_FIELDS = [
	{
		"fieldname": ORDER_ID_FIELD,
		"label": "Shopify Order Id",
		"fieldtype": "Small Text",
		"insert_after": "title",
		"read_only": 1,
		"no_copy": 1,
		"search_index": 1,
	},
	{
		"fieldname": ORDER_NUMBER_FIELD,
		"label": "Shopify Order Number",
		"fieldtype": "Small Text",
		"insert_after": ORDER_ID_FIELD,
		"read_only": 1,
		"no_copy": 1,
	},
	{
		"fieldname": ORDER_STATUS_FIELD,
		"label": "Shopify Order Status",
		"fieldtype": "Small Text",
		"insert_after": ORDER_NUMBER_FIELD,
		"read_only": 1,
		"no_copy": 1,
	},
	{
		"fieldname": STORE_FIELD,
		"label": "Shopify Store",
		"fieldtype": "Link",
		"options": "Shopify Store",
		"insert_after": ORDER_STATUS_FIELD,
		"read_only": 1,
		"no_copy": 1,
		"in_standard_filter": 1,
	},
]

DELIVERY_NOTE_FIELDS = [
	{
		"fieldname": ORDER_ID_FIELD,
		"label": "Shopify Order Id",
		"fieldtype": "Small Text",
		"insert_after": "title",
		"read_only": 1,
		"no_copy": 1,
		"search_index": 1,
	},
	{
		"fieldname": ORDER_NUMBER_FIELD,
		"label": "Shopify Order Number",
		"fieldtype": "Small Text",
		"insert_after": ORDER_ID_FIELD,
		"read_only": 1,
		"no_copy": 1,
	},
	{
		"fieldname": ORDER_STATUS_FIELD,
		"label": "Shopify Order Status",
		"fieldtype": "Small Text",
		"insert_after": ORDER_NUMBER_FIELD,
		"read_only": 1,
		"no_copy": 1,
	},
	{
		"fieldname": FULFILLMENT_ID_FIELD,
		"label": "Shopify Fulfillment Id",
		"fieldtype": "Small Text",
		"insert_after": ORDER_STATUS_FIELD,
		"read_only": 1,
		"no_copy": 1,
		"search_index": 1,
	},
	{
		"fieldname": STORE_FIELD,
		"label": "Shopify Store",
		"fieldtype": "Link",
		"options": "Shopify Store",
		"insert_after": FULFILLMENT_ID_FIELD,
		"read_only": 1,
		"no_copy": 1,
		"in_standard_filter": 1,
	},
]

OTHER_FIELDS = [
	("Customer", CUSTOMER_ID_FIELD, "Shopify Customer Id", "Data", "series"),
	("Address", ADDRESS_ID_FIELD, "Shopify Address Id", "Data", "fax"),
	("Item", ITEM_SELLING_RATE_FIELD, "Shopify Selling Rate", "Currency", "standard_rate"),
	("Supplier", "shopify_supplier_id", "Shopify Supplier Id", "Data", "supplier_name"),
	("Sales Order Item", ITEM_DISCOUNT_FIELD, "Shopify Discount per unit", "Float", "discount_and_margin"),
	("Sales Order Item", ITEM_LINE_ID_FIELD, "Shopify Line Item Id", "Data", "item_code"),
]


def ensure_custom_fields():
	"""Create only the fields that are missing. Never updates existing ones."""
	created = []
	for doctype, specs in (
		("Sales Order", SALES_ORDER_FIELDS),
		("Sales Invoice", SALES_INVOICE_FIELDS),
		("Delivery Note", DELIVERY_NOTE_FIELDS),
	):
		for spec in specs:
			spec = {key: value for key, value in spec.items() if value is not None}
			if _ensure(doctype, spec):
				created.append(f"{doctype}-{spec['fieldname']}")

	for doctype, fieldname, label, fieldtype, insert_after in OTHER_FIELDS:
		spec = {
			"fieldname": fieldname,
			"label": label,
			"fieldtype": fieldtype,
			"insert_after": insert_after,
			"read_only": 1,
			"no_copy": 1,
		}
		if _ensure(doctype, spec):
			created.append(f"{doctype}-{fieldname}")

	frappe.clear_cache()
	return created


def _ensure(doctype: str, spec: dict) -> bool:
	name = f"{doctype}-{spec['fieldname']}"
	if frappe.db.exists("Custom Field", name):
		return False
	try:
		if frappe.get_meta(doctype).has_field(spec["fieldname"]):
			# A future ERPNext version may ship the field natively.
			return False
	except Exception:
		pass

	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	df = dict(spec)
	df["dt"] = doctype
	df["is_system_generated"] = 0
	try:
		create_custom_field(doctype, df, ignore_validate=True)
		return True
	except Exception:
		frappe.log_error(
			title="Shopify: could not create custom field",
			message=f"{name}\n{frappe.get_traceback()}",
		)
		return False
