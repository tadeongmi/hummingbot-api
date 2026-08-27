"""Derive connectors must have a working ticker adapter.

Before this adapter existed, derive_perpetual fell through to the generic ticker path,
which calls the connector's ``get_all_pairs_prices()``. That method wraps every result as
``{"symbol": {"instrument_name": ..., "best_bid": ..., "best_ask": ...}}`` — the symbol
field is a dict and the prices are nested inside it — so the generic flat-key extractor
found no price in any row, dropped them all before symbol mapping, and every fetch failed
with the misleading "none of 12 ticker rows matched the symbol map ... payload
symbols=[]". No Derive asset could ever be priced, which is the root cause behind the
empty-portfolio bug for accounts holding non-USDC collateral.

Run with: pytest test/test_ticker_sources_derive.py -v
"""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("hummingbot")

from services.ticker_sources import (  # noqa: E402
    TICKER_ADAPTERS,
    TickerFetchError,
    _derive_ticker_extract,
)


def _derive_get_ticker_result(instrument: str, bid: str, ask: str, mark: str) -> dict:
    """The shape of a real ``public/get_ticker`` response, numbers as strings."""
    return {
        "result": {
            "instrument_name": instrument,
            "best_bid_price": bid,
            "best_ask_price": ask,
            "mark_price": mark,
            "index_price": mark,
        }
    }


class TestAdapterRegistration:
    def test_both_derive_connectors_have_an_adapter(self):
        # The registration is the fix: without it, both fall to the generic path that
        # cannot parse get_all_pairs_prices' nested rows.
        assert "derive_perpetual" in TICKER_ADAPTERS
        assert "derive" in TICKER_ADAPTERS


class TestExtract:
    def test_mid_from_string_bid_ask(self):
        symbol, price, base_vol, quote_vol = _derive_ticker_extract(
            _derive_get_ticker_result("ETH-PERP", "2493.38", "2495.64", "2494.79")["result"]
        )
        assert symbol == "ETH-PERP"
        assert price == (Decimal("2493.38") + Decimal("2495.64")) / 2
        assert base_vol is None and quote_vol is None

    def test_falls_back_to_mark_price_when_book_is_empty(self):
        row = _derive_get_ticker_result("CC-PERP", "0", "0", "0.1167")["result"]
        _, price, _, _ = _derive_ticker_extract(row)
        assert price == Decimal("0.1167")


def _connector_with(payloads: dict) -> MagicMock:
    connector = MagicMock()

    async def api_post(path_url=None, data=None, **kwargs):
        instrument = data["instrument_name"]
        result = payloads[instrument]
        if isinstance(result, Exception):
            raise result
        return result

    connector._api_post = AsyncMock(side_effect=api_post)
    return connector


class TestDeriveAdapter:
    def test_prices_land_under_hb_pairs(self):
        symbol_map = {"ETH-PERP": "ETH-USDC", "CC-PERP": "CC-USDC"}
        connector = _connector_with({
            "ETH-PERP": _derive_get_ticker_result("ETH-PERP", "2493.38", "2495.64", "2494.79"),
            "CC-PERP": _derive_get_ticker_result("CC-PERP", "0.1165", "0.1167", "0.1166"),
        })

        tickers = asyncio.run(TICKER_ADAPTERS["derive_perpetual"](connector, symbol_map))

        assert set(tickers) == {"ETH-USDC", "CC-USDC"}
        assert tickers["ETH-USDC"].price == (Decimal("2493.38") + Decimal("2495.64")) / 2
        assert tickers["CC-USDC"].price == (Decimal("0.1165") + Decimal("0.1167")) / 2

    def test_one_failed_instrument_does_not_kill_the_batch(self):
        symbol_map = {"ETH-PERP": "ETH-USDC", "CC-PERP": "CC-USDC"}
        connector = _connector_with({
            "ETH-PERP": _derive_get_ticker_result("ETH-PERP", "2493.38", "2495.64", "2494.79"),
            "CC-PERP": RuntimeError("simulated request failure"),
        })

        tickers = asyncio.run(TICKER_ADAPTERS["derive_perpetual"](connector, symbol_map))

        assert set(tickers) == {"ETH-USDC"}

    def test_all_instruments_failing_raises_not_empty(self):
        # An empty result must be loud: silently returning {} would look like an empty 200
        # with no clue as to why (the exact failure mode this replaces).
        symbol_map = {"ETH-PERP": "ETH-USDC"}
        connector = _connector_with({"ETH-PERP": RuntimeError("down")})

        with pytest.raises(TickerFetchError):
            asyncio.run(TICKER_ADAPTERS["derive_perpetual"](connector, symbol_map))

    def test_request_goes_through_the_connectors_throttled_post(self):
        symbol_map = {"ETH-PERP": "ETH-USDC"}
        connector = _connector_with({
            "ETH-PERP": _derive_get_ticker_result("ETH-PERP", "2493.38", "2495.64", "2494.79"),
        })

        asyncio.run(TICKER_ADAPTERS["derive_perpetual"](connector, symbol_map))

        call = connector._api_post.call_args
        assert call.kwargs["path_url"] == "/public/get_ticker"
        assert call.kwargs["data"] == {"instrument_name": "ETH-PERP"}
