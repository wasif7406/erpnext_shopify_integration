"""Shopify payload helpers: dates, net rates, shipping and HMAC."""

import base64
import datetime
import hashlib
import hmac
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shopify_integration.payloads import (
	clamp_fulfillment_qty,
	invoice_may_be_created,
	line_net_rate,
	normalize_datetime,
	order_items_changed,
	retry_delay_minutes,
	shipping_net_amount,
	shopify_time,
	valid_shopify_signature,
)


def test_shopify_offset_without_colon():
	assert normalize_datetime("2026-09-27T16:29:59+0500") == datetime.datetime(2026, 9, 27, 16, 29, 59)


def test_plain_date_and_empty():
	assert normalize_datetime("2026-06-10") == datetime.datetime(2026, 6, 10, 0, 0, 0)
	assert normalize_datetime(None) is None


def test_utc_timestamp_is_converted_to_site_timezone():
	# 11:29 UTC is 16:29 in Asia/Karachi (UTC+5).
	assert normalize_datetime("2026-09-27T11:29:59Z", tz="Asia/Karachi") == datetime.datetime(
		2026, 9, 27, 16, 29, 59
	)


def test_offset_timestamp_keeps_wall_clock_without_tz():
	assert normalize_datetime("2026-09-27T16:29:59+0500") == datetime.datetime(2026, 9, 27, 16, 29, 59)


def test_shopify_filter_time_converts_site_local_to_utc():
	assert shopify_time("2026-11-04 22:29:00", "Asia/Karachi") == "2026-11-04T17:29:00Z"


def test_line_rate_subtracts_discount_per_unit():
	line = {
		"price": "1000.00",
		"quantity": 2,
		"discount_allocations": [{"amount": "200.00"}],
		"tax_lines": [],
	}
	assert line_net_rate(line) == 900.0


def test_line_rate_removes_included_tax():
	line = {
		"price": "1170.00",
		"quantity": 1,
		"discount_allocations": [],
		"tax_lines": [{"price": "170.00", "rate": 0.17}],
	}
	assert line_net_rate(line, taxes_inclusive=True) == 1000.0


def test_free_line_stays_free():
	line = {"price": "500", "quantity": 1, "discount_allocations": [{"amount": "500"}]}
	assert line_net_rate(line) == 0.0


def test_shipping_discount_and_tax():
	shipping = {
		"price": "300",
		"discount_allocations": [{"amount": "50"}],
		"tax_lines": [{"price": "36"}],
	}
	assert shipping_net_amount(shipping) == 250.0
	assert shipping_net_amount(shipping, taxes_inclusive=True) == 214.0


def test_webhook_signature_roundtrip():
	body = b'{"id": 123, "name": "#1001"}'
	secret = "shpss_test"
	digest = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
	assert valid_shopify_signature(body, digest, secret)
	assert not valid_shopify_signature(body, digest, "wrong")
	assert not valid_shopify_signature(body, "", secret)


# ---------------------------------------------------------------------------
# Sync decisions (the places where a wrong answer costs money or stock)
# ---------------------------------------------------------------------------
def test_fulfillment_quantity_is_clamped_to_what_is_pending():
	# PostEx already delivered 1 of 3; Shopify says it shipped 3.
	assert clamp_fulfillment_qty(2, 3) == 2
	# Normal case: Shopify shipped less than the order.
	assert clamp_fulfillment_qty(3, 1) == 1
	# Nothing left to deliver.
	assert clamp_fulfillment_qty(0, 2) == 0


def test_retry_backoff_is_exponential_and_capped():
	assert retry_delay_minutes(1) == 5
	assert retry_delay_minutes(2) == 10
	assert retry_delay_minutes(3) == 20
	# Cap: one day, never longer.
	assert retry_delay_minutes(20) == 1440


def test_skipped_orders_retry_once_a_day():
	assert retry_delay_minutes(1, skipped=True) == 1440
	assert retry_delay_minutes(5, skipped=True) == 1440


def test_invoice_is_not_created_when_the_order_is_already_billed():
	assert invoice_may_be_created(0, False) is True
	assert invoice_may_be_created(0, True) is False
	assert invoice_may_be_created(100, False) is False
	assert invoice_may_be_created(40, False) is False


def test_order_items_changed_ignores_rounding_noise():
	base = [("ITEM-A", 1, 1999.0), ("ITEM-B", 2, 1799.1)]
	assert not order_items_changed(base, list(base))
	assert order_items_changed(base, [("ITEM-A", 2, 1999.0), ("ITEM-B", 2, 1799.1)])
	assert order_items_changed(base, [("ITEM-A", 1, 1999.0)])
