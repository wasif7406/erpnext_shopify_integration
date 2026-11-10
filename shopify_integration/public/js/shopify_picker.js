/**
 * Shopify catalogue picker.
 *
 * One dialog that lists the live products (or every variant) of a store and
 * hands the chosen ids back to the caller. Used by `Shopify Item Map`, by the
 * Item form and by the "Add Item Mapping" action on `Shopify Store`, so the
 * merchant never has to copy a product or variant id by hand.
 *
 *   shopify_integration.picker.open({
 *     store: "SHOP-0001",        // optional: one store, or the picker asks
 *     mode: "products",          // "products" | "variants"
 *     on_select(row) { ... }     // row: product_id, variant_id, sku, ...
 *   });
 */
frappe.provide("shopify_integration.picker");

(function () {
	const DEBOUNCE_MS = 250;

	function esc(value) {
		return frappe.utils.escape_html(value == null ? "" : String(value));
	}

	function status_pill(status) {
		const colours = { active: "green", draft: "orange", unlisted: "blue", archived: "gray" };
		const colour = colours[status] || "gray";
		return `<span class="indicator-pill ${colour}">${esc(status)}</span>`;
	}

	function price(value) {
		return frappe.format(value || 0, { fieldtype: "Currency" });
	}

	function debounce(fn, wait) {
		let timer = null;
		return function () {
			const args = arguments;
			clearTimeout(timer);
			timer = setTimeout(() => fn.apply(this, args), wait);
		};
	}

	shopify_integration.picker.open = function (options) {
		const opts = options || {};
		const state = {
			store: opts.store || null,
			mode: (opts.mode || "products").toLowerCase() === "variants" ? "variants" : "products",
			term: "",
			rows: [],
			meta: {},
			expanded: {},
			loaded: false,
			token: 0,
		};

		const dialog = new frappe.ui.Dialog({
			title: opts.title || __("Browse Shopify Catalogue"),
			size: "large",
			fields: [
				{
					fieldname: "store",
					fieldtype: "Link",
					options: "Shopify Store",
					label: __("Shopify Store"),
					description: __("The list is read live from this store's catalogue."),
					onchange() {
						state.store = dialog.get_value("store") || null;
						state.expanded = {};
						state.loaded = false;
						load(false);
					},
				},
				{
					fieldname: "mode",
					fieldtype: "Select",
					label: __("Show"),
					options: ["Products", "Variants"],
					default: state.mode === "variants" ? "Variants" : "Products",
					onchange() {
						state.mode = dialog.get_value("mode") === "Variants" ? "variants" : "products";
						state.expanded = {};
						load(false);
					},
				},
				{
					fieldname: "term",
					fieldtype: "Data",
					label: __("Search"),
					description: __("Title, handle, SKU, barcode, product or variant id."),
				},
				{ fieldname: "results", fieldtype: "HTML" },
			],
			primary_action_label: __("Close"),
			primary_action() {
				dialog.hide();
			},
		});

		// -- data ---------------------------------------------------------
		function load(refresh) {
			if (!state.store) {
				render();
				return;
			}
			state.token += 1;
			const token = state.token;
			frappe.call({
				method: "shopify_integration.api.search_catalogue",
				args: {
					store: state.store,
					term: state.term,
					mode: state.mode,
					refresh: refresh ? 1 : 0,
				},
				freeze: !state.loaded,
				freeze_message: refresh
					? __("Re-reading the Shopify catalogue…")
					: __("Reading the Shopify catalogue…"),
				callback(response) {
					if (token !== state.token) return; // a newer search already won
					const data = response.message || {};
					state.rows = data.rows || [];
					state.meta = data;
					state.loaded = true;
					render();
				},
			});
		}

		// -- rendering ----------------------------------------------------
		function render() {
			const wrapper = dialog.fields_dict.results.$wrapper;
			if (!state.store) {
				wrapper.html(
					`<p class="text-muted">${__("Select a Shopify Store to browse its catalogue.")}</p>`
				);
				return;
			}
			if (!state.loaded) {
				wrapper.html(`<p class="text-muted">${__("Loading…")}</p>`);
				return;
			}
			wrapper.html(header() + (state.mode === "variants" ? variants_table() : products_table()));
			bind(wrapper);
		}

		function header() {
			const meta = state.meta || {};
			const parts = [];
			parts.push(__("{0} products read", [meta.fetched || 0]));
			parts.push(__("{0} match(es)", [meta.total_matches || 0]));
			if ((meta.total_matches || 0) > state.rows.length) {
				parts.push(__("showing the first {0} — refine the search", [state.rows.length]));
			}
			if (meta.truncated) {
				parts.push(__("catalogue truncated"));
			}
			if (meta.cached_on) {
				parts.push(__("cached {0}", [meta.cached_on]));
			}
			return `<div class="flex justify-between align-center" style="margin-bottom:8px">
					<div class="text-muted small">${esc(parts.join(" · "))}</div>
					<button class="btn btn-xs btn-default si-refresh">${__("Refresh from Shopify")}</button>
				</div>`;
		}

		function products_table() {
			let html = `<div style="max-height:60vh;overflow:auto">
				<table class="table table-sm" style="margin-bottom:0">
				<thead><tr>
					<th style="width:56px"></th>
					<th>${__("Product")}</th>
					<th style="width:90px">${__("Variants")}</th>
					<th style="width:190px"></th>
				</tr></thead><tbody>`;
			if (!state.rows.length) {
				html += `<tr><td colspan="4" class="text-muted">${__("No product matches.")}</td></tr>`;
			}
			state.rows.forEach((product, index) => {
				html += product_row(product, index);
				if (state.expanded[product.product_id]) {
					html += variant_child_rows(product);
				}
			});
			return html + "</tbody></table></div>";
		}

		function product_row(product, index) {
			const skus = (product.skus || []).slice(0, 3).join(", ");
			const extra = (product.skus || []).length > 3 ? " …" : "";
			const image = product.image
				? `<img src="${esc(product.image)}" style="width:40px;height:40px;object-fit:cover;border-radius:4px" />`
				: "";
			const multiple = (product.variant_count || 0) > 1;
			const action = multiple
				? `<button class="btn btn-xs btn-default si-expand" data-idx="${index}">${__("Choose variant")}</button>`
				: `<button class="btn btn-xs btn-primary si-pick-product" data-idx="${index}">${__("Select")}</button>`;
			const only = multiple
				? `<button class="btn btn-xs btn-link si-pick-template" data-idx="${index}">${__("Product only")}</button>`
				: "";
			return `<tr>
				<td>${image}</td>
				<td>
					<div><b>${esc(product.title)}</b> ${status_pill(product.status)}</div>
					<div class="text-muted small">ID ${esc(product.product_id)}${
						product.handle ? " · " + esc(product.handle) : ""
					}</div>
					<div class="text-muted small">${__("SKUs")}: ${esc(skus || "—")}${extra}</div>
				</td>
				<td>${product.variant_count || 0}</td>
				<td class="text-right">${action} ${only}</td>
			</tr>`;
		}

		function variant_child_rows(product) {
			if (!(product.variants || []).length) {
				return `<tr><td></td><td colspan="3" class="text-muted">${__("No variants.")}</td></tr>`;
			}
			return product.variants
				.map(
					(variant) => `<tr style="background:var(--bg-light-gray,#fafafa)">
					<td></td>
					<td style="padding-left:22px">
						↳ <b>${esc(variant.title)}</b>
						${
							(variant.options || []).length
								? `<span class="text-muted"> (${esc(variant.options.join(" / "))})</span>`
								: ""
						}
						<div class="text-muted small">${__("Variant ID")} ${esc(variant.variant_id)} · ${__(
						"SKU"
					)} ${esc(variant.sku || "—")} · ${__("Stock")} ${variant.inventory_quantity || 0}</div>
					</td>
					<td>${price(variant.price)}</td>
					<td class="text-right">
						<button class="btn btn-xs btn-primary si-pick-variant"
							data-pid="${esc(product.product_id)}" data-vid="${esc(variant.variant_id)}">
							${__("Select")}
						</button>
					</td>
				</tr>`
				)
				.join("");
		}

		function variants_table() {
			let html = `<div style="max-height:60vh;overflow:auto">
				<table class="table table-sm" style="margin-bottom:0">
				<thead><tr>
					<th style="width:56px"></th>
					<th>${__("Product / Variant")}</th>
					<th style="width:150px">${__("SKU")}</th>
					<th style="width:90px" class="text-right">${__("Price")}</th>
					<th style="width:70px" class="text-right">${__("Stock")}</th>
					<th style="width:90px"></th>
				</tr></thead><tbody>`;
			if (!state.rows.length) {
				html += `<tr><td colspan="6" class="text-muted">${__("No variant matches.")}</td></tr>`;
			}
			state.rows.forEach((variant) => {
				const image = variant.image
					? `<img src="${esc(variant.image)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px" />`
					: "";
				html += `<tr>
					<td>${image}</td>
					<td>
						<div><b>${esc(variant.product_title)}</b> ${status_pill(variant.product_status)}</div>
						<div class="text-muted small">${esc(variant.title)} · ${__("Variant ID")} ${esc(
					variant.variant_id
				)}</div>
					</td>
					<td>${esc(variant.sku || "—")}</td>
					<td class="text-right">${price(variant.price)}</td>
					<td class="text-right">${variant.inventory_quantity || 0}</td>
					<td class="text-right">
						<button class="btn btn-xs btn-primary si-pick-variant"
							data-pid="${esc(variant.product_id)}" data-vid="${esc(variant.variant_id)}">
							${__("Select")}
						</button>
					</td>
				</tr>`;
			});
			return html + "</tbody></table></div>";
		}

		// -- interaction --------------------------------------------------
		function bind(wrapper) {
			wrapper.find(".si-refresh").on("click", () => {
				state.loaded = false;
				render();
				load(true);
			});
			wrapper.find(".si-expand").on("click", (event) => {
				const product = state.rows[Number($(event.currentTarget).attr("data-idx"))];
				if (!product) return;
				state.expanded[product.product_id] = true;
				render();
			});
			wrapper.find(".si-pick-product").on("click", (event) => {
				const product = state.rows[Number($(event.currentTarget).attr("data-idx"))];
				if (!product) return;
				select_product(product);
			});
			wrapper.find(".si-pick-template").on("click", (event) => {
				const product = state.rows[Number($(event.currentTarget).attr("data-idx"))];
				if (!product) return;
				choose({
					product_id: product.product_id,
					variant_id: "",
					sku: "",
					inventory_item_id: "",
					has_variants: 1,
					title: product.title,
					variant_title: "",
					image: product.image,
				});
			});
			wrapper.find(".si-pick-variant").on("click", (event) => {
				const $button = $(event.currentTarget);
				const productId = $button.attr("data-pid");
				const variantId = $button.attr("data-vid");
				const variant = find_variant(productId, variantId);
				if (!variant) return;
				choose({
					product_id: productId,
					variant_id: variantId,
					sku: variant.sku || "",
					inventory_item_id: variant.inventory_item_id || "",
					has_variants: 0,
					title: variant.product_title || variant.product_handle || productId,
					variant_title: variant.title || "",
					image: variant.image || "",
				});
			});
		}

		function select_product(product) {
			const variants = product.variants || [];
			if (variants.length !== 1) {
				// Never guess which variant the merchant wants.
				state.expanded[product.product_id] = true;
				render();
				return;
			}
			const variant = variants[0];
			choose({
				product_id: product.product_id,
				variant_id: variant.variant_id,
				sku: variant.sku || "",
				inventory_item_id: variant.inventory_item_id || "",
				has_variants: 0,
				title: product.title,
				variant_title: variant.title || "",
				image: product.image,
			});
		}

		function find_variant(productId, variantId) {
			if (state.mode === "variants") {
				return state.rows.find(
					(row) => String(row.product_id) === String(productId) && String(row.variant_id) === String(variantId)
				);
			}
			const product = state.rows.find((row) => String(row.product_id) === String(productId));
			if (!product) return null;
			const variant = (product.variants || []).find(
				(row) => String(row.variant_id) === String(variantId)
			);
			if (!variant) return null;
			return Object.assign({}, variant, {
				product_id: product.product_id,
				product_title: product.title,
				image: product.image,
			});
		}

		function choose(row) {
			row.store = state.store;
			row.label = row.variant_title ? `${row.title} — ${row.variant_title}` : row.title;
			dialog.hide();
			if (typeof opts.on_select === "function") {
				opts.on_select(row);
			}
		}

		// -- boot ---------------------------------------------------------
		dialog.show();

		dialog.fields_dict.store.get_query = () => ({ filters: { enabled: 1 } });

		dialog.fields_dict.term.$input.on(
			"input",
			debounce(function () {
				state.term = dialog.get_value("term") || "";
				load(false);
			}, DEBOUNCE_MS)
		);

		if (opts.store) {
			dialog.set_value("store", opts.store);
			dialog.set_df_property("store", "read_only", 1);
			state.store = opts.store;
			load(false);
			return;
		}

		frappe.call({
			method: "shopify_integration.api.picker_stores",
			callback(response) {
				const stores = response.message || [];
				if (!stores.length) {
					dialog.fields_dict.results.$wrapper.html(
						`<p class="text-muted">${__("No enabled Shopify Store exists yet.")}</p>`
					);
					return;
				}
				if (stores.length === 1) {
					dialog.set_value("store", stores[0].name);
					dialog.set_df_property("store", "read_only", 1);
					state.store = stores[0].name;
					load(false);
					return;
				}
				render();
			},
		});
	};
})();
