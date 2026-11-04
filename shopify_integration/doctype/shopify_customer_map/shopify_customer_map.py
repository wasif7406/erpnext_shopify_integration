import frappe
from frappe.model.document import Document


class ShopifyCustomerMap(Document):
	def validate(self):
		duplicate = frappe.db.get_value(
			"Shopify Customer Map",
			{
				"store": self.store,
				"shopify_customer_id": self.shopify_customer_id,
				"name": ("!=", self.name or ""),
			},
			"name",
		)
		if duplicate:
			frappe.throw(f"This Shopify customer is already mapped to {duplicate}.")
