"""Thin, dependency-free Shopify Admin REST client.

Uses `requests` directly instead of the old `shopify` python library: the REST
Admin API is stable, and avoiding the library means no pinned pyactiveresource
(or its silent retries) inside a Frappe worker.

Features the integration actually needs:
  * one token per store, refreshed lazily for OAuth stores;
  * cursor pagination (`Link` header) for every list endpoint;
  * a per-run request budget, so a misconfigured sync stops cleanly;
  * 429 handling that honours `Retry-After`, and bounded 5xx retries.
"""

import time
from urllib.parse import parse_qs, urlparse

import requests

from .config import StoreConfig, get_store
from .exceptions import (
	ShopifyBudgetExhausted,
	ShopifyClientError,
	ShopifyRateLimitError,
)
from .oauth import get_access_token

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 3


class RequestBudget:
	"""Counts HTTP calls in one run and refuses to exceed its share."""

	def __init__(self, limit: int):
		self.limit = max(1, int(limit or 800))
		self.spent = 0

	def spend(self):
		if self.spent >= self.limit:
			raise ShopifyBudgetExhausted(f"Request budget of {self.limit} calls is exhausted.")
		self.spent += 1

	def exhausted(self) -> bool:
		return self.spent >= self.limit


class ShopifyClient:
	def __init__(self, store, budget: RequestBudget | None = None):
		self.cfg = StoreConfig(store) if isinstance(store, str) else (
			store if isinstance(store, StoreConfig) else StoreConfig(store)
		)
		self.store = self.cfg.store
		self.budget = budget or RequestBudget(self.cfg.request_budget)
		self._token = None
		self._called = 0
		self.last_headers = {}

	# -- auth / urls ------------------------------------------------------
	@property
	def token(self) -> str:
		if self._token is None:
			self._token = get_access_token(self.store)
		return self._token

	def base_url(self, path: str = "") -> str:
		path = (path or "").lstrip("/")
		return f"https://{self.cfg.shop_domain}/admin/api/{self.cfg.api_version}/{path}"

	def _throttle(self):
		# Shopify's REST bucket is ~2 requests/second. Sleep *between* calls,
		# not before the first one, so a single-call endpoint is instant.
		if self._called:
			time.sleep(self.cfg.request_delay)
		self._called += 1

	def _request(self, method: str, path: str, params=None, payload=None):
		payload = _prune(payload)
		url = self.base_url(path) if not str(path).lower().startswith("http") else path
		headers = {
			"X-Shopify-Access-Token": self.token,
			"Content-Type": "application/json",
			"Accept": "application/json",
		}

		last_error = None
		for attempt in range(MAX_RETRIES):
			self.budget.spend()
			self._throttle()
			try:
				response = requests.request(
					method.upper(),
					url,
					headers=headers,
					params=params,
					json=payload,
					timeout=45,
				)
			except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
				last_error = exc
				if attempt < MAX_RETRIES - 1:
					time.sleep(2 * (attempt + 1))
					continue
				raise ShopifyClientError(f"Shopify request failed: {exc}") from exc

			self.last_headers = dict(response.headers or {})

			if response.status_code in (200, 201, 202, 204):
				if response.status_code == 204 or not response.content:
					return None
				try:
					return response.json()
				except ValueError as exc:
					raise ShopifyClientError("Shopify returned a non-JSON response.") from exc

			if response.status_code == 429:
				wait = _retry_after(response)
				if attempt < MAX_RETRIES - 1:
					time.sleep(wait)
					continue
				raise ShopifyRateLimitError("Shopify rate limit: retries exhausted.", 429, response.text)

			if response.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES - 1:
				time.sleep(2 * (attempt + 1))
				continue

			message = _error_message(response)
			raise ShopifyClientError(
				f"Shopify {method.upper()} {path} failed ({response.status_code}): {message}",
				response.status_code,
				response.text,
			)

		raise ShopifyClientError(f"Shopify request failed after retries: {last_error}")

	# -- verbs ------------------------------------------------------------
	def get(self, path: str, params=None):
		return self._request("GET", path, params=params)

	def post(self, path: str, payload=None):
		return self._request("POST", path, payload=payload)

	def put(self, path: str, payload=None):
		return self._request("PUT", path, payload=payload)

	def delete(self, path: str, payload=None):
		return self._request("DELETE", path, payload=payload)

	# -- pagination -------------------------------------------------------
	def paginate(self, path: str, resource_key: str, params=None, page_limit: int = 250):
		"""Yield items from a cursor-paginated list endpoint.

		Shopify returns the next cursor in the `Link` header, not in the body.
		Only `limit` + `page_info` are sent on subsequent calls; the API
		documents that any other filter is ignored once a cursor is used.
		"""
		params = dict(params or {})
		params.setdefault("limit", page_limit)
		cursor = None
		while True:
			call_params = {"limit": params["limit"]}
			if cursor:
				call_params["page_info"] = cursor
			else:
				call_params.update({k: v for k, v in params.items() if k != "limit"})

			data = self.get(path, call_params) or {}
			for item in data.get(resource_key) or []:
				yield item

			cursor = _next_page_info(self.last_headers.get("Link") or self.last_headers.get("link"))
			if not cursor:
				break

	# -- convenience ------------------------------------------------------
	def shop(self):
		return (self.get("shop.json") or {}).get("shop") or {}

	def locations(self):
		return (self.get("locations.json") or {}).get("locations") or []

	def order(self, order_id):
		return (self.get(f"orders/{order_id}.json") or {}).get("order") or {}

	def product(self, product_id):
		return (self.get(f"products/{product_id}.json") or {}).get("product") or {}

	def variant(self, variant_id):
		return (self.get(f"variants/{variant_id}.json") or {}).get("variant") or {}


def _prune(value):
	"""Drop `None` values recursively: Shopify rejects null options/fields."""
	if isinstance(value, dict):
		return {key: _prune(item) for key, item in value.items() if item is not None}
	if isinstance(value, list):
		return [_prune(item) for item in value]
	return value


def _retry_after(response) -> float:
	try:
		return min(30.0, max(1.0, float(response.headers.get("Retry-After") or 2)))
	except (TypeError, ValueError):
		return 2.0


def _error_message(response) -> str:
	try:
		data = response.json()
	except ValueError:
		return (response.text or "")[:500]
	if isinstance(data, dict):
		if data.get("errors"):
			errors = data["errors"]
			if isinstance(errors, dict):
				return "; ".join(f"{key}: {value}" for key, value in errors.items())
			return str(errors)
		if data.get("error"):
			return str(data["error"])
	return str(data)[:500]


def _next_page_info(link_header):
	"""Extract `page_info` from a Shopify `Link: <…>; rel="next"` header."""
	if not link_header:
		return None
	for part in link_header.split(","):
		section = part.strip()
		if 'rel="next"' not in section:
			continue
		url = section.split(";")[0].strip().strip("<>")
		query = parse_qs(urlparse(url).query)
		values = query.get("page_info") or []
		return values[0] if values else None
	return None


def client_for(store, budget: RequestBudget | None = None) -> ShopifyClient:
	return ShopifyClient(store, budget=budget)


def raw_shopify_request(store, method: str, path: str, payload=None):
	"""Small helper for API endpoints that only need connectivity."""
	return ShopifyClient(get_store(store))._request(method, path, payload=payload)
