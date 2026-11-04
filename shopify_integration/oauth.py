"""Shopify access tokens: static Admin API token and OAuth client credentials.

The static-token path is what most custom apps use. The OAuth path is the
Dev-Dashboard "client credentials" grant: it only works when the app and the
shop belong to the same Shopify organization, and the token lives ~24 hours,
so it is refreshed lazily before every run.
"""

import time
from datetime import timedelta

import frappe
import requests
from frappe.utils import cint, get_datetime, get_datetime_str, now_datetime
from frappe.utils.password import set_encrypted_password

from .constants import (
	AUTH_METHOD_OAUTH,
	AUTH_METHOD_STATIC,
	STORE_DOCTYPE,
)
from .exceptions import ShopifyAuthError

TOKEN_BUFFER_MINUTES = 5
DEFAULT_EXPIRES_IN = 86399


def normalize_shop_domain(value) -> str:
	text = (value or "").strip().lower()
	if not text:
		return ""
	text = text.replace("https://", "").replace("http://", "")
	return text.split("/")[0].split("?")[0]


def token_endpoint(shop_domain: str) -> str:
	return f"https://{normalize_shop_domain(shop_domain)}/admin/oauth/access_token"


def get_access_token(store) -> str:
	"""The token to send in `X-Shopify-Access-Token`."""
	if store.authentication_method == AUTH_METHOD_OAUTH:
		return get_valid_oauth_token(store)

	token = store.get_password("access_token", raise_exception=False)
	if not token:
		raise ShopifyAuthError(
			f"Shopify Store {store.name} has no Access Token. Add one or switch to OAuth."
		)
	return token


def is_token_valid(expires_at, buffer_minutes: int = TOKEN_BUFFER_MINUTES) -> bool:
	if not expires_at:
		return False
	try:
		return get_datetime(expires_at) > now_datetime() + timedelta(minutes=buffer_minutes)
	except Exception:
		return False


def get_valid_oauth_token(store) -> str:
	if is_token_valid(store.token_expires_at):
		token = store.get_password("oauth_access_token", raise_exception=False)
		if token:
			return token
	return refresh_oauth_token(store)


def refresh_oauth_token(store, persist: bool = True) -> str:
	if store.authentication_method != AUTH_METHOD_OAUTH:
		raise ShopifyAuthError("Token refresh is only applicable to OAuth 2.0 authentication.")

	client_secret = store.get_password("client_secret", raise_exception=False)
	if not store.client_id or not client_secret:
		raise ShopifyAuthError(
			f"Shopify Store {store.name}: Client ID and Client Secret are required for OAuth."
		)

	payload = {
		"grant_type": "client_credentials",
		"client_id": store.client_id,
		"client_secret": client_secret,
	}
	try:
		response = requests.post(
			token_endpoint(store.shop_domain),
			data=payload,
			headers={"Content-Type": "application/x-www-form-urlencoded"},
			timeout=30,
		)
	except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
		# One retry: a token refresh is the first call of every run, so a
		# transient DNS blip must not fail the whole run.
		time.sleep(1)
		try:
			response = requests.post(
				token_endpoint(store.shop_domain),
				data=payload,
				headers={"Content-Type": "application/x-www-form-urlencoded"},
				timeout=30,
			)
		except requests.exceptions.RequestException as retry_exc:
			raise ShopifyAuthError(f"Shopify token request failed: {retry_exc}") from retry_exc

	if response.status_code >= 400:
		error = None
		try:
			error = response.json()
			message = error.get("error_description") or error.get("error") or response.text
		except ValueError:
			message = response.text
		if isinstance(error, dict) and error.get("error") == "shop_not_permitted":
			message = (
				f"{message}. Client credentials work only when the app and shop belong to the "
				"same Shopify organization; for an external merchant store use a static token."
			)
		raise ShopifyAuthError(f"Shopify token request failed ({response.status_code}): {message}")

	try:
		data = response.json()
	except ValueError as exc:
		raise ShopifyAuthError("Shopify returned a non-JSON token response.") from exc

	token = data.get("access_token")
	if not token:
		raise ShopifyAuthError("Shopify returned no access_token.")

	expires_at = get_datetime_str(
		now_datetime() + timedelta(seconds=cint(data.get("expires_in")) or DEFAULT_EXPIRES_IN)
	)
	if persist:
		set_encrypted_password(STORE_DOCTYPE, store.name, token, fieldname="oauth_access_token")
		frappe.db.set_value(
			STORE_DOCTYPE, store.name, "token_expires_at", expires_at, update_modified=False
		)
		frappe.db.commit()
	else:
		store.oauth_access_token = token
		store.token_expires_at = expires_at
	return token


def webhook_secret(store) -> str:
	"""The HMAC secret Shopify signs webhooks with.

	Static-token apps sign with the app's Shared Secret; OAuth apps sign with
	the Client Secret. A store may also override it explicitly.
	"""
	override = store.get_password("webhook_secret", raise_exception=False)
	if override:
		return override
	if store.authentication_method == AUTH_METHOD_OAUTH:
		return store.get_password("client_secret", raise_exception=False) or ""
	return store.get_password("shared_secret", raise_exception=False) or ""


def masked(value) -> str:
	if not value:
		return ""
	return value[:4] + "…" if len(value) > 8 else "…"


def debug_token_state(store) -> dict:
	"""Read-only diagnostics used by `api.test_connection`."""
	return {
		"method": store.authentication_method or AUTH_METHOD_STATIC,
		"has_token": bool(
			store.get_password("oauth_access_token" if store.authentication_method == AUTH_METHOD_OAUTH else "access_token", raise_exception=False)
		),
		"token_expires_at": str(store.token_expires_at or ""),
		"has_webhook_secret": bool(webhook_secret(store)),
	}
