# Shopify Integration — Multi-Company / Multi-Store Architecture

Status: v0.1.0 implementation. The app lives in `shopify_integration/`; see
`README.md` for install, configuration, operation and migration.

---

## 1. Purpose

Run one ERPNext site with several Shopify shops and several companies, in every
legal configuration:

```
1 store  -> 1 company        (the normal setup)
N stores -> 1 company        (several shopfronts, one ledger)
1 store  -> N companies      (one shop routed to different legal entities)
N stores -> N companies      (arbitrary)
```

A store never posts into another store's documents, and a company never settles
another company's revenue: the store and the company are resolved per order and
recorded on every document created.

The app replaces the Shopify half of `ecommerce_integrations` feature-for-
feature. Both sync directions are opt-in and default **off**.

---

## 2. Non-goals

* Creating Shopify orders from ERPNext (order push).
* Refund / credit-note automation from Shopify refunds.
* Multi-currency settlement beyond the document exchange rate.
* Replacing `ecommerce_integrations` for Amazon / Unicommerce / Zenoti.

---

## 3. Baseline (what the old connector does)

`ecommerce_integrations.shopify`:

| Area | Behaviour |
|---|---|
| Settings | Single `Shopify Setting`: one shop, one company, one warehouse |
| Auth | static token or OAuth client credentials |
| Webhooks | 5 topics, HMAC, one site URL, documents created asynchronously |
| Pull | customers + addresses + contacts, items from products, Sales Orders, invoice on paid, DN on fulfilled, cancel |
| Push | new items, item updates, variants, inventory levels |
| Logs | `Ecommerce Integration Log` |
| Backfill | `sync_old_orders` by created_at range |
| Taxes | `Shopify Tax Account` child → account, Actual rows |

Everything above exists here. The only new hard requirement is company routing.

---

## 4. Target model

```
Shopify Store S1 ──┬── Shopify Store Company (company=A, default)     ── token S1, webhooks S1
                   └── Shopify Store Company (company=B, route=pos)   ── same token, same catalogue

Shopify Store S2 ──── Shopify Store Company (company=A, default)       ── token S2, webhooks S2

Company A  <- orders from S1 (web), S2 (all)
Company B  <- orders from S1 (source_name=pos)
```

Rules:

1. A store has one `shop_domain`, one token and one webhook URL.
2. A store has one or more company rows. With several, exactly one is the
   default and the others must have a routing rule.
3. An order is routed to exactly one company (`routing.resolve_company`). Two
   matches are an error.
4. A document created by the app belongs to the company of its store row, never
   to a hard-coded company.
5. An item or customer master is global; the store-specific link is a mapping
   row (`Shopify Item Map`, `Shopify Customer Map`).
6. Disabling a store row or a company row stops that flow without touching the
   other store/company.

---

## 5. DocTypes

### 5.1 `Shopify Store` (regular, not Single)

Identity and auth:

| Field | Notes |
|---|---|
| `store_name`, `shop_domain` | `shop_domain` unique |
| `authentication_method` | Static Token / OAuth 2.0 Client Credentials |
| `access_token`, `shared_secret` | Password fields |
| `client_id`, `client_secret`, `oauth_access_token`, `token_expires_at` | OAuth |
| `api_version` | per-store Admin API version |
| `webhook_key`, `webhook_url`, `webhook_secret` | per-store callback identity |

Direction and features:

| Field | Default |
|---|---|
| `enabled` | on |
| `enable_shopify_to_erpnext` | **off** |
| `enable_erpnext_to_shopify` | **off** |
| `dry_run` | off |
| `pull_orders` / `pull_customers` / `pull_products` | on (only relevant when the pull master is on) |
| `create_sales_invoice` / `create_delivery_note` / `cancel_sales_order` | **off** |
| `push_items` / `push_item_updates` / `push_variants` / `push_inventory` | **off** |
| `order_sync_lookback_days` | 3 |
| `pull_order_from` | empty |
| `inventory_sync_frequency` | 60 min |
| `request_delay_seconds` / `run_request_budget` | 0.5 s / 800 |
| `sync_old_orders`, `old_orders_from/to` | one-shot backfill |

Child tables:

* `companies` → `Shopify Store Company`
* `warehouse_mapping` → `Shopify Store Warehouse`
* `taxes` → `Shopify Store Tax`
* `webhooks` → `Shopify Store Webhook`
* result fields: `last_order_sync_on`, `last_inventory_push_on`,
  `last_webhook_on`, `last_run_on`, `last_run_result`, `last_connection_status`.

### 5.2 `Shopify Store Company` (child)

`company`, `is_default`, `enabled`, `route_field`, `route_value`,
`customer_group`, `default_customer`, `default_warehouse`, `cost_center`,
`cash_bank_account`, three naming series, default sales-tax / shipping
accounts, `create_payment_entry`, `invoice_posting_date_basis`.

A company row is the *whole* company-specific configuration; nothing about a
company is hard-coded in the modules.

### 5.3 `Shopify Store Warehouse` / `Shopify Store Tax` (children)

* location id → warehouse, per company.
* Shopify tax title → ERPNext account + optional description, per company.

Both are separate child tables because Frappe child tables cannot nest.

### 5.4 `Shopify Item Map` / `Shopify Customer Map` (regular)

One row per (store, Shopify product/variant) → ERPNext Item and per (store,
Shopify customer id) → ERPNext Customer. The legacy `Ecommerce Item` table and
`Customer.shopify_customer_id` field are **read once** by the migration; the app
itself never depends on them.

### 5.5 Observability

* `Shopify Sync Run` — one row per run: store, mode, trigger, status, counters,
  request count/budget flag, note and a `failures` table
  (`Shopify Sync Failure`).
* `Shopify Webhook Log` — store, topic, `X-Shopify-Webhook-Id`, order id,
  status, result, payload, traceback. Failed rows can be replayed.

---

## 6. Resolution rules

```
store    = Shopify Store (explicit for every job)
company  = routing.resolve_company(store.companies, order)
           - one enabled row -> it
           - matching route rows -> exactly one
           - is_default row
           - error (never guess)

warehouse = mapping[company][shopify_location_id]
            or company.default_warehouse
            or Stock Settings.default_warehouse (if same company)
            or error

tax account = mapping[company][tax title]
              or company.default_sales_tax_account / default_shipping_charges_account
              or error

series / cost center / cash account = company row, then company defaults
```

Fallbacks never cross a company boundary.

---

## 7. Flows

### 7.1 Pull: order

`webhook orders/create|updated` or the 15-minute poll → `order.import_order`:

1. dedupe by `(store, shopify_order_id)`; adopt a legacy row;
2. resolve company;
3. ensure customer (create/update + addresses + contact) if `pull_customers`,
   else use the mapping / default customer;
4. ensure items (product fetch once per product id) if `pull_products`;
5. build the Sales Order: net rates (discounts and included tax removed),
   `Actual` tax rows from the store/company mapping, dummy price list and tax
   category, currency conversion when needed;
6. submit and commit;
7. run opt-in downstream steps: invoice when paid and enabled; delivery notes
   for fulfillments when enabled; cancellation when cancelled and enabled.

A line without an item and `pull_products` off fails the whole order with the
missing SKU: a partial order would silently change the customer's invoice.

### 7.2 Pull: invoice

`orders/paid` (or an import of a paid order) → `invoice.ensure_invoice`:

* skip unless `create_sales_invoice` and the SO is submitted;
* dedupe `(store, shopify_order_id)`, lock per order;
* `make_sales_invoice`, posting date per company basis, series, cost center;
* submit, then optionally create a Payment Entry into the company's cash/bank
  account (`get_payment_entry`, allocated to the invoice).

The invoice does **not** move stock; Delivery Notes do.

### 7.3 Pull: delivery note

`orders/fulfilled|partially_fulfilled` → `fulfillment.ensure_delivery_notes`:

* one DN per fulfillment, dedupe `(store, shopify_fulfillment_id)`;
* posting date = fulfillment `created_at`;
* items = only the fulfilled lines, warehouse from the location mapping;
* submit, commit.

### 7.4 Pull: cancellation

`orders/cancelled` → cancel the SO only when no invoice and no DN exist. A
documented order keeps its sale and only gets `shopify_order_status =
cancelled`. A cancel blocked by ERPNext (e.g. Closed status) is logged and
leaves the status stamp.

### 7.5 Push: items

`Item.after_insert` / `on_update` → `product.upload_item`:

* runs only if at least one store has push enabled + `push_items`;
* skips items created from Shopify (`flags.from_shopify`);
* simple items → one product; variant items → template product with options and
  all variants (`push_variants`);
* mapping rows store product/variant/inventory item ids;
* `push_item_updates` controls updates.

### 7.5.1 Push: fulfillment

`Delivery Note.on_submit` → `fulfillment.push_delivery_note` when
`Push Fulfillment from Delivery Note` is on:

* skipped for a DN that came *from* Shopify (`flags.from_shopify`) or already
  carries `shopify_fulfillment_id`;
* the line ids come from `Sales Order Item.shopify_line_item_id`, else are
  matched by SKU against the live Shopify order;
* one `orders/{id}/fulfillments.json` POST, location from the warehouse map;
* the returned fulfillment id is stamped on the DN.

A Shopify error is logged and never blocks the Delivery Note: the ERPNext stock
movement is the source of truth.

### 7.6 Push: inventory

Every 5 minutes, each store that is due (`inventory_sync_frequency`) pushes
changed `(warehouse, item)` pairs:

* joins `Shopify Item Map` with `Bin`, only `Bin.modified > inventory_synced_on`;
* `available = actual_qty − reserved_qty`;
* `POST inventory_levels/set.json` per location mapping;
* `inventory_synced_on` stamped on success, so the next run is small;
* `inventory_item_id` is fetched once per variant and cached on the map.

### 7.7 Backfill

`sync_old_orders` flag or `api.sync_old_orders(store, from, to)` paginates
`orders.json?created_at_min/max` and imports each order. The flag is cleared
after the run so the scheduler does not repeat it.

---

## 8. Idempotency and concurrency

| Risk | Mitigation |
|---|---|
| Webhook + poll import the same order | `(store, order_id)` lookup inside a per-order `filelock`, re-checked after acquiring the lock |
| Two DNs / invoices for one fulfillment | per-fulfillment `filelock` + `(store, shipment id)` lookup |
| Two runs create the same link | `utils.claim_field` conditional UPDATE before keeping a created document |
| Duplicate webhook delivery | `X-Shopify-Webhook-Id` existing log is acknowledged and ignored |
| Two cron ticks for one store | `filelock("shopify-<kind>-<store>")`, timeout 0 |
| A broken order blocks the queue | errors are recorded per run; the poll continues; a permanently broken order is reported in `Shopify Sync Run` |
| API budget blowout | one `RequestBudget` per run, shared by every client call; the run stops cleanly and reports `budget exhausted` |
| Rate limits | 0.5 s between calls, `Retry-After` honoured on 429, bounded 5xx retries |

---

## 9. Scheduler (`hooks.py`)

| Job | Cron | Notes |
|---|---|---|
| `poll_all_stores` | `*/15 * * * *` | one locked job per pulling store |
| `push_inventory_all_stores` | `*/5 * * * *` | per-store frequency gate |
| `old_order_sync` | `*/30 * * * *` | only for stores with the flag set |
| `webhook_watchdog` | `0 7 * * *` | 24 h silence alert |
| `daily_digest` | `30 8 * * *` | counts, last run, webhook age |
| `prune_logs` | daily | 90 days |

No `Scheduled Job Type` rows are created by the app; removing/renaming the app
removes the schedule with it (the failure mode that left an orphan PostEx
scheduler does not exist here).

---

## 10. API and security

* Every write endpoint checks System Manager + document permission.
* Tokens and secrets are `Password` fields (encrypted at rest).
* Webhook: guest-callable, HMAC-SHA256 over the raw body, constant-time compare,
  per-store secret and unguessable key. It creates no documents.
* The app never logs secrets; error messages contain status/response text only.

---

## 11. Migration

`migrate.import_legacy_settings`:

1. read `Shopify Setting` (Single) — never modify it;
2. create/reuse one `Shopify Store` for its domain with token, company row,
   tax rows and warehouse rows;
3. both direction masters stay **off**;
4. backfill `custom_shopify_store` on SO/SI/DN by `shopify_order_id`;
5. copy `Customer.shopify_customer_id` and `Ecommerce Item` into the new maps.

`migrate.status()` reports what is missing. Disabling the old connector is a
manual step because both enabled at once would import every order twice.

---

## 12. Implementation map

| Concern | Module |
|---|---|
| DocTypes | `doctype/shopify_store*`, `shopify_sync_run`, `shopify_sync_failure`, `shopify_webhook_log`, `shopify_item_map`, `shopify_customer_map` |
| Constants / topics / fieldnames | `constants.py` |
| Store + company resolution, validation | `config.py` |
| Pure company routing | `routing.py` |
| Pure payload maths (dates, rates, shipping, HMAC) | `payloads.py` |
| REST client, pagination, budget, retries | `client.py` |
| Static token + OAuth client credentials | `oauth.py` |
| Customers / addresses / contacts | `customer.py` |
| Products, item pull and push | `product.py` |
| Orders, poll, backfill, cancel | `order.py` |
| Invoice + Payment Entry | `invoice.py` |
| Delivery notes | `fulfillment.py` |
| Inventory push | `inventory.py` |
| Tax/price-list helpers | `taxes.py` |
| Guest webhook + signature + replay | `webhook.py` |
| Cron entry points and locks | `tasks.py` |
| Whitelisted endpoints | `api.py` |
| Custom fields | `fields.py` |
| Install / patches | `install.py`, `patches/` |
| Legacy migration | `migrate.py` |
| Desk | `public/js/*.js` |
| Reports | `report/shopify_store_health`, `report/shopify_unmapped_orders` |
| Tests | `tests/test_routing.py`, `tests/test_payloads.py` |

---

## 13. Decisions recorded

1. **Both directions default off**, including sub-features; installation is
   side-effect free.
2. **N:M store/company via a child table with routing rules**, not one company
   per store and not a hard-coded site company.
3. **One token per store**; several companies share it (Shopify cannot issue
   company-scoped tokens).
4. **Item/customer maps are per store**, because Shopify ids are store-scoped
   and two stores may collide.
5. **No stock movement on invoice** (legacy behaviour); Delivery Notes move
   stock.
6. **Tax amounts are preserved exactly** (`Actual` rows), because Shopify sends
   money, not a rate to recompute.
7. **Raw `requests` client** instead of the `shopify` python package: fewer
   pinned dependencies, explicit retries/pagination/budget.
8. **Webhook creates no documents**; one writer per document type.
9. **Company routing errors are fatal**, never guessed.

### Deferred

* Order/fulfilment push (ERPNext → Shopify).
* Refund/credit-note automation.
* Per-store ROAS/analytics dashboard (the data is present in the maps/logs).
* A UI for “sync this item now” on the Item form (API exists:
  `api.push_item`).
