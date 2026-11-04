"""Install / migrate hooks.

No legacy doctype is adopted: the old `ecommerce_integrations` app may still be
installed during the shadow period and it owns `Shopify Setting`,
`Ecommerce Item` and `Ecommerce Integration Log`. This app only *reads* those
once, in `migrate.import_legacy_settings`.
"""

import frappe

MODULE = "Shopify Integration"
APP = "shopify_integration"


def before_install():
	_ensure_module_def()
	frappe.db.commit()


def after_install():
	from .fields import ensure_custom_fields

	ensure_custom_fields()
	frappe.db.commit()
	print(
		"\nShopify Integration installed.\n"
		"Next steps:\n"
		"  1. Create a Shopify Store (Shopify Integration > Shopify Store) with the\n"
		"     shop domain and Admin API token, and add at least one company row.\n"
		"  2. Map warehouses (Fetch Shopify Locations helps) and tax accounts.\n"
		"  3. Test the connection, then press Register Webhooks.\n"
		"  4. Keep both sync directions OFF until you are ready: they default to off.\n"
		"  5. Migrating from ecommerce_integrations? Run:\n"
		"     bench --site <site> execute shopify_integration.migrate.import_legacy_settings\n"
		"     then set 'Enable Shopify' to 0 in the old Shopify Setting so the two\n"
		"     connectors never import the same order.\n"
	)


def after_migrate():
	"""Never let field provisioning abort a migrate."""
	try:
		from .fields import ensure_custom_fields

		ensure_custom_fields()
		frappe.clear_cache()
	except Exception:
		frappe.log_error(title="Shopify: after_migrate failed", message=frappe.get_traceback())


def _ensure_module_def():
	if frappe.db.exists("Module Def", MODULE):
		return
	try:
		frappe.get_doc(
			{
				"doctype": "Module Def",
				"module_name": MODULE,
				"app_name": APP,
				"custom": 0,
			}
		).insert(ignore_permissions=True)
	except Exception:
		# Frappe creates it during the doctype sync if this fails here.
		frappe.log_error("Shopify: could not create Module Def", frappe.get_traceback())
