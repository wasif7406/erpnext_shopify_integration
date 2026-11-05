"""Deduplicate the mapping tables and add the unique indexes.

The maps are resolved with read-then-insert, so two concurrent order imports
could create two rows for the same Shopify product/customer. The code now
handles the constraint violation; this patch makes the database enforce it.

It is idempotent: existing duplicates are merged (the oldest row wins), empty
keys are normalised, and an index that already exists is left alone. Any
failure is logged, never raised, so `bench migrate` is never pinned by it.
"""

import frappe

INDEXES = (
	("Shopify Item Map", "uidx_shopify_item_map", ("store", "product_id", "variant_id")),
	("Shopify Customer Map", "uidx_shopify_customer_map", ("store", "shopify_customer_id")),
	("Shopify Webhook Log", "uidx_shopify_webhook_id", ("shopify_webhook_id",)),
)


def execute():
	_normalise_item_maps()
	_normalise_webhook_ids()
	for doctype, index_name, fields in INDEXES:
		_try_add_unique(doctype, index_name, fields)


def _normalise_item_maps():
	if not frappe.db.exists("DocType", "Shopify Item Map"):
		return
	frappe.db.sql("update `tabShopify Item Map` set variant_id = '' where variant_id is null")
	frappe.db.commit()


def _normalise_webhook_ids():
	if not frappe.db.exists("DocType", "Shopify Webhook Log"):
		return
	# Many legacy rows have ''. A unique index allows many NULLs but only one ''.
	frappe.db.sql(
		"update `tabShopify Webhook Log` set shopify_webhook_id = null "
		"where ifnull(shopify_webhook_id, '') = ''"
	)
	frappe.db.commit()


def _try_add_unique(doctype, index_name, fields):
	if not frappe.db.exists("DocType", doctype):
		return
	try:
		if _index_exists(doctype, index_name):
			return
		_dedupe(doctype, fields)
		frappe.db.add_unique(doctype, list(fields), constraint_name=index_name)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			title=f"Shopify: could not add {index_name}",
			message=frappe.get_traceback(),
		)


def _index_exists(doctype, index_name) -> bool:
	rows = frappe.db.sql(
		"""
		select 1 from information_schema.statistics
		where table_schema = database() and table_name = %s and index_name = %s
		limit 1
		""",
		(f"tab{doctype}", index_name),
	)
	return bool(rows)


def _dedupe(doctype, fields):
	"""Delete every duplicate except the oldest row of each key group."""
	table = f"tab{doctype}"
	key_cols = ", ".join(f"`{field}`" for field in fields)
	group = ", ".join(f"ifnull(`{field}`, '')" for field in fields)
	condition = " and ".join(f"ifnull(`{field}`, '') = %s" for field in fields)

	groups = frappe.db.sql(
		f"select {key_cols}, count(*) as total from `{table}` group by {group} having total > 1",
		as_dict=True,
	)
	for row in groups:
		params = tuple((row.get(field) or "") for field in fields)
		names = frappe.db.sql(
			f"select name from `{table}` where {condition} order by creation asc, name asc",
			params,
			as_dict=True,
		)
		for extra in names[1:]:
			try:
				frappe.delete_doc(doctype, extra.name, force=True, ignore_permissions=True)
			except Exception:
				frappe.log_error(
					title=f"Shopify: duplicate {doctype} not removed",
					message=f"{extra.name}\n{frappe.get_traceback()}",
				)
	frappe.db.commit()
