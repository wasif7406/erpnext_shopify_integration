import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_url

from shopify_integration import config
from shopify_integration.constants import CALLBACK_METHOD


class ShopifyStore(Document):
	def validate(self):
		config.ensure_single_default(self)
		config.validate_store(self)
		self.webhook_url = build_webhook_url(self)

	def before_save(self):
		if not self.webhook_key:
			self.webhook_key = frappe.generate_hash(length=32)

	def after_insert(self):
		# The URL needs the document name, which only exists after insert.
		self.db_set("webhook_url", build_webhook_url(self))

	def on_trash(self):
		linked = (
			frappe.db.count("Sales Order", {"custom_shopify_store": self.name})
			+ frappe.db.count("Sales Invoice", {"custom_shopify_store": self.name})
			+ frappe.db.count("Delivery Note", {"custom_shopify_store": self.name})
			+ frappe.db.count("Shopify Item Map", {"store": self.name})
			+ frappe.db.count("Shopify Customer Map", {"store": self.name})
		)
		if linked:
			frappe.throw(
				_(
					"This store still has {0} linked documents/mappings. Disable it instead of "
					"deleting it, or clean the links first."
				).format(linked)
			)


def build_webhook_url(doc) -> str:
	if not doc.name or not doc.webhook_key:
		return ""
	return get_url(f"/api/method/{CALLBACK_METHOD}/{doc.name}/{doc.webhook_key}")
