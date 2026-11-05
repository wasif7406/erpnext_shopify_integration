import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime


def execute(filters=None):
	filters = filters or {}
	days = cint(filters.get("days")) or 7
	since = add_to_date(now_datetime(), days=-days)

	columns = [
		{"label": _("Store"), "fieldname": "store", "fieldtype": "Link", "options": "Shopify Store", "width": 160},
		{"label": _("Shop Domain"), "fieldname": "shop_domain", "fieldtype": "Data", "width": 220},
		{"label": _("Enabled"), "fieldname": "enabled", "fieldtype": "Check", "width": 70},
		{"label": _("Shopify → ERPNext"), "fieldname": "pull", "fieldtype": "Check", "width": 120},
		{"label": _("ERPNext → Shopify"), "fieldname": "push", "fieldtype": "Check", "width": 120},
		{"label": _("Last Order Poll"), "fieldname": "last_order_sync_on", "fieldtype": "Datetime", "width": 150},
		{"label": _("Last Webhook"), "fieldname": "last_webhook_on", "fieldtype": "Datetime", "width": 150},
		{"label": _("Orders"), "fieldname": "orders", "fieldtype": "Int", "width": 80},
		{"label": _("Invoices"), "fieldname": "invoices", "fieldtype": "Int", "width": 80},
		{"label": _("Delivery Notes"), "fieldname": "delivery_notes", "fieldtype": "Int", "width": 110},
		{"label": _("Poll Lag (min)"), "fieldname": "poll_lag", "fieldtype": "Int", "width": 110},
		{"label": _("Open Imports"), "fieldname": "open_imports", "fieldtype": "Int", "width": 110},
		{"label": _("Failures"), "fieldname": "failures", "fieldtype": "Int", "width": 80},
		{"label": _("Last Run"), "fieldname": "last_run_result", "fieldtype": "Small Text", "width": 380},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 120},
	]

	rows = []
	for store in frappe.get_all(
		"Shopify Store",
		fields=[
			"name",
			"store_name",
			"shop_domain",
			"enabled",
			"enable_shopify_to_erpnext",
			"enable_erpnext_to_shopify",
			"last_order_sync_on",
			"last_webhook_on",
			"last_run_result",
		],
		order_by="creation asc",
	):
		orders = frappe.db.count(
			"Sales Order", {"custom_shopify_store": store.name, "creation": (">=", since)}
		)
		invoices = frappe.db.count(
			"Sales Invoice",
			{"custom_shopify_store": store.name, "creation": (">=", since), "docstatus": 1},
		)
		delivery_notes = frappe.db.count(
			"Delivery Note",
			{"custom_shopify_store": store.name, "creation": (">=", since), "docstatus": 1},
		)
		failures = frappe.db.count(
			"Shopify Sync Run",
			{"store": store.name, "creation": (">=", since), "status": ("in", ["Failed", "Partial"])},
		)
		open_imports = frappe.db.count(
			"Shopify Import Retry",
			{"store": store.name, "status": ("in", ["Pending", "Skipped", "Failed"])},
		)
		poll_lag = 0
		if store.last_order_sync_on:
			poll_lag = max(
				0, int((now_datetime() - get_datetime(store.last_order_sync_on)).total_seconds() // 60)
			)

		status = "OK"
		if not cint(store.enabled):
			status = "Disabled"
		elif not (store.enable_shopify_to_erpnext or store.enable_erpnext_to_shopify):
			status = "Not syncing"
		elif failures:
			status = "Errors"
		elif open_imports and poll_lag > 30:
			status = "Retrying"
		elif (store.enable_shopify_to_erpnext and not store.last_order_sync_on) or (
			store.enable_shopify_to_erpnext
			and store.last_webhook_on
			and store.last_webhook_on < add_to_date(now_datetime(), hours=-48)
		):
			status = "Stale"

		rows.append(
			{
				"store": store.name,
				"shop_domain": store.shop_domain,
				"enabled": cint(store.enabled),
				"pull": cint(store.enable_shopify_to_erpnext),
				"push": cint(store.enable_erpnext_to_shopify),
				"last_order_sync_on": store.last_order_sync_on,
				"last_webhook_on": store.last_webhook_on,
				"orders": orders,
				"invoices": invoices,
				"delivery_notes": delivery_notes,
				"poll_lag": poll_lag,
				"open_imports": open_imports,
				"failures": failures,
				"last_run_result": store.last_run_result,
				"status": status,
			}
		)

	return columns, rows
