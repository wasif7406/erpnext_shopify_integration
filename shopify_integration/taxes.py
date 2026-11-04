"""Tax and price-list helpers shared by the order / invoice mappers.

Shopify sends tax **amounts** per line (`price`), not a rate we can recompute.
ERPNext is therefore driven with `Actual` charge rows, and the account is
resolved per store/company from `Shopify Store Tax` (never hard-coded).
"""

import frappe

from .config import tax_account_for, warehouse_for
from .constants import DUMMY_PRICE_LIST, DUMMY_TAX_CATEGORY
from .payloads import money, tax_rate_percent


def discard_item_prices(doc, method=None):
	"""Item Price hook: keep the dummy price list empty (legacy behaviour)."""
	if getattr(doc, "price_list", None) != DUMMY_PRICE_LIST:
		return
	frappe.enqueue(method=_delete_dummy_prices, queue="short", enqueue_after_commit=True)


def _delete_dummy_prices():
	if frappe.db.exists("Price List", DUMMY_PRICE_LIST):
		frappe.db.delete("Item Price", {"price_list": DUMMY_PRICE_LIST, "selling": 1})


def get_dummy_price_list() -> str:
	"""An empty selling price list, so ERPNext never re-prices a Shopify line.

	Reuses the legacy name when the old connector created it.
	"""
	if not frappe.db.exists("Price List", DUMMY_PRICE_LIST):
		pl = frappe.get_doc(
			{"doctype": "Price List", "price_list_name": DUMMY_PRICE_LIST, "selling": 1}
		)
		pl.insert(ignore_permissions=True, ignore_mandatory=True)
		pl.add_comment(text="Used by the Shopify integration; keep this price list empty.")
	return DUMMY_PRICE_LIST


def get_dummy_tax_category() -> str:
	"""A tax category with no item tax templates, so no template is applied."""
	if not frappe.db.exists("Tax Category", DUMMY_TAX_CATEGORY):
		frappe.get_doc({"doctype": "Tax Category", "title": DUMMY_TAX_CATEGORY}).insert(
			ignore_permissions=True, ignore_mandatory=True
		)
	return DUMMY_TAX_CATEGORY


def tax_description(cfg, company_cfg, tax_title):
	title = (tax_title or "").strip().lower()
	for row in cfg.store.taxes or []:
		if row.company == company_cfg.company and (row.shopify_tax or "").strip().lower() == title:
			if row.tax_description:
				return row.tax_description
	return None


def _row(account, description, amount, company_cfg, cost_center=None):
	return {
		"charge_type": "Actual",
		"account_head": account,
		"description": description,
		"tax_amount": money(amount),
		"included_in_print_rate": 0,
		"cost_center": cost_center,
		"dont_recompute_tax": 1,
	}


def get_order_taxes(cfg, company_cfg, order, items, cost_center=None):
	"""Build the ERPNext tax table for a Shopify order.

	Line taxes are added first, then shipping (as a charge or as an item), then
	the optional consolidation that keeps one row per account — the old
	connector's `consolidate_taxes`, default on.
	"""
	rows = []
	taxes_inclusive = bool(order.get("taxes_included"))

	for line_item in order.get("line_items") or []:
		for tax in line_item.get("tax_lines") or []:
			amount = money(tax.get("price"))
			if not amount:
				continue
			title = tax.get("title")
			account = tax_account_for(cfg, company_cfg, title, "sales_tax")
			rows.append(
				_row(
					account,
					tax_description(cfg, company_cfg, title)
					or f"{title} - {tax_rate_percent(tax):.2f}%",
					amount,
					company_cfg,
					cost_center,
				)
			)

	append_shipping_rows(cfg, company_cfg, order, rows, items, taxes_inclusive, cost_center)

	if int(cfg.store.consolidate_taxes or 0):
		rows = consolidate_taxes(rows)

	# Drop empty rows (a fully discounted line can produce 0.00).
	return [row for row in rows if row["tax_amount"]]


def append_shipping_rows(cfg, company_cfg, order, rows, items, taxes_inclusive, cost_center):
	"""Shipping charge (as tax row or as item) plus the tax on that shipping."""
	shipping_as_item = bool(int(cfg.store.add_shipping_as_item or 0)) and cfg.store.shipping_item

	for shipping in order.get("shipping_lines") or []:
		price = money(shipping.get("price"))
		discounts = sum(money(d.get("amount")) for d in (shipping.get("discount_allocations") or []))
		tax_total = sum(money(t.get("price")) for t in (shipping.get("tax_lines") or []))
		amount = price - discounts - (tax_total if taxes_inclusive else 0)
		amount = money(amount)

		if amount and shipping_as_item:
			items.append(
				{
					"item_code": cfg.store.shipping_item,
					"item_name": shipping.get("title") or "Shipping",
					"rate": amount,
					"qty": 1,
					"stock_uom": "Nos",
					"warehouse": warehouse_for(cfg, company_cfg),
					"delivery_date": items[-1].get("delivery_date") if items else None,
					"is_free_item": 0,
				}
			)
		elif amount:
			account = tax_account_for(cfg, company_cfg, shipping.get("title"), "shipping")
			rows.append(
				_row(
					account,
					tax_description(cfg, company_cfg, shipping.get("title"))
					or shipping.get("title")
					or "Shipping",
					amount,
					company_cfg,
					cost_center,
				)
			)

		for tax in shipping.get("tax_lines") or []:
			tax_amount = money(tax.get("price"))
			if not tax_amount:
				continue
			title = tax.get("title")
			account = tax_account_for(cfg, company_cfg, title, "sales_tax")
			rows.append(
				_row(
					account,
					tax_description(cfg, company_cfg, title)
					or f"{title} - {tax_rate_percent(tax):.2f}%",
					tax_amount,
					company_cfg,
					cost_center,
				)
			)


def consolidate_taxes(rows):
	"""One row per account head, summing the amounts (keeps first description)."""
	merged = {}
	order = []
	for row in rows:
		account = row["account_head"]
		if account not in merged:
			merged[account] = dict(row)
			order.append(account)
		else:
			merged[account]["tax_amount"] = money(merged[account]["tax_amount"] + row["tax_amount"])
	return [merged[account] for account in order if merged[account]["tax_amount"]]


