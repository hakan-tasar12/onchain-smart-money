"""CoinGecko price fetcher — USD price by ETH contract address, cached.

Tokens are priced by contract address (not symbol) to prevent symbol-spoofing.
Spam / airdrop tokens are absent from CoinGecko — no price returned — natural filter.
"""
import time
import json
import hashlib
import requests
from pathlib import Path

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
CACHE_DIR = Path(".cache/prices")
PRICE_TTL = 120        # price cache: 2 minutes
LIST_TTL = 86400       # contract-to-id map cache: 24 hours

# Hardcoded fallback so core tokens work even if the CoinGecko list endpoint is down
_FALLBACK = {
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "weth",
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "usd-coin",
    "0xdac17f958d2ee523a2206206994597c13d831ec7": "tether",
    "0x6b175474e89094c44da98b954eedeac495271d0f": "dai",
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "wrapped-bitcoin",
}


def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{hashlib.md5(key.encode()).hexdigest()}.json"


def _contract_to_id_map() -> dict[str, str]:
    """Build {eth_contract_lower: coin_id} map from the full CoinGecko coin list (24h cache)."""
    path = _cache_path("coingecko_contract_map_v1")
    if path.exists() and (time.time() - path.stat().st_mtime) < LIST_TTL:
        return json.loads(path.read_text())
    try:
        r = requests.get(
            f"{COINGECKO_BASE}/coins/list",
            params={"include_platform": "true"},
            timeout=30,
        )
        r.raise_for_status()
        mapping: dict[str, str] = {}
        for coin in r.json():
            addr = (coin.get("platforms") or {}).get("ethereum")
            if addr:
                mapping[addr.lower()] = coin["id"]
        if mapping:
            path.write_text(json.dumps(mapping))
            return mapping
    except Exception:
        pass
    return dict(_FALLBACK)


_GLOBAL_PRICE_CACHE = CACHE_DIR / "id_prices.json"


def _load_id_cache() -> dict[str, dict]:
    if _GLOBAL_PRICE_CACHE.exists():
        try:
            return json.loads(_GLOBAL_PRICE_CACHE.read_text())
        except Exception:
            return {}
    return {}


def _fetch_ids(ids: list[str]) -> dict[str, float]:
    """Batch-fetch USD prices by CoinGecko ID; retries with backoff on 429."""
    out: dict[str, float] = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        for attempt in range(3):
            try:
                r = requests.get(
                    f"{COINGECKO_BASE}/simple/price",
                    params={"ids": ",".join(chunk), "vs_currencies": "usd"},
                    timeout=15,
                )
                if r.status_code == 429:
                    time.sleep(2 * (attempt + 1))
                    continue
                r.raise_for_status()
                for coin_id, val in r.json().items():
                    out[coin_id] = val.get("usd", 0.0)
                break
            except Exception:
                time.sleep(1)
        time.sleep(0.3)
    return out


def get_prices_by_contract(contracts: list[str]) -> dict[str, float]:
    """
    List of ETH contract addresses, returns {contract_lower: USD price}.
    Contracts not found on CoinGecko (spam) are silently omitted.

    Uses a global per-coin cache so the same token (e.g. WETH) is fetched only
    once across all wallets in a multi-wallet run, avoiding rate-limit storms.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    contracts = [c.lower() for c in contracts if c]
    if not contracts:
        return {}

    cmap = _contract_to_id_map()
    contract_to_id = {c: cmap[c] for c in contracts if c in cmap}
    if not contract_to_id:
        return {}

    cache = _load_id_cache()
    now = time.time()
    needed = sorted({
        cid for cid in contract_to_id.values()
        if cid not in cache or (now - cache[cid].get("ts", 0)) > PRICE_TTL
    })

    if needed:
        fetched = _fetch_ids(needed)
        for cid in needed:
            # Keep stale cache value for failed fetches; default to 0 if never cached
            if cid in fetched:
                cache[cid] = {"usd": fetched[cid], "ts": now}
            elif cid not in cache:
                cache[cid] = {"usd": 0.0, "ts": now}
        try:
            _GLOBAL_PRICE_CACHE.write_text(json.dumps(cache))
        except Exception:
            pass

    return {
        contract: cache.get(coin_id, {}).get("usd", 0.0)
        for contract, coin_id in contract_to_id.items()
    }


def eth_price_usd() -> float:
    """ETH spot price via the WETH contract address."""
    weth = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    return get_prices_by_contract([weth]).get(weth, 0.0)
