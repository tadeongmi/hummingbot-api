"""The account update must survive a connector returning a price as a string.

Regression tests for the crash that emptied whole accounts: derive_perpetual's
``_get_last_traded_price`` is declared ``-> float`` but returns Derive's raw JSON string
(the exchange serializes every number as a string). The bare ``price > 0`` comparison in
``AccountsService._safe_get_last_traded_prices`` raised
``TypeError: '>' not supported between instances of 'str' and 'int'``, the exception
propagated out of the per-account update, and every balance in the account was discarded —
including the USDC that had priced fine without any lookup.

Run with: pytest test/test_accounts_price_coercion.py -v
"""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("hummingbot")

from services.accounts_service import AccountsService, coerce_price  # noqa: E402


class TestCoercePrice:
    def test_numeric_string_parses(self):
        # Derive's actual wire format: numbers as strings.
        assert coerce_price("2494.79") == Decimal("2494.79")

    def test_decimal_and_float_pass_through(self):
        assert coerce_price(Decimal("1.5")) == Decimal("1.5")
        assert coerce_price(2.5) == Decimal("2.5")

    def test_high_precision_string_survives(self):
        raw = "0.116559814706268527"
        assert coerce_price(raw) == Decimal(raw)

    def test_none_is_none(self):
        assert coerce_price(None) is None

    def test_garbage_string_is_none_not_a_raise(self):
        assert coerce_price("not-a-price") is None

    def test_zero_and_negative_are_none(self):
        # A zero or negative price is "no price", same contract as the old truthiness check.
        assert coerce_price(0) is None
        assert coerce_price("0") is None
        assert coerce_price("-3") is None

    def test_non_finite_is_none(self):
        assert coerce_price("NaN") is None
        assert coerce_price("Infinity") is None

    def test_empty_string_is_none(self):
        assert coerce_price("") is None


def _bare_service() -> AccountsService:
    """An AccountsService with only the state _safe_get_last_traded_prices touches."""
    service = AccountsService.__new__(AccountsService)
    service._last_known_prices = {}
    return service


class TestSafeGetLastTradedPrices:
    def test_string_price_from_derive_does_not_raise(self):
        """The historical crash: a str price must parse, not TypeError the whole update."""
        service = _bare_service()
        connector = MagicMock()
        connector._get_last_traded_price = AsyncMock(return_value="2494.79")

        prices = asyncio.run(service._safe_get_last_traded_prices(connector, ["ETH-USDC"]))

        assert prices["ETH-USDC"] == Decimal("2494.79")
        # And it must be cached for the fallback path.
        assert service._last_known_prices["ETH-USDC"] == Decimal("2494.79")

    def test_unparseable_price_falls_back_to_zero_not_none(self):
        """A bad price must yield the documented fallback (cached value or 0), never a raw
        None that crashes the caller's ``Decimal(str(...))``."""
        service = _bare_service()
        connector = MagicMock()
        connector._get_last_traded_price = AsyncMock(return_value=None)

        prices = asyncio.run(service._safe_get_last_traded_prices(connector, ["ETH-USDC"]))

        assert prices["ETH-USDC"] == Decimal("0")

    def test_unparseable_price_uses_cache_when_available(self):
        service = _bare_service()
        service._last_known_prices["ETH-USDC"] = Decimal("2400")
        connector = MagicMock()
        connector._get_last_traded_price = AsyncMock(return_value="garbage")

        prices = asyncio.run(service._safe_get_last_traded_prices(connector, ["ETH-USDC"]))

        assert prices["ETH-USDC"] == Decimal("2400")

    def test_one_bad_pair_does_not_poison_the_rest(self):
        service = _bare_service()
        connector = MagicMock()

        async def per_pair(trading_pair):
            if trading_pair == "BAD-USDC":
                return "not-a-number"
            return "10.5"

        connector._get_last_traded_price = AsyncMock(side_effect=per_pair)

        prices = asyncio.run(
            service._safe_get_last_traded_prices(connector, ["ETH-USDC", "BAD-USDC"])
        )

        assert prices["ETH-USDC"] == Decimal("10.5")
        assert prices["BAD-USDC"] == Decimal("0")
