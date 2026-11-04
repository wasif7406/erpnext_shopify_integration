"""Company routing rules (pure functions, no Frappe site needed)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shopify_integration.routing import AmbiguousCompany, UnroutableOrder, resolve_company


def row(company, is_default=0, route_field="Any", route_value="", enabled=1):
	return {
		"company": company,
		"is_default": is_default,
		"route_field": route_field,
		"route_value": route_value,
		"enabled": enabled,
	}


def test_one_company_serves_every_order():
	rows = [row("A", is_default=1)]
	assert resolve_company(rows, {"source_name": "web"})["company"] == "A"


def test_two_companies_route_by_source_name():
	rows = [
		row("A", is_default=1),
		row("B", route_field="Source Name", route_value="pos"),
	]
	assert resolve_company(rows, {"source_name": "pos"})["company"] == "B"
	assert resolve_company(rows, {"source_name": "web"})["company"] == "A"


def test_route_is_case_insensitive():
	rows = [row("A", is_default=1), row("B", route_field="Currency", route_value="usd")]
	assert resolve_company(rows, {"currency": "USD"})["company"] == "B"


def test_shipping_country_matches_name_or_code():
	rows = [
		row("A", is_default=1),
		row("B", route_field="Shipping Country", route_value="pk"),
	]
	assert resolve_company(rows, {"shipping_address": {"country": "Pakistan"}})["company"] == "A"
	assert resolve_company(rows, {"shipping_address": {"country": "Pakistan", "country_code": "PK"}})[
		"company"
	] == "B"


def test_order_tag_matches_any_tag():
	rows = [row("A", is_default=1), row("B", route_field="Order Tag", route_value="wholesale")]
	assert resolve_company(rows, {"tags": "vip, wholesale ,gift"})["company"] == "B"
	assert resolve_company(rows, {"tags": ["vip", "gift"]})["company"] == "A"


def test_ambiguous_rules_refuse_to_guess():
	rows = [
		row("A", is_default=1),
		row("B", route_field="Currency", route_value="pkr"),
		row("C", route_field="Source Name", route_value="web"),
	]
	with pytest.raises(AmbiguousCompany):
		resolve_company(rows, {"currency": "PKR", "source_name": "web"})


def test_missing_default_is_an_error():
	rows = [
		row("A", route_field="Currency", route_value="usd"),
		row("B", route_field="Currency", route_value="eur"),
	]
	with pytest.raises(UnroutableOrder):
		resolve_company(rows, {"currency": "PKR"})


def test_disabled_company_is_ignored():
	rows = [row("A", is_default=0), row("B", is_default=1, enabled=0)]
	assert resolve_company(rows, {})["company"] == "A"
