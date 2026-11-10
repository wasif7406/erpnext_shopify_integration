frappe.ui.form.on("Shopify Store", {
	refresh(frm) {
		bind_actions(frm);
		set_filters(frm);

		if (frm.doc.webhook_key && !frm.doc.webhook_url) {
			frappe.call({
				method: "shopify_integration.api.webhook_url",
				args: { store: frm.doc.name },
				callback: (r) => {
					if (r.message) frm.set_value("webhook_url", r.message);
				},
			});
		}
	},

	test_connection(frm) {
		if (frm.is_new()) {
			frappe.msgprint(__("Save the store before testing the connection."));
			return;
		}
		frappe
			.call({
				method: "shopify_integration.api.test_connection",
				args: { store: frm.doc.name },
				freeze: true,
			})
			.then((r) => {
				const data = r.message || {};
				frappe.msgprint({
					title: __("Connected"),
					indicator: "green",
					message: __("{0} · {1} · {2}", [
						data.shop || "",
						data.domain || "",
						data.currency || "",
					]),
				});
			});
	},

	register_webhooks(frm) {
		if (frm.is_new()) {
			frappe.msgprint(__("Save the store first (the webhook key is created on save)."));
			return;
		}
		frappe.confirm(
			__("Replace the webhooks registered for this site with the current topic list?"),
			() =>
				frappe
					.call({
						method: "shopify_integration.api.register_webhooks",
						args: { store: frm.doc.name },
						freeze: true,
						freeze_message: __("Registering webhooks…"),
					})
					.then((r) => {
						const result = r.message || {};
						frappe.show_alert({
							message: __("{0} webhooks registered.", [result.registered || 0]),
							indicator: "green",
						});
						frm.reload_doc();
					})
		);
	},

	fetch_locations(frm) {
		if (frm.is_new()) {
			frappe.msgprint(__("Save the store first."));
			return;
		}
		frappe
			.call({
				method: "shopify_integration.api.fetch_locations",
				args: { store: frm.doc.name },
				freeze: true,
			})
			.then((r) => {
				const locations = r.message || [];
				if (!locations.length) {
					frappe.msgprint(__("Shopify returned no locations."));
					return;
				}
				let html = "<table class='table table-bordered'><thead><tr><th>ID</th><th>Name</th><th>City</th><th>Country</th></tr></thead><tbody>";
				locations.forEach((row) => {
					html += `<tr><td>${row.id}</td><td>${row.name || ""}</td><td>${row.city || ""}</td><td>${row.country || ""}</td></tr>`;
				});
				html += "</tbody></table>";
				frappe.msgprint({
					title: __("Shopify Locations"),
					message: html,
					wide: true,
					primary_action: {
						label: __("Add missing mappings using each company's default warehouse"),
						action() {
							frappe
								.call({
									method: "shopify_integration.api.provision_warehouse_mapping",
									args: { store: frm.doc.name },
									freeze: true,
								})
								.then((res) => {
									frappe.show_alert({
										message: __("{0} mapping rows added.", [(res.message || {}).added || 0]),
										indicator: "green",
									});
									hide_msgprint();
									frm.reload_doc();
								});
						},
					},
				});
			});
	},

	sync_orders_now(frm) {
		run_now(frm, "orders");
	},

	push_inventory_now(frm) {
		run_now(frm, "inventory");
	},
});

frappe.ui.form.on("Shopify Store Company", {
	company(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, "cash_bank_account", "");
		frappe.model.set_value(cdt, cdn, "default_warehouse", "");
		frappe.model.set_value(cdt, cdn, "cost_center", "");
		frappe.model.set_value(cdt, cdn, "default_sales_tax_account", "");
		frappe.model.set_value(cdt, cdn, "default_shipping_charges_account", "");
	},
	route_field(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (row.route_field === "Any") {
			frappe.model.set_value(cdt, cdn, "route_value", "");
		}
	},
	is_default(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.is_default) return;
		(frm.doc.companies || []).forEach((other) => {
			if (other.name !== row.name && other.is_default) {
				frappe.model.set_value(other.doctype, other.name, "is_default", 0);
			}
		});
	},
});

frappe.ui.form.on("Shopify Store Warehouse", {
	company(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, "warehouse", "");
	},
});

frappe.ui.form.on("Shopify Store Tax", {
	company(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, "tax_account", "");
	},
});

function bind_actions(frm) {
	if (frm.__shopify_buttons_added) return;
	frm.__shopify_buttons_added = true;
	frm.add_custom_button(__("Add Item Mapping"), () => add_item_mapping(frm));
	frm.add_custom_button(__("View Item Mappings"), () => {
		frappe.route_options = { store: frm.doc.name };
		frappe.set_route("List", "Shopify Item Map");
	});
	frm.add_custom_button(__("Show Webhook URL"), () => {
		if (!frm.doc.name) return;
		frappe.call({
			method: "shopify_integration.api.webhook_url",
			args: { store: frm.doc.name },
			callback: (r) => {
				if (r.message) {
					frappe.msgprint({
						title: __("Webhook URL"),
						message: `<code style="word-break:break-all">${r.message}</code>`,
						wide: true,
					});
				}
			},
		});
	});
}

function set_filters(frm) {
	const by_company = (field) => (doc, cdt, cdn) => {
		const row = locals[cdt][cdn] || {};
		return { filters: { company: row.company, is_group: 0 } };
	};
	const account = (field) => (doc, cdt, cdn) => {
		const row = locals[cdt][cdn] || {};
		return { filters: { company: row.company, is_group: 0 } };
	};

	["default_warehouse"].forEach((field) => frm.set_query(field, "companies", by_company(field)));
	["cost_center"].forEach((field) =>
		frm.set_query(field, "companies", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn] || {};
			return { filters: { company: row.company, is_group: 0 } };
		})
	);
	[
		"cash_bank_account",
		"default_sales_tax_account",
		"default_shipping_charges_account",
	].forEach((field) => frm.set_query(field, "companies", account(field)));

	frm.set_query("tax_account", "taxes", (doc, cdt, cdn) => {
		const row = locals[cdt][cdn] || {};
		return { filters: { company: row.company, is_group: 0 } };
	});
	frm.set_query("warehouse", "warehouse_mapping", (doc, cdt, cdn) => {
		const row = locals[cdt][cdn] || {};
		return { filters: { company: row.company, is_group: 0 } };
	});
}

function add_item_mapping(frm) {
	if (frm.is_new()) {
		frappe.msgprint(__("Save the store first."));
		return;
	}
	const dialog = new frappe.ui.Dialog({
		title: __("Map a Shopify product to an ERPNext Item"),
		fields: [
			{
				fieldname: "item_code",
				fieldtype: "Link",
				options: "Item",
				label: __("ERPNext Item"),
				reqd: 1,
			},
			{
				fieldname: "hint",
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"Next, pick the product or variant from this store's live catalogue. The product ID, variant ID and SKU are filled in automatically."
				)}</p>`,
			},
		],
		primary_action_label: __("Choose Shopify Product…"),
		primary_action(values) {
			if (!values.item_code) {
				frappe.msgprint(__("Select an ERPNext Item first."));
				return;
			}
			dialog.hide();
			open_picker({
				store: frm.doc.name,
				title: __("Select a Shopify product for {0}", [values.item_code]),
				on_select(row) {
					map_picked_item(frm, values.item_code, row);
				},
			});
		},
		secondary_action_label: __("Enter IDs manually"),
		secondary_action() {
			dialog.hide();
			add_item_mapping_manual(frm);
		},
	});
	dialog.show();
}

function map_picked_item(frm, item_code, row) {
	frappe.call({
		method: "shopify_integration.api.map_item",
		args: {
			item_code: item_code,
			store: frm.doc.name,
			product_id: row.product_id,
			variant_id: row.variant_id || null,
			sku: row.sku || null,
			inventory_item_id: row.inventory_item_id || null,
			has_variants: row.variant_id ? 0 : 1,
		},
		freeze: true,
		freeze_message: __("Saving the mapping…"),
		callback(response) {
			if (response.exc) return;
			const mapping = response.message || {};
			frappe.show_alert({
				message: __("{0} mapped to {1}", [item_code, mapping.name || __("Shopify Item Map")]),
				indicator: "green",
			});
			frm.reload_doc();
		},
	});
}

function add_item_mapping_manual(frm) {
	if (frm.is_new()) {
		frappe.msgprint(__("Save the store first."));
		return;
	}
	const dialog = new frappe.ui.Dialog({
		title: __("Map a Shopify product to an ERPNext Item"),
		fields: [
			{
				fieldname: "item_code",
				fieldtype: "Link",
				options: "Item",
				label: __("ERPNext Item"),
				reqd: 1,
			},
			{
				fieldname: "sku",
				fieldtype: "Data",
				label: __("Shopify SKU"),
				description: __("Usually the simplest key: the SKU on the Shopify order line."),
			},
			{
				fieldname: "product_id",
				fieldtype: "Data",
				label: __("Shopify Product ID"),
			},
			{
				fieldname: "variant_id",
				fieldtype: "Data",
				label: __("Shopify Variant ID"),
			},
		],
		primary_action_label: __("Add Mapping"),
		primary_action(values) {
			if (!values.sku && !values.product_id && !values.variant_id) {
				frappe.msgprint(__("Enter at least a SKU, a product ID or a variant ID."));
				return;
			}
			frappe.call({
				method: "frappe.client.insert",
				args: {
					doc: Object.assign(
						{ doctype: "Shopify Item Map", store: frm.doc.name },
						values
					),
				},
				freeze: true,
				callback: (r) => {
					if (!r.exc) {
						dialog.hide();
						frappe.show_alert({
							message: __("Item mapping added."),
							indicator: "green",
						});
					}
				},
			});
		},
	});
	dialog.show();
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

function run_now(frm, mode) {
	if (frm.is_new()) return;
	frappe
		.call({
			method: "shopify_integration.api.sync_now",
			args: { store: frm.doc.name, mode, background: 1 },
			freeze: true,
			freeze_message: __("Queued. This can take a while for a large catalogue."),
		})
		.then(() => {
			frappe.show_alert({ message: __("Sync queued."), indicator: "green" });
		});
}

function hide_msgprint() {
	if (cur_dialog) cur_dialog.hide();
}
