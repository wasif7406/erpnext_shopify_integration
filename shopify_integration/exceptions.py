"""Exceptions raised by the Shopify integration.

They are deliberately small and specific so a caller can decide whether an
error is retryable (budget/rate limit) or permanent (configuration).
"""


class ShopifyIntegrationError(Exception):
	"""Base class for every error raised by this app."""


class ShopifyConfigError(ShopifyIntegrationError):
	"""The store/company configuration cannot resolve a required value."""


class ShopifyAuthError(ShopifyIntegrationError):
	"""Credentials are missing, expired or rejected by Shopify."""


class ShopifyClientError(ShopifyIntegrationError):
	"""Shopify returned a non-retryable HTTP error."""

	def __init__(self, message, status_code=None, response=None):
		super().__init__(message)
		self.status_code = status_code
		self.response = response


class ShopifyRateLimitError(ShopifyClientError):
	"""Shopify returned HTTP 429 after the allowed retries."""


class ShopifyBudgetExhausted(ShopifyIntegrationError):
	"""The per-run request budget was spent; the run stops cleanly."""


class ShopifyDuplicateDocument(ShopifyIntegrationError):
	"""Another run already created the document this run wanted to create."""


class ShopifyAmbiguousCompany(ShopifyConfigError):
	"""A store serves several companies and an order matches more than one."""
