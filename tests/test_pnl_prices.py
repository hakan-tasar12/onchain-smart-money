"""Unit tests for the PnL pricing layer (src/pnl.py) — no network.

These cover the lookup and caching logic added when the per-day /history endpoint
was replaced with a single market_chart/range series fetch, plus the per-wallet
time budget. The network call itself (_fetch_price_series) is not exercised here;
the point is that lookups, caching, and the deadline gate behave deterministically.
"""
import time
from unittest.mock import MagicMock, patch

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


# ── CoinGecko 401 (free-tier range wall) ─────────────────────────────────────

def test_fetch_price_series_401_returns_none_without_retry():
    """HTTP 401 from CoinGecko on a valid (recent) range must return None
    (transient — don't negative-cache) and must NOT retry.
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    now = int(time.time())

    with patch("src.pnl.requests.get", return_value=mock_resp) as mock_get, \
         patch("src.pnl._throttle"):
        # Use a recent window so the clamp doesn't trigger the from>=to early exit
        result = pnl._fetch_price_series("dai", now - 30 * 86400, now)

    assert result is None
    assert mock_get.call_count == 1  # no retries


def test_fetch_price_series_429_retries():
    """HTTP 429 (rate limit) must retry up to 4 times, unlike 401."""
    mock_resp_429 = MagicMock()
    mock_resp_429.status_code = 429
    mock_resp_ok = MagicMock()
    mock_resp_ok.status_code = 200
    mock_resp_ok.json.return_value = {"prices": [[1620518400000, 3950.0]]}

    with patch("src.pnl.requests.get", side_effect=[mock_resp_429, mock_resp_ok]) as mock_get, \
         patch("src.pnl._throttle"), \
         patch("src.pnl.time.sleep"):
        result = pnl._fetch_price_series("weth", 0, 9999999999)

    assert result == {"2021-05-09": 3950.0}
    assert mock_get.call_count == 2  # one 429 retry, then success


# ── from_ts clamp (365-day wall) ─────────────────────────────────────────────

def test_fetch_price_series_clamps_from_ts_to_wall():
    """A from_ts older than 365d - safety margin must be clamped before the
    request so we never send a guaranteed-401 range to CoinGecko.
    """
    captured = {}
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"prices": []}

    def capture_get(url, params=None, timeout=None):
        captured["from"] = params["from"]
        return mock_resp

    very_old_ts = int(time.time()) - pnl.TWELVE_MONTHS_SECONDS - 10 * 86400  # 10 days beyond wall

    with patch("src.pnl.requests.get", side_effect=capture_get), \
         patch("src.pnl._throttle"):
        pnl._fetch_price_series("weth", very_old_ts, int(time.time()))

    wall = int(time.time()) - pnl.TWELVE_MONTHS_SECONDS + pnl._FREE_TIER_SAFETY
    assert captured["from"] >= wall - 5  # within 5s of wall (time.time() drift)


def test_fetch_price_series_from_ge_to_returns_empty_no_network():
    """If the entire activity window is older than the wall (from_ts >= to_ts after
    clamp), skip the network call and return {} — not None, since the coin itself
    may be valid; we just have no usable price window.
    """
    now = int(time.time())
    # Both timestamps are very old — after clamping, from will exceed to.
    very_old = now - pnl.TWELVE_MONTHS_SECONDS - 20 * 86400

    with patch("src.pnl.requests.get") as mock_get, \
         patch("src.pnl._throttle"):
        result = pnl._fetch_price_series("weth", very_old, very_old + 1)

    assert result == {}
    mock_get.assert_not_called()  # no network request fired
