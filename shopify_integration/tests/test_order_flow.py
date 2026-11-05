"""Regression tests for the order flow decisions that cost money or stock.

They run under `bench run-tests` (they need the `frappe` package on the path)
and are skipped by a plain `pytest` on a machine without a bench.
"""

import pytest

pytest.importorskip("frappe")

from shopify_integration import order as order_module


class FakeStore:
	last_order_sync_on = "2026-11-01 10:00:00"


class FakeCfg:
	name = "SHOP-TEST"
	enabled = True
	pull_enabled = True
	pull_orders = True
	dry_run = False
	pull_orders_since = None
	order_sync_lookback_days = 3
	create_sales_invoice = False
	create_delivery_note = False
	store = FakeStore()

	def is_historical(self, order):
		return True


class FakeRun:
	def __init__(self):
		self.counters = {}
		self.budget = None
		self.budget_exhausted = 0
		self.notes = []
		self.failures = []

	def bump(self, key, amount=1):
		pass

	def fail(self, *args, **kwargs):
		self.failures.append(args)

	def note(self, text):
		self.notes.append(text)


def test_existing_order_is_refreshed_even_when_it_predates_the_cutoff(monkeypatch):
	"""The cutoff must never freeze an order that already exists."""
	refreshed = []
	monkeypatch.setattr(
		order_module, "find_sales_order", lambda cfg, order_id, number=None: object()
	)
	monkeypatch.setattr(
		order_module,
		"_refresh_existing_safe",
		lambda cfg, so, order, run=None: refreshed.append(order),
	)
	monkeypatch.setattr(order_module.retry, "resolve", lambda *a, **k: None)

	so, result = order_module.import_order(FakeCfg(), {"id": 1, "name": "VM1"})

	assert result == "exists"
	assert refreshed, "an existing order must be refreshed despite the cutoff"
	assert so is not None


def test_new_order_before_the_cutoff_is_skipped(monkeypatch):
	monkeypatch.setattr(order_module, "find_sales_order", lambda *a, **k: None)
	so, result = order_module.import_order(FakeCfg(), {"id": 2, "name": "VM2"}, run=FakeRun())

	assert so is None
	assert result == "before pull cutoff"


def test_after_import_never_cancels_a_sales_order(monkeypatch):
	"""Cancellation is owned by the store setting, checked in refresh_existing."""
	calls = []
	monkeypatch.setattr(
		order_module, "cancel_sales_order", lambda *a, **k: calls.append(a)
	)
	order_module._after_import(FakeCfg(), object(), {"id": 3, "cancelled_at": "yes"})

	assert calls == []


def test_poll_does_not_advance_the_cursor_when_it_crashes(monkeypatch):
	class BrokenClient:
		def __init__(self, *args, **kwargs):
			pass

		def paginate(self, *args, **kwargs):
			raise RuntimeError("Shopify 502")

	written = []
	monkeypatch.setattr(order_module, "ShopifyClient", BrokenClient)
	monkeypatch.setattr(
		order_module,
		"set_values",
		lambda doctype, name, values, commit=False: written.append(values),
	)
	monkeypatch.setattr(order_module, "log_error", lambda *a, **k: None)

	with pytest.raises(RuntimeError):
		order_module.poll_orders(FakeCfg(), FakeRun())

	assert not any("last_order_sync_on" in values for values in written), (
		"a crashed poll must leave the cursor where it was"
	)


def test_poll_advances_the_cursor_after_a_clean_run(monkeypatch):
	class EmptyClient:
		def __init__(self, *args, **kwargs):
			pass

		def paginate(self, *args, **kwargs):
			return iter(())

	written = []
	monkeypatch.setattr(order_module, "ShopifyClient", EmptyClient)
	monkeypatch.setattr(
		order_module,
		"set_values",
		lambda doctype, name, values, commit=False: written.append(values),
	)

	order_module.poll_orders(FakeCfg(), FakeRun())

	assert any("last_order_sync_on" in values for values in written)
