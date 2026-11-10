"""Catalogue picker helpers: normalisation, search and row clamping."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shopify_integration.picker import (
	clamp,
	normalize_product,
	normalize_variant,
	search_products,
	search_variants,
	sort_products,
	variant_rows,
)

RAW = {
	"id": 8907288772696,
	"title": "3D Fireworks Bulb",
	"handle": "3d-fireworks-bulb",
	"status": "active",
	"product_type": "Lighting",
	"vendor": "Aqreno",
	"image": {"src": "https://cdn.shopify.com/x.jpg"},
	"variants": [
		{
			"id": 44492394889304,
			"title": "Default Title",
			"sku": "VM-1",
			"barcode": "8907288772696",
			"price": "1999.00",
			"inventory_item_id": 4488,
			"inventory_quantity": 12,
			"option1": "Default Title",
		}
	],
}

SECOND = {
	"id": "111",
	"title": "Retro Gel Pens",
	"handle": "retro-gel-pens",
	"status": "draft",
	"product_type": "Stationery",
	"image": None,
	"variants": [
		{"id": "222", "title": "Blue", "sku": "PEN-B", "price": 100, "inventory_quantity": 3},
		{"id": "333", "title": "Red", "sku": "PEN-R", "price": 100, "inventory_quantity": 0},
	],
}


def test_normalize_product_ids_are_strings_and_image_flattened():
	product = normalize_product(RAW)
	assert product["product_id"] == "8907288772696"
	assert product["image"] == "https://cdn.shopify.com/x.jpg"
	assert product["variant_count"] == 1
	assert product["skus"] == ["VM-1"]
	assert product["inventory_total"] == 12


def test_normalize_variant_keeps_ids_and_price():
	variant = normalize_product(SECOND)["variants"][1]
	assert variant["variant_id"] == "333"
	assert variant["sku"] == "PEN-R"
	assert variant["price"] == 100.0
	assert variant["inventory_quantity"] == 0


def test_normalize_handles_missing_variants():
	product = normalize_product({"id": 5, "title": "Empty"})
	assert product["variant_count"] == 0
	assert product["skus"] == []
	assert product["status"] == "active"


def test_search_products_matches_title_sku_and_barcode():
	products = [normalize_product(RAW), normalize_product(SECOND)]
	assert [row["product_id"] for row in search_products(products, "fireworks")] == ["8907288772696"]
	assert [row["product_id"] for row in search_products(products, "pen-r")] == ["111"]
	assert [row["product_id"] for row in search_products(products, "8907288772696")] == ["8907288772696"]
	assert [row["product_id"] for row in search_products(products, "stationery")] == ["111"]
	assert len(search_products(products, "  ")) == 2


def test_search_variants_keeps_product_context():
	products = [normalize_product(RAW), normalize_product(SECOND)]
	rows = search_variants(products, "blue")
	assert len(rows) == 1
	assert rows[0]["variant_id"] == "222"
	assert rows[0]["product_title"] == "Retro Gel Pens"
	assert rows[0]["product_id"] == "111"


def test_variant_rows_flattens_every_variant():
	rows = variant_rows([normalize_product(SECOND)])
	assert [row["variant_id"] for row in rows] == ["222", "333"]


def test_sort_products_is_case_insensitive_by_title():
	products = [normalize_product(SECOND), normalize_product(RAW)]
	assert [row["product_id"] for row in sort_products(products)] == ["8907288772696", "111"]


def test_clamp_reports_the_full_match_count():
	rows, total = clamp(list(range(10)), 4)
	assert rows == [0, 1, 2, 3]
	assert total == 10
	rows, total = clamp(list(range(3)), None)
	assert rows == [0, 1, 2]
	assert total == 3
