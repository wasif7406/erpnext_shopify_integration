"""Sales Orders imported by a Shopify connector but not owned by a store.

Before this app, the legacy connector wrote `shopify_order_id` and nothing else.
After migration those rows are adopted automatically the first time a store
sees the order; this report is the read-only worklist for the rest (for example
an order whose store was never migrated).
"""

import frappe
from frappe import _


def execute(filters=None):
	filters = filters or {}
	conditions = ["ifnull(so.shopify_order_id, '') != ''", "ifnull(so.custom_shopify_store, '') = ''"]
	values = {}
	if filters.get("company"):
		conditions.append("so.company = %(company)s")
		values["company"] = filters["company"]
	if filters.get("include_cancelled") is None or not filters.get("include_cancelled"):
		conditions.append("so.docstatus != 2")

	columns = [
		{"label": _("Sales Order"), "fieldname": "sales_order", "fieldtype": "Link", "options": "Sales Order", "width": 170},
		{"label": _("Company"), "fieldname": "company", "fieldtype": "Link", "options": "Company", "width": 140},
		{"label": _("Customer"), "fieldname": "customer", "fieldtype": "Link", "options": "Customer", "width": 180},
		{"label": _("Shopify Order ID"), "fieldname": "shopify_order_id", "fieldtype": "Data", "width": 160},
		{"label": _("Shopify Number"), "fieldname": "shopify_order_number", "fieldtype": "Data", "width": 120},
		{"label": _("Date"), "fieldname": "transaction_date", "fieldtype": "Date", "width": 100},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 140},
		{"label": _("Grand Total"), "fieldname": "grand_total", "fieldtype": "Currency", "width": 120},
	]

	rows = frappe.db.sql(
		f"""
		select so.name as sales_order, so.company, so.customer, so.shopify_order_id,
		       so.shopify_order_number, so.transaction_date, so.status, so.grand_total
		from `tabSales Order` so
		where {" and ".join(conditions)}
		order by so.transaction_date desc, so.name desc
		limit 5000
		""",
		values,
		as_dict=True,
	)
	return columns, rows
