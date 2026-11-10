/**
 * Shopify Item Map: pick the product/variant from the live catalogue instead
 * of pasting numeric ids.
 *
 * One button only: the dialog lists products and lets the merchant switch to
 * the flattened variant list from its "Show" selector — two buttons opened the
 * same dialog, which was just noise.
 *
 * Two entry points are wired deliberately:
 *  - the button inside the form (`custom_browse_shopify`, provisioned by
 *    `fields.py`), which is where the merchant looks for it;
 *  - the form-header button, which needs no migration and works on a brand new
 *    (not saved) mapping too.
 */
frappe.ui.form.on("Shopify Item Map", {
	refresh(frm) {
		prepare(frm);
	},

	store(frm) {
		prepare(frm);
	},

	custom_browse_shopify(frm) {
		pick(frm);
	},
});

function prepare(frm) {
	if (!frm.__shopify_picker_buttons) {
		frm.__shopify_picker_buttons = true;
		frm.add_custom_button(__("Browse Shopify Products"), () => pick(frm));
	}

	// The picker can ask for the store itself, so a missing store is a hint,
	// never a reason to hide the button.
	if (frm.doc.store) {
		if (frm.dashboard.clear_headline) frm.dashboard.clear_headline();
	} else {
		frm.dashboard.set_headline(
			__("Pick the product with “Browse Shopify Products” — the store is chosen in the dialog if the field is empty.")
		);
	}
}

function pick(frm) {
	open_picker({
		store: frm.doc.store || undefined,
		mode: "products",
		title: __("Select a Shopify Product"),
		on_select(row) {
			apply_row(frm, row);
		},
	});
}

/**
 * Load the shared picker module on first use.
 *
 * It is deliberately not an `app_include_js` entry: if that include silently
 * misses, the buttons do nothing at all. `frappe.require` resolves the asset
 * path directly and surfaces a real error when it cannot.
 */
function open_picker(options) {
	const ready =
		window.shopify_integration &&
		shopify_integration.picker &&
		typeof shopify_integration.picker.open === "function";

	if (ready) {
		shopify_integration.picker.open(options);
		return;
	}

	frappe.require("/assets/shopify_integration/js/shopify_picker.js", () => {
		if (
			window.shopify_integration &&
			shopify_integration.picker &&
			typeof shopify_integration.picker.open === "function"
		) {
			shopify_integration.picker.open(options);
			return;
		}
		frappe.msgprint({
			title: __("Shopify"),
			indicator: "red",
			message: __(
				"Could not load the catalogue picker script. Reload the page with Ctrl+Shift+R and try again."
			),
		});
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
