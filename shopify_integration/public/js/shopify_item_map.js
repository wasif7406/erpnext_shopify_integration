/**
 * Shopify Item Map: pick the product/variant from the live catalogue instead
 * of pasting numeric ids.
 */
frappe.ui.form.on("Shopify Item Map", {
	refresh(frm) {
		if (!frm.doc.store) {
			frm.dashboard.set_headline(
				__("Select a Shopify Store first — the product list is read from it.")
			);
			return;
		}
		frm.add_custom_button(__("Browse Products"), () => pick(frm, "products"), __("Shopify"));
		frm.add_custom_button(__("Browse Variants"), () => pick(frm, "variants"), __("Shopify"));
	},
});

function pick(frm, mode) {
	shopify_integration.picker.open({
		store: frm.doc.store,
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
