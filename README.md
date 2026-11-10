# Shopify Integration (Frappe app)

Multi-company, multi-store Shopify connector for ERPNext.

It is a drop-in replacement for the Shopify half of
[`ecommerce_integrations`](https://github.com/frappe/ecommerce_integrations):
same fieldnames on Sales Order / Sales Invoice / Delivery Note / Customer /
Item, same order → Sales Order → invoice / delivery note flow, same item and
inventory upload — but **store-aware**, **company-aware**, and with **both sync
directions off by default**.

| | `ecommerce_integrations` | This app |
|---|---|---|
| Stores | one `Shopify Setting` (Single) | many `Shopify Store` records |
| Company | one company per site | one store can serve several companies (N:M) |
| Direction | always on when enabled | two master switches, **both default OFF** |
| Order owner | no field | `Sales Order.custom_shopify_store` |
| Duplicate protection | per-order id check | per-order + per-customer lock, conditional claim on link fields, unique map indexes, persistent retry queue |
| History | `Ecommerce Integration Log` | `Shopify Sync Run`, `Shopify Webhook Log`, `Shopify Item/Customer Map` |
| Runtime | `shopify` + `pyactiveresource` | plain `requests` REST client |
| Webhooks | one URL for the site | one URL + key per store, HMAC per store |

`architecture.md` documents the design decisions; this README documents install,
configuration, operation and migration.

---

## 1. Install

```bash
cd /path/to/frappe-bench
bench get-app shopify_integration /path/to/shopify_integration
bench --site <site> install-app shopify_integration
```

`after_install` creates the custom fields (only the missing ones). No fixtures
are shipped, so `bench migrate` never rewrites desk layout or list settings.

**Nothing is enabled after install.** Both sync directions default to off, so
installing the app cannot create or change an order until you say so.

---

## 2. Parity: everything the old connector did

| Old connector feature | This app | Toggle |
|---|---|---|
| Pull orders (`orders/create`) | `order.import_order` | `Enable Shopify → ERPNext` + `Pull Orders` |
| Pull old orders by date range | `sync_old_orders` (flag or API) | `Sync Old Orders` |
| Customers + addresses + contact | `customer.ensure_customer` | `Pull Customers` |
| Items from products/order lines | `product.ensure_items` / `sync_product` | `Pull Products` (off by default — explicit `Shopify Item Map` only) |
| Tax title → account mapping | `Shopify Store Tax` rows + defaults | — |
| Shipping as tax row or item | `add_shipping_as_item` + `shipping_item` | — |
| Consolidate tax rows | `consolidate_taxes` | — |
| Invoice on `orders/paid` | `invoice.ensure_invoice` | `Create Sales Invoice when Paid` |
| Payment Entry for paid order | `invoice._make_payment_entry` | company row `Create Payment Entry` |
| Delivery Note on `orders/fulfilled` | `fulfillment.ensure_delivery_notes` | `Create Delivery Note on Fulfillment` |
| Cancel order | `order.cancel_sales_order` | `Cancel Sales Order from Shopify` |
| Webhooks + HMAC validation | `webhook.receive` | per-store key |
| Upload new items | `product.upload_item` | `Push New Items` |
| Update item on change | same hook | `Push Item Updates` |
| Push variants | `product._push_template` | `Push Variants` |
| Push stock levels | `inventory.push_inventory` | `Push Inventory` |
| Push a fulfillment from a Delivery Note (new) | `fulfillment.push_delivery_note` | `Push Fulfillment from Delivery Note` (off) |
| Warehouse / location mapping | `Shopify Store Warehouse` | — |
| Sync log | `Shopify Sync Run` + Webhook Log | — |
| Retry a failed job | `Shopify Webhook Log` → Replay | — |

New, because the old connector could not express them:

* N stores → 1 company.
* 1 store → N companies, routed per order.
* Per-store accounts, warehouses, series and cost centers.
* Dry run (shadow) mode.
* Per-run request budget and 429/5xx handling.
* Store health and unmapped-order reports.

---

## 3. Configure

### 3.1 Create a store

`Shopify Integration > Shopify Store > New`

| Field | Meaning |
|---|---|
| `store_name` | Human label |
| `shop_domain` | `my-store.myshopify.com` (unique) |
| `authentication_method` | `Static Token` (custom app) or `OAuth 2.0 Client Credentials` |
| `access_token` / `shared_secret` | Static-token app credentials (secret verifies webhooks) |
| `client_id` / `client_secret` | OAuth app credentials (client credentials grant) |
| `api_version` | Admin API version the shop accepts |
| `enable_shopify_to_erpnext` | **Master switch, default off** |
| `enable_erpnext_to_shopify` | **Master switch, default off** |
| `dry_run` | Log only, create nothing |
| `enabled` | Stops every flow for this store |

Press **Test Connection**, then **Register Webhooks**, then **Fetch Shopify
Locations**.

### 3.2 Companies (all supported shapes)

The `Companies` table is the junction between a store and ERPNext companies.

**1 store → 1 company** (the normal case):

| Company | Default | Route Field | Route Value |
|---|---|---|---|
| Aqreno Nova | ✔ | Any | |

**N stores → 1 company**: create two `Shopify Store` documents, each with a
company row pointing at the same company. Each store has its own token,
webhooks and mapping.

**1 store → N companies**: add one row per company, mark exactly one as
`Default`, and give the others a routing rule:

| Company | Default | Route Field | Route Value |
|---|---|---|---|
| Company A | ✔ | Any | |
| Company B | | Currency | USD |
| Company C | | Source Name | pos |

Routing rules (`routing.resolve_company`):

1. one enabled company row → that company;
2. otherwise the rows whose rule matches the order — exactly one must match;
3. otherwise the `Default` row;
4. otherwise the run fails with a clear message (never guessed).

`Route Field` options: `Any`, `Source Name` (`order.source_name`), `Currency`,
`Shipping Country` (name or ISO code), `Order Tag`, `App ID`.

Two companies matching the same order is a hard error: revenue must never land
in the wrong ledger. The same store can therefore be linked to different
companies, but each order has exactly one owner.

Each company row also carries its own:

* customer group / default customer;
* default warehouse, cost center, cash/bank account;
* Sales Order / Delivery Note / Sales Invoice naming series;
* default sales-tax and shipping-charge accounts;
* `Create Payment Entry when Paid`;
* invoice posting date basis (`Order Date`, `Paid Date`, `Today`).

### 3.2.1 Item mapping — only the products you list

`Pull Products / Auto-create Items` is **off by default**. In that mode the app
never invents an ERPNext Item: every Shopify product you want to sell must be
listed once in **Shopify Item Map** (`Shopify Store → Add Item Mapping`).

A mapping row needs the ERPNext Item plus one of:

- **Shopify SKU** (easiest), or
- **Shopify Product ID + Variant ID** (needed when the SKU is blank).

#### Picking the product instead of typing it

The ids are 13–14 digit numbers, so the desk offers a catalogue browser:

- **`Shopify Item Map` → *Browse Shopify Products*** (a button inside the form
  next to the ids, plus the same action in the form header) — one dialog that
  lists the store's live catalogue, searchable by title, handle, SKU, barcode,
  product id or variant id. Its *Show* selector switches the same dialog between
  products and the flattened variant list. Choosing a product with a single
  variant fills *Product ID, Variant ID, SKU, Inventory Item ID* in one go; a
  product with several variants expands so you pick the exact variant.
  *Product only* fills just the product id (for a template item). The button
  works on a brand-new mapping too: if the Store field is empty the dialog asks
  for it.
- **`Item` → Shopify → Link Shopify Product** — same dialog, and it writes the
  `Shopify Item Map` row for that item (`Shopify Mappings` shows/links them).
  The map row stays the single source of truth; no id is copied onto the item.
- **`Shopify Store` → Add Item Mapping** — asks for the ERPNext Item, then the
  same picker; *Enter IDs manually* is still there for edge cases.

The catalogue is read from the Shopify Admin API once per store and cached for
10 minutes (`push_inventory`/order sync are unaffected). A mapping cannot be
silently stolen: if the product/variant already belongs to another item the
call fails with that item's name, and the Item form offers to move it.

If an order contains **any** product without a mapping, the whole order is
**skipped** (nothing is created) and the reason is written to the Sync Run /
Webhook Log. This is what makes it safe to run test products in Shopify
without polluting the ERPNext catalogue.

Turning `Pull Products` on restores the old behaviour: missing products are
fetched from Shopify and Items are created automatically.

### 3.3 Warehouses and taxes

* **Warehouse mapping**: `Shopify Location ID → Warehouse`, per company.
  `Fetch Shopify Locations` shows the ids; its confirmation adds one row per
  company/location using that company's default warehouse.
* **Tax mapping**: `Shopify Tax` title → ERPNext account, per company. The
  title must match what Shopify sends (`IGST`, `SGST`, `CGST`, `Standard`, …).
  Unmapped titles fall back to the company's default sales-tax / shipping
  account; if that is empty the order fails with the exact missing title.

### 3.4 Direction toggles (both default OFF)

```
Enable Shopify → ERPNext          [ ]   # pull master
  Pull Orders                     [x]
  Pull Customers                  [x]
  Pull Products                   [ ]   # explicit item map only
  Create Sales Invoice when Paid  [ ]
  Create Delivery Note            [ ]
  Cancel Sales Order              [ ]

Enable ERPNext → Shopify          [ ]   # push master
  Push New Items                  [ ]
  Push Item Updates               [ ]
  Push Variants                   [ ]
  Push Fulfillment from DN        [ ]
  Push Inventory                  [ ]
```

Turning a master switch on does **not** automatically turn sub-features on
except the pull defaults above; invoice / delivery-note / cancellation are
always opt-in.

---

## 4. What runs, and when

| Job | Frequency | Function |
|---|---|---|
| Order poll | `*/15 * * * *` | `tasks.poll_all_stores` → locked per store |
| Failed invoice/DN webhook replay | `*/15 * * * *` | `tasks.retry_failed_webhooks` |
| Failed order import replay | `*/10 * * * *` | `tasks.retry_failed_imports` (row-driven backoff) |
| Inventory push | `*/5 * * * *` | `tasks.push_inventory_all_stores` (each store obeys its own interval) |
| Old-order backfill | `*/30 * * * *` | `tasks.old_order_sync` (only when `Sync Old Orders` is ticked) |
| Webhook watchdog | daily 07:00 | alerts if a pulling store received no webhook for 24 h |
| Daily digest | daily 08:30 | per-store counts, last runs, webhook age |
| Log pruning | daily | 90 days of runs/webhook logs |

Webhooks are the primary path; the 15-minute poll is the safety net and uses
`updated_at_min` with a small overlap, so a missed delivery is picked up without
re-importing the catalogue.

Every cron job takes `filelock("shopify-<kind>-<store>")`, so the 15-minute
poll, the retry queue, a manual **Poll Orders Now** and inventory push can never
process the same store concurrently. Webhook jobs are not serialised per store
(that would make one slow order block the whole shop); they take a **per-order**
lock inside `import_order`, which is the granularity that matters.

### 4.1 Idempotency

* Sales Order: `(custom_shopify_store, shopify_order_id)`; a legacy row without
  a store (old connector) or with only `po_no` (historical CSV import) is
  adopted under a conditional `UPDATE`, never duplicated. An existing order is
  always refreshed, even when it predates the pull cutoff.
* Sales Invoice: `(store, shopify_order_id)`, and the invoice is **skipped**
  when the Sales Order is already billed by another process (PostEx / manual).
* Payment Entry: claimed on `Sales Invoice.custom_shopify_payment_entry` with a
  conditional `UPDATE`; a lost race cancels the duplicate it just created.
* Delivery Note: `(store, shopify_fulfillment_id)`, per-fulfillment lock, and
  the quantity is clamped to what is still pending on the Sales Order.
* Item and Customer: `Shopify Item Map` / `Shopify Customer Map`, unique per
  store (enforced by a unique index, see patch `v0_2.unique_map_indexes`).
* Webhook: `X-Shopify-Webhook-Id`; a repeated delivery is acknowledged and
  ignored, and `attempts` caps the automatic replay.

### 4.1.1 Failures are retried

A failed import (API error, contention, untaxed title, unmapped product) is
written to `Shopify Import Retry` and replayed every 10 minutes with exponential
backoff (6 attempts). A `Skipped` row means a product has no explicit Item Map:
it is retried once a day, so adding the mapping makes the order import itself.
A failed webhook delivery — Shopify never retries after a `200` — is replayed
every 15 minutes (5 attempts). **Shopify Orders Not Imported** is the worklist.

### 4.2 Posting dates

* Sales Order: Shopify `created_at`.
* Delivery Note: the fulfillment's `created_at`.
* Sales Invoice: per-company `Invoice Posting Date` (`Order Date` by default,
  `Paid Date` or `Today`).

Stock is **not** deducted by the invoice. Enable **Create Delivery Note on
Fulfillment** to move stock (one DN per fulfillment) or let your warehouse
process issue the DN.

### 4.3 Currency

If the Shopify order currency differs from the company currency, the document
is created in the order currency with the exchange rate for its posting date;
the mapping fails loudly if no rate exists.

---

## 5. Webhooks

Per-store URL (the **Show Webhook URL** button displays it):

```
https://<site>/api/method/shopify_integration.webhook.receive/<store>/<webhook_key>
```

* HMAC is verified against the **raw request body** with the static app's
  Shared Secret, the OAuth app's Client Secret, or the store's
  `Webhook Secret Override`.
* The key is an extra, unguessable path segment; the shop-domain header is used
  as a fallback selector.
* The endpoint only logs and enqueues. It creates **no** documents.
* Topics: `orders/create`, `orders/updated`, `orders/paid`,
  `orders/fulfilled`, `orders/partially_fulfilled`, `orders/cancelled`,
  `products/update`.

---

## 6. API (whitelisted, System Manager unless noted)

| Method | Purpose |
|---|---|
| `shopify_integration.api.health` | build/version/store counts |
| `shopify_integration.api.test_connection(store)` | shop name/currency + connectivity |
| `shopify_integration.api.register_webhooks(store)` | replace this store's webhooks |
| `shopify_integration.api.webhook_url(store)` | URL to paste into Shopify |
| `shopify_integration.api.fetch_locations(store)` | read Shopify locations |
| `shopify_integration.api.provision_warehouse_mapping(store)` | add rows using default warehouses |
| `shopify_integration.api.sync_now(store, mode, background)` | orders / inventory |
| `shopify_integration.api.import_order(store, order_id)` | pull one order |
| `shopify_integration.api.sync_old_orders(store, from, to)` | backfill a range |
| `shopify_integration.api.push_inventory(store)` | forced stock push |
| `shopify_integration.api.push_item(item_code, store?)` | push one item |
| `shopify_integration.api.push_customer(customer, store?)` | push one customer |
| `shopify_integration.api.replay_webhook(log_name)` | re-run a stored payload |
| `shopify_integration.api.picker_stores()` | enabled stores for the picker |
| `shopify_integration.api.search_catalogue(store, term, mode, status, refresh)` | products / flattened variants for the desk picker (10-minute cache) |
| `shopify_integration.api.map_item(item_code, store, product_id, variant_id, …)` | create/refresh a `Shopify Item Map` row (refuses to steal another item's mapping) |
| `shopify_integration.api.item_mappings(item_code)` | every store mapping of one item |

The guest endpoint is `shopify_integration.webhook.receive`.

---

## 7. Reports

* **Shopify Store Health** — per store: directions, last poll/webhook, orders,
  invoices, DNs, failures, status.
* **Shopify Unmapped Orders** — Sales Orders with a Shopify id but no store
  (the migration worklist).

---

## 8. Migrating from `ecommerce_integrations`

1. Install this app; leave both directions off.
2. Run:

   ```bash
   bench --site <site> execute shopify_integration.migrate.import_legacy_settings
   ```

   This creates one `Shopify Store` from `Shopify Setting`, copies the token,
   company defaults, tax rows and warehouse mapping, and backfills
   `custom_shopify_store` on Sales Orders / Invoices / Delivery Notes plus the
   item/customer maps from `Ecommerce Item` and `Customer.shopify_customer_id`.
   Re-running it is safe; `--kwargs "{'force': 1}"` re-copies the scalar config
   and mapping tables.
3. Compare with `shopify_integration.migrate.status`.
4. Disable the old connector: `Shopify Setting.enable_shopify = 0`.
5. Enable **Shopify → ERPNext** here and let the poll run in dry-run first if
   you want a shadow period.
6. Delete the old app only when you are satisfied; this app never writes to its
   doctypes.

The old and new connector must not both be enabled: both would import the same
order. The migration does **not** touch `Shopify Setting`, so disabling it is a
manual step.

---

## 9. Tests

Pure functions (routing, payload maths, HMAC) need no site:

```bash
cd shopify_integration
python -m pytest shopify_integration/tests -q
```

---

## 10. Known limits / deliberate choices

* **No order push** (ERPNext → Shopify Sales Order). The legacy connector never
  did it; items, prices, stock and (optionally) fulfilments are pushed. Creating
  Shopify orders from ERPNext needs a checkout/price-list workflow and is out
  of scope for v0.1.
* **No refunds/credit notes** from Shopify refunds. A refunded order stays
  un-invoiced; a delivered-then-refunded order needs a manual credit note.
* **No stock on invoice**: use Delivery Notes (or your own stock process).
* **One token per store**: Shopify's Admin API token is per app/shop.
* **Taxes as `Actual` rows**: Shopify sends amounts, not rates; ERPNext is
  driven with exact amounts so the invoice equals Shopify to the paisa.
* **Product options**: up to 3 options (Shopify's limit); more are ignored.
* **Webhook key** is generated once and read-only. Deleting and re-creating a
  store produces a new key, so press Register Webhooks again.
