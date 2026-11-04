frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		if (!frm.doc.shopify_order_id) return;
		add_shopify_link(frm, "Sales Order");
	},
});

function add_shopify_link(frm, doctype) {
	if (frm.__shopify_link_added) return;
	frm.__shopify_link_added = true;

	const store = frm.doc.custom_shopify_store;
	if (!store) return;

	frappe.db.get_value("Shopify Store", store, "shop_domain").then((r) => {
		const domain = (r.message || {}).shop_domain;
		if (!domain) return;
		const url = `https://${domain}/admin/orders/${frm.doc.shopify_order_id}`;
		frm.add_custom_button(__("Open in Shopify"), () => {
			window.open(url, "_blank");
		});
	});
}
