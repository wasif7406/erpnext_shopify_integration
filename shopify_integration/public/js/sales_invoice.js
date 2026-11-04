frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		if (!frm.doc.shopify_order_id || !frm.doc.custom_shopify_store) return;
		if (frm.__shopify_link_added) return;
		frm.__shopify_link_added = true;

		frappe.db
			.get_value("Shopify Store", frm.doc.custom_shopify_store, "shop_domain")
			.then((r) => {
				const domain = (r.message || {}).shop_domain;
				if (!domain) return;
				const url = `https://${domain}/admin/orders/${frm.doc.shopify_order_id}`;
				frm.add_custom_button(__("Open in Shopify"), () => {
					window.open(url, "_blank");
				});
			});
	},
});
