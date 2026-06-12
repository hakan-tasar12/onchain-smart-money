"""Etherscan API wrapper — rate-limit aware, cached."""
import os
import time
import json
import hashlib
import requests
from pathlib import Path

BASE_URL = "https://api.etherscan.io/v2/api"
CHAIN_ID = 1  # Ethereum mainnet
CACHE_DIR = Path(".cache/etherscan")
CACHE_TTL = 300  # 5 minutes


def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{hashlib.md5(key.encode()).hexdigest()}.json"


def _cached_get(params: dict) -> dict:
    key = json.dumps(params, sort_keys=True)
    path = _cache_path(key)
    if path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL:
        return json.loads(path.read_text())
    api_key = os.getenv("ETHERSCAN_API_KEY", "")
    response = requests.get(BASE_URL, params={**params, "chainid": CHAIN_ID, "apikey": api_key}, timeout=10)
    response.raise_for_status()
    data = response.json()
    path.write_text(json.dumps(data))
    time.sleep(0.25)  # free tier: 5 req/s
    return data


def get_eth_balance(address: str) -> float:
    """Fetch balance in Wei and convert to ETH."""
    data = _cached_get({
        "module": "account",
        "action": "balance",
        "address": address,
        "tag": "latest",
    })
    if data.get("status") != "1":
        return 0.0
    return int(data["result"]) / 1e18


def get_transactions(address: str, limit: int = 20) -> list[dict]:
    """Fetch the last N normal ETH transactions."""
    data = _cached_get({
        "module": "account",
        "action": "txlist",
        "address": address,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": limit,
        "sort": "desc",
    })
    if data.get("status") != "1":
        return []
    return data.get("result", [])


def get_token_transfers(address: str, limit: int = 20) -> list[dict]:
    """Fetch the last N ERC-20 token transfers."""
    data = _cached_get({
        "module": "account",
        "action": "tokentx",
        "address": address,
        "page": 1,
        "offset": limit,
        "sort": "desc",
    })
    if data.get("status") != "1":
        return []
    return data.get("result", [])


def _paginate_since(action: str, address: str, since_ts: int,
                    page_size: int = 1000, max_pages: int = 12) -> list[dict]:
    """
    Paginate a time window (since_ts to now) from Etherscan.
    sort=desc (newest first); stops when the oldest record pre-dates since_ts.
    A single page (1000 rows) typically covers 12 months; max_pages is a defensive cap (~12k txs).
    """
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        data = _cached_get({
            "module": "account",
            "action": action,
            "address": address,
            "page": page,
            "offset": page_size,
            "sort": "desc",
        })
        if data.get("status") != "1":
            break
        results = data.get("result", [])
        if not results:
            break
        out.extend(results)
        oldest_ts = int(results[-1].get("timeStamp", 0))
        if len(results) < page_size or oldest_ts < since_ts:
            break
    # Trim to the requested window
    return [t for t in out if int(t.get("timeStamp", 0)) >= since_ts]


def get_token_transfers_since(address: str, since_ts: int) -> list[dict]:
    """All ERC-20 token transfers from since_ts to now (paginated)."""
    return _paginate_since("tokentx", address, since_ts)


def get_transactions_since(address: str, since_ts: int) -> list[dict]:
    """All normal ETH transactions from since_ts to now (paginated)."""
    return _paginate_since("txlist", address, since_ts)
