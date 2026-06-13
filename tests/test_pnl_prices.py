"""Unit tests for the PnL pricing layer (src/pnl.py) — no network.

These cover the lookup and caching logic added when the per-day /history endpoint
was replaced with a single market_chart/range series fetch, plus the per-wallet
time budget. The network call itself (_fetch_price_series) is not exercised here;
the point is that lookups, caching, and the deadline gate behave deterministically.
"""
import time

from src import pnl


def setup_function():
    # Each test starts with an empty in-process series cache.
    pnl._series_mem.clear()


# ── Day lookup ───────────────────────────────────────────────────────────────

def test_day_str_is_utc_calendar_day():
    # 2021-05-09 00:00:00 UTC
    assert pnl._day_str(1620518400) == "2021-05-09"


def test_price_on_day_returns_price_for_known_day():
    series = {"2021-05-09": 3950.0}
    assert pnl._price_on_day(series, 1620518400) == 3950.0


def test_price_on_day_missing_day_is_none():
    assert pnl._price_on_day({}, 1620518400) is None


def test_price_on_day_nonpositive_is_none():
    # A $0 (or negative) point is never a usable price -> None -> no_price downstream.
    assert pnl._price_on_day({"2021-05-09": 0.0}, 1620518400) is None
    assert pnl._price_on_day({"2021-05-09": -1.0}, 1620518400) is None


# ── Series cache ─────────────────────────────────────────────────────────────

def test_get_price_series_serves_memory_cache_without_network():
    pnl._series_mem["weth"] = {"2021-05-09": 3950.0}
    # If this tried to hit the network it would be slow / fail offline; the mem
    # cache must short-circuit it entirely.
    out = pnl._get_price_series("weth", 0, 1, deadline=time.time() + 100)
    assert out == {"2021-05-09": 3950.0}


def test_get_price_series_past_deadline_skips_network_and_returns_empty():
    # Coin not in memory and the deadline already passed: no fetch is attempted,
    # so the wallet's un-cached coins resolve to no_price instead of stalling.
    out = pnl._get_price_series("never-fetched-coin", 0, 1, deadline=time.time() - 1)
    assert out == {}


# ── Negative caching (dead tokens) ───────────────────────────────────────────

def test_genuine_empty_is_negative_cached_and_not_refetched(tmp_path, monkeypatch):
    """A coin CoinGecko has no data for ({} from fetch) is persisted and reused.

    The second lookup must NOT hit the network — otherwise every daily run wastes
    its budget re-probing the same dead tokens.
    """
    monkeypatch.setattr(pnl, "SERIES_CACHE_DIR", tmp_path)
    calls = {"n": 0}

    def fake_fetch(coin_id, frm, to):
        calls["n"] += 1
        return {}  # genuine no-data

    monkeypatch.setattr(pnl, "_fetch_price_series", fake_fetch)

    assert pnl._get_price_series("deadcoin", 0, 1) == {}
    pnl._series_mem.clear()  # force a disk read on the next call, not the mem hit
    assert pnl._get_price_series("deadcoin", 0, 1) == {}
    assert calls["n"] == 1                       # fetched once, then served from disk
    assert (tmp_path / "deadcoin.json").exists()  # negative result persisted


def test_transient_failure_is_not_persisted(tmp_path, monkeypatch):
    """A transient fetch failure (None) must not be negative-cached as a dead token."""
    monkeypatch.setattr(pnl, "SERIES_CACHE_DIR", tmp_path)
    monkeypatch.setattr(pnl, "_fetch_price_series", lambda c, f, t: None)

    assert pnl._get_price_series("flaky", 0, 1) == {}
    assert not (tmp_path / "flaky.json").exists()  # not persisted -> retried next run


def test_real_series_is_persisted_and_served(tmp_path, monkeypatch):
    monkeypatch.setattr(pnl, "SERIES_CACHE_DIR", tmp_path)
    series = {"2021-05-09": 3950.0}
    calls = {"n": 0}

    def fake_fetch(coin_id, frm, to):
        calls["n"] += 1
        return series

    monkeypatch.setattr(pnl, "_fetch_price_series", fake_fetch)

    assert pnl._get_price_series("weth", 0, 1) == series
    pnl._series_mem.clear()
    assert pnl._get_price_series("weth", 0, 1) == series
    assert calls["n"] == 1  # served from disk the second time
