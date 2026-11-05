"""Orders the connector knows about but could not import.

Everything that failed (API error, missing mapping, contention) is recorded in
`Shopify Import Retry` and replayed by a cron job. This report is the worklist:
a `Skipped` row means a product has no Item Map (intended — the merchant
decides what to sell), anything else is an error that should clear itself.
"""

import frappe
from frappe import _


def execute(filters=None):
	filters = filters or {}
	conditions = ["1 = 1"]
	values = {}
	if filters.get("store"):
		conditions.append("store = %(store)s")
		values["store"] = filters["store"]
	if filters.get("status"):
		conditions.append("status = %(status)s")
		values["status"] = filters["status"]
	else:
		conditions.append("status in ('Pending', 'Skipped', 'Failed')")

	columns = [
		{"label": _("Store"), "fieldname": "store", "fieldtype": "Link", "options": "Shopify Store", "width": 140},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 90},
		{"label": _("Shopify Order"), "fieldname": "order_number", "fieldtype": "Data", "width": 140},
		{"label": _("Shopify Order Id"), "fieldname": "shopify_order_id", "fieldtype": "Data", "width": 180},
		{"label": _("Attempts"), "fieldname": "attempts", "fieldtype": "Int", "width": 80},
		{"label": _("Next Retry"), "fieldname": "next_retry_on", "fieldtype": "Datetime", "width": 150},
		{"label": _("Sales Order"), "fieldname": "sales_order", "fieldtype": "Link", "options": "Sales Order", "width": 160},
		{"label": _("Reason"), "fieldname": "reason", "fieldtype": "Small Text", "width": 420},
	]

	rows = frappe.db.sql(
		f"""
		select store, status, order_number, shopify_order_id, attempts,
		       next_retry_on, sales_order, reason
		from `tabShopify Import Retry`
		where {" and ".join(conditions)}
		order by
			case status when 'Failed' then 0 when 'Pending' then 1 else 2 end,
			next_retry_on asc, creation desc
		limit 5000
		""",
		values,
		as_dict=True,
	)
	return columns, rows
