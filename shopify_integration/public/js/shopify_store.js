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
