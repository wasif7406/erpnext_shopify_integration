app_name = "shopify_integration"
app_title = "Shopify Integration"
app_publisher = "Aqreno Nova"
app_description = "Multi-company, multi-store Shopify integration for ERPNext"
app_email = "dev@aqrenonova.com"
app_license = "mit"
app_version = "0.1.0"

required_apps = ["erpnext"]

# ---------------------------------------------------------------------------
# Form scripts
# ---------------------------------------------------------------------------
# The catalogue picker is shared by several forms, so it is loaded once on
# every desk page instead of being inlined in each doctype script.
app_include_js = ["public/js/shopify_picker.js"]

doctype_js = {
	"Shopify Store": "public/js/shopify_store.js",
	"Shopify Item Map": "public/js/shopify_item_map.js",
	"Item": "public/js/item.js",
	"Sales Order": "public/js/sales_order.js",
	"Sales Invoice": "public/js/sales_invoice.js",
}

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
before_install = "shopify_integration.install.before_install"
after_install = "shopify_integration.install.after_install"
after_migrate = "shopify_integration.install.after_migrate"

# ---------------------------------------------------------------------------
# Document events
# ---------------------------------------------------------------------------
doc_events = {
	"Item": {
		"after_insert": "shopify_integration.product.upload_item",
		"on_update": "shopify_integration.product.upload_item",
	},
	# Keep the dummy price list empty: ERPNext silently inserts a price when an
	# item has none, and a populated dummy list would re-price future orders.
	"Item Price": {
		"on_change": "shopify_integration.taxes.discard_item_prices",
	},
	# Opt-in reverse direction: mark the Shopify order fulfilled.
	"Delivery Note": {
		"on_submit": "shopify_integration.fulfillment.push_delivery_note",
	},
}

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
# Every job fans out into one short, locked background job per store.
scheduler_events = {
	"cron": {
		"*/15 * * * *": [
			"shopify_integration.tasks.poll_all_stores",
			"shopify_integration.tasks.retry_failed_webhooks",
		],
		# Failed order imports are replayed with backoff; the row itself decides
		# whether it is due, so a 10-minute tick is cheap.
		"*/10 * * * *": [
			"shopify_integration.tasks.retry_failed_imports",
		],
		# Every 5 minutes the task checks each store's own interval (5-60 min).
		"*/5 * * * *": [
			"shopify_integration.tasks.push_inventory_all_stores",
		],
		"*/30 * * * *": [
			"shopify_integration.tasks.old_order_sync",
		],
		"0 7 * * *": [
			"shopify_integration.tasks.webhook_watchdog",
		],
		"30 8 * * *": [
			"shopify_integration.tasks.daily_digest",
		],
	},
	"daily": [
		"shopify_integration.tasks.prune_logs",
	],
}

# No fixtures on purpose: custom fields are created from code (fields.py) so an
# existing site's field positions, labels and list settings are never rewritten.
fixtures = []
