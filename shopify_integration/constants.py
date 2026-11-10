"""Constants that are genuinely Shopify-wide (not store- or company-specific).

Anything that can differ per shop or per ERPNext company belongs in
`Shopify Store` / `Shopify Store Company`, never here.
"""

# Bumped whenever we need to prove which code a site is actually running; call
# `shopify_integration.api.health` after a deploy and compare.
BUILD = "2026-11-10.2"

APP = "shopify_integration"
MODULE = "Shopify Integration"
STORE_DOCTYPE = "Shopify Store"
SYNC_RUN_DOCTYPE = "Shopify Sync Run"
WEBHOOK_LOG_DOCTYPE = "Shopify Webhook Log"
ITEM_MAP_DOCTYPE = "Shopify Item Map"
CUSTOMER_MAP_DOCTYPE = "Shopify Customer Map"
RETRY_DOCTYPE = "Shopify Import Retry"

# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------
# A failed order import is retried from `Shopify Import Retry` with exponential
# backoff (5, 10, 20 ... minutes, capped at one day). An order skipped because a
# product has no explicit Item Map is retried once a day instead: the merchant
# does not want every Shopify product created, but a map added later should
# self-heal the order.
MAX_IMPORT_ATTEMPTS = 6
RETRY_BASE_MINUTES = 5
RETRY_MAX_MINUTES = 1440
SKIPPED_RETRY_MINUTES = 1440

# Webhook deliveries that failed (or whose worker died) are replayed by the
# sweeper at most this many times.
MAX_WEBHOOK_ATTEMPTS = 5
WEBHOOK_STALE_MINUTES = 10

DEFAULT_API_VERSION = "2025-10"
API_VERSIONS = [
	"2024-10",
	"2025-01",
	"2025-04",
	"2025-07",
	"2025-10",
	"2026-01",
	"2026-04",
	"2026-07",
	"2026-10",
]

AUTH_METHOD_STATIC = "Static Token"
AUTH_METHOD_OAUTH = "OAuth 2.0 Client Credentials"
AUTH_METHODS = [AUTH_METHOD_STATIC, AUTH_METHOD_OAUTH]

CALLBACK_METHOD = "shopify_integration.webhook.receive"

# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------
# `orders/updated` is the safety net: status/payment changes that do not emit
# their own topic (partially paid, refunded, edited line items) only arrive
# here. It deliberately does *not* create documents other than the SO itself.
WEBHOOK_TOPICS = [
	"orders/create",
	"orders/updated",
	"orders/paid",
	"orders/fulfilled",
	"orders/partially_fulfilled",
	"orders/cancelled",
	"products/update",
]

WEBHOOK_HANDLERS = {
	"orders/create": "shopify_integration.order.handle_webhook_order",
	"orders/updated": "shopify_integration.order.handle_webhook_order",
	"orders/paid": "shopify_integration.invoice.handle_webhook_paid",
	"orders/fulfilled": "shopify_integration.fulfillment.handle_webhook_fulfilled",
	"orders/partially_fulfilled": "shopify_integration.fulfillment.handle_webhook_fulfilled",
	"orders/cancelled": "shopify_integration.order.handle_webhook_cancelled",
	"products/update": "shopify_integration.product.handle_webhook_product",
}

# ---------------------------------------------------------------------------
# Custom fields
# ---------------------------------------------------------------------------
# The names are the ones the legacy `ecommerce_integrations` app created, so an
# existing site keeps every link and report. Only `custom_shopify_store` is new.
ORDER_ID_FIELD = "shopify_order_id"
ORDER_NUMBER_FIELD = "shopify_order_number"
ORDER_STATUS_FIELD = "shopify_order_status"
FULFILLMENT_ID_FIELD = "shopify_fulfillment_id"
CUSTOMER_ID_FIELD = "shopify_customer_id"
ADDRESS_ID_FIELD = "shopify_address_id"
ITEM_SELLING_RATE_FIELD = "shopify_selling_rate"
ITEM_DISCOUNT_FIELD = "shopify_item_discount"
ITEM_LINE_ID_FIELD = "shopify_line_item_id"
STORE_FIELD = "custom_shopify_store"
PAYMENT_ENTRY_FIELD = "custom_shopify_payment_entry"

# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
# How an order of a multi-company store is assigned to a company. The value of
# the selected order field is compared case-insensitively with `route_value`.
ROUTE_ANY = "Any"
ROUTE_SOURCE_NAME = "Source Name"
ROUTE_CURRENCY = "Currency"
ROUTE_SHIPPING_COUNTRY = "Shipping Country"
ROUTE_ORDER_TAG = "Order Tag"
ROUTE_APP_ID = "App ID"
ROUTE_FIELDS = [
	ROUTE_ANY,
	ROUTE_SOURCE_NAME,
	ROUTE_CURRENCY,
	ROUTE_SHIPPING_COUNTRY,
	ROUTE_ORDER_TAG,
	ROUTE_APP_ID,
]

# ---------------------------------------------------------------------------
# Posting date basis (invoice mirrors the legacy behaviour by default)
# ---------------------------------------------------------------------------
POSTING_ORDER_DATE = "Order Date"
POSTING_PAID_DATE = "Paid Date"
POSTING_TODAY = "Today"
POSTING_DATE_BASES = [POSTING_ORDER_DATE, POSTING_PAID_DATE, POSTING_TODAY]

# ---------------------------------------------------------------------------
# Legacy doctypes / shared helper records
# ---------------------------------------------------------------------------
# Reusing the legacy names means a site that already ran the old connector does
# not get a second empty price list / tax category.
DUMMY_PRICE_LIST = "Ecommerce Integrations - Ignore"
DUMMY_TAX_CATEGORY = "Ecommerce Integrations - Ignore"
LEGACY_SETTING_DOCTYPE = "Shopify Setting"
LEGACY_OLD_SETTING_DOCTYPE = "Shopify Settings"
LEGACY_ITEM_DOCTYPE = "Ecommerce Item"
LEGACY_LOG_DOCTYPE = "Ecommerce Integration Log"

# Re-read this much of the recent window on every poll, so an order whose
# import failed transiently is retried even if it is not updated again.
POLL_OVERLAP_MINUTES = 60

# Safety ceiling, not a policy: `sync_page_size` / `run_request_budget` are the
# knobs. The ceiling only prevents a mis-typed budget from running for hours.
MAX_REQUESTS_HARD_CEILING = 5000

# Fulfillment statuses.
FULFILLMENT_STATUSES = ["", "partial", "fulfilled", "restocked"]

# Default log retention.
LOG_RETENTION_DAYS = 90
