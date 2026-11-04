import frappe
from frappe.model.document import Document


class ShopifyItemMap(Document):
	def validate(self):
		duplicate = frappe.db.get_value(
			"Shopify Item Map",
			{
				"store": self.store,
				"product_id": self.product_id or "",
				"variant_id": self.variant_id or "",
				"name": ("!=", self.name or ""),
			},
			"name",
		)
		if duplicate:
			frappe.throw(f"This Shopify product/variant is already mapped to {duplicate}.")
