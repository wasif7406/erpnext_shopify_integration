/**
 * Item form: link an ERPNext item to a Shopify product/variant by picking it
 * from the live catalogue. The `Shopify Item Map` row stays the single source
 * of truth — the item master only gains a button, never a copied id.
 */
frappe.ui.form.on("Item", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Link Shopify Product"), () => link_product(frm), __("Shopify"));
		frm.add_custom_button(__("Shopify Mappings"), () => show_mappings(frm), __("Shopify"));
	},
});

function link_product(frm) {
	open_picker({
		title: __("Link {0} to a Shopify product", [frm.doc.name]),
		on_select(row) {
			send_mapping(frm, row, 0);
		},
	});
}

/** Load the shared picker on first use (see shopify_item_map.js for why). */
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

function send_mapping(frm, row, force) {
	frappe.call({
		method: "shopify_integration.api.map_item",
		args: {
			item_code: frm.doc.name,
			store: row.store,
			product_id: row.product_id,
			variant_id: row.variant_id || null,
			sku: row.sku || null,
			inventory_item_id: row.inventory_item_id || null,
			has_variants: row.variant_id ? 0 : 1,
			force: force ? 1 : 0,
		},
		freeze: true,
		freeze_message: __("Saving the mapping…"),
		callback(response) {
			if (response.exc) {
				const message = server_message(response);
				if (!force && /already mapped/i.test(message)) {
					frappe.confirm(
						__("{0}<br><br>Move this Shopify product to {1}?", [
							frappe.utils.escape_html(message),
							frappe.utils.escape_html(frm.doc.name),
						]),
						() => send_mapping(frm, row, 1)
					);
				}
				return;
			}
			const mapping = response.message || {};
			frappe.show_alert({
				message: __("Mapped to {0}", [mapping.name || __("Shopify Item Map")]),
				indicator: "green",
			});
			frm.reload_doc();
		},
	});
}

function show_mappings(frm) {
	frappe.call({
		method: "shopify_integration.api.item_mappings",
		args: { item_code: frm.doc.name },
		callback(response) {
			const rows = response.message || [];
			if (!rows.length) {
				frappe.msgprint(__("This item is not linked to any Shopify store yet."));
				return;
			}
			let html =
				"<table class='table table-bordered'><thead><tr><th>Store</th><th>Product ID</th>" +
				"<th>Variant ID</th><th>SKU</th><th>Mapping</th></tr></thead><tbody>";
			rows.forEach((row) => {
				html += `<tr>
					<td>${frappe.utils.escape_html(row.store || "")}</td>
					<td>${frappe.utils.escape_html(row.product_id || "")}</td>
					<td>${frappe.utils.escape_html(row.variant_id || "—")}</td>
					<td>${frappe.utils.escape_html(row.sku || "—")}</td>
					<td><a href="/app/shopify-item-map/${encodeURIComponent(row.name)}">${
					row.has_variants ? __("Template") : __("Variant")
				}</a></td>
				</tr>`;
			});
			frappe.msgprint({
				title: __("Shopify Mappings"),
				message: html + "</tbody></table>",
				wide: true,
			});
		},
	});
}

function server_message(response) {
	try {
		const list = JSON.parse(response._server_messages || "[]");
		return list
			.map((entry) => {
				try {
					return JSON.parse(entry).message || "";
				} catch (e) {
					return "";
				}
			})
			.filter(Boolean)
			.join("\n");
	} catch (e) {
		return "";
	}
}
