/**
 * Shopify Item Map: pick the product/variant from the live catalogue instead
 * of pasting numeric ids.
 *
 * Two entry points are wired deliberately:
 *  - the buttons inside the form (`custom_browse_shopify_products` /
 *    `custom_browse_shopify_variants`, provisioned by `fields.py`), which are
 *    where the merchant looks for them;
 *  - the form-header buttons, which need no migration and work on a brand new
 *    (not saved) mapping too.
 */
frappe.ui.form.on("Shopify Item Map", {
	refresh(frm) {
		prepare(frm);
	},

	store(frm) {
		prepare(frm);
	},

	custom_browse_shopify_products(frm) {
		pick(frm, "products");
	},

	custom_browse_shopify_variants(frm) {
		pick(frm, "variants");
	},
});

function prepare(frm) {
	if (!frm.__shopify_picker_buttons) {
		frm.__shopify_picker_buttons = true;
		frm.add_custom_button(__("Browse Shopify Products"), () => pick(frm, "products"));
		frm.add_custom_button(__("Browse Shopify Variants"), () => pick(frm, "variants"));
	}

	// The picker can ask for the store itself, so a missing store is a hint,
	// never a reason to hide the buttons.
	if (frm.doc.store) {
		if (frm.dashboard.clear_headline) frm.dashboard.clear_headline();
	} else {
		frm.dashboard.set_headline(
			__("Pick the product with “Browse Products” — the store is chosen in the dialog if the field is empty.")
		);
	}
}

function pick(frm, mode) {
	shopify_integration.picker.open({
		store: frm.doc.store || undefined,
		mode: mode,
		title:
			mode === "variants"
				? __("Select a Shopify Variant")
				: __("Select a Shopify Product"),
		on_select(row) {
			apply_row(frm, row);
		},
	});
}

function apply_row(frm, row) {
	if (!frm.doc.store && row.store) {
		frm.set_value("store", row.store);
	}

	frm.set_value("product_id", row.product_id);

	if (row.variant_id) {
		frm.set_value("variant_id", row.variant_id);
		frm.set_value("sku", row.sku || "");
		frm.set_value("inventory_item_id", row.inventory_item_id || "");
		frm.set_value("has_variants", 0);
	} else {
		// "Product only" = template item, every variant maps separately.
		frm.set_value("variant_id", "");
		frm.set_value("has_variants", 1);
	}

	// A SKU that is literally an ERPNext item code is the merchant's own link.
	if (!frm.doc.item_code && row.sku) {
		frappe.db.exists("Item", row.sku).then((name) => {
			if (name) frm.set_value("item_code", name);
		});
	}

	frappe.show_alert({
		message: __("Selected {0} (product {1}{2})", [
			row.label || row.title,
			row.product_id,
			row.variant_id ? ", variant " + row.variant_id : "",
		]),
		indicator: "green",
	});
}
