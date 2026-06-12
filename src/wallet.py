"""Wallet analysis — holdings, recent transactions, basic accumulation signals."""
import pandas as pd
from datetime import datetime
import re
from src.etherscan import get_eth_balance, get_transactions, get_token_transfers
from src.prices import get_prices_by_contract, eth_price_usd

# Phishing / spam token name patterns (URLs, "claim", "reward", etc.)
_SPAM_PATTERNS = re.compile(
    r"(https?://|www\.|\.com|\.xyz|\.io\b|\.org|\.net|\.app|\.fi\b|\.vip|\.cc\b|"
    r"\.top\b|t\.me|claim|reward|bonus|airdrop|voucher|giveaway|redeem|visit |access )",
    re.IGNORECASE,
)


def is_spam_token(symbol: str, name: str) -> bool:
    blob = f"{symbol} {name}"
    return bool(_SPAM_PATTERNS.search(blob))


def load_watchlist(path: str = "watchlist.txt") -> list[dict]:
    wallets = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",", 1)
            address = parts[0].strip()
            label = parts[1].strip() if len(parts) > 1 else address[:8] + "..."
            wallets.append({"address": address, "label": label})
    return wallets


def analyze_wallet(address: str) -> dict:
    eth_balance = get_eth_balance(address)
    txns = get_transactions(address, limit=50)
    token_txns = get_token_transfers(address, limit=50)

    token_df = _parse_token_txns(token_txns, address)
    holdings = token_holdings_summary(token_df)

    eth_usd = eth_price_usd()
    eth_value_usd = eth_balance * eth_usd

    return {
        "eth_balance": eth_balance,
        "eth_usd": eth_usd,
        "eth_value_usd": eth_value_usd,
        "transactions": _parse_txns(txns, address),
        "token_transfers": token_df,
        "holdings": holdings,
    }


def _parse_txns(raw: list[dict], address: str) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame()
    rows = []
    addr_lower = address.lower()
    for tx in raw:
        value_eth = int(tx.get("value", 0)) / 1e18
        direction = "IN" if tx.get("to", "").lower() == addr_lower else "OUT"
        rows.append({
            "hash": tx.get("hash", "")[:10] + "...",
            "timestamp": datetime.fromtimestamp(int(tx.get("timeStamp", 0))),
            "direction": direction,
            "value_eth": round(value_eth, 4),
            "from": tx.get("from", "")[:10] + "...",
            "to": tx.get("to", "")[:10] + "...",
        })
    return pd.DataFrame(rows)


def _parse_token_txns(raw: list[dict], address: str) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame()
    rows = []
    addr_lower = address.lower()
    for tx in raw:
        decimals = int(tx.get("tokenDecimal", 18) or 18)
        value = int(tx.get("value", 0)) / (10 ** decimals)
        direction = "IN" if tx.get("to", "").lower() == addr_lower else "OUT"
        rows.append({
            "timestamp": datetime.fromtimestamp(int(tx.get("timeStamp", 0))),
            "token": tx.get("tokenSymbol", "?"),
            "token_name": tx.get("tokenName", ""),
            "contract": tx.get("contractAddress", "").lower(),
            "direction": direction,
            "value": round(value, 4),
            "from": tx.get("from", "")[:10] + "...",
            "to": tx.get("to", "")[:10] + "...",
        })
    return pd.DataFrame(rows)


def token_holdings_summary(token_df: pd.DataFrame) -> pd.DataFrame:
    # net position per token across the last 50 transfers; not realized PnL
    if token_df.empty:
        return pd.DataFrame()
    df = token_df.copy()
    df["signed_value"] = df.apply(
        lambda r: r["value"] if r["direction"] == "IN" else -r["value"], axis=1
    )
    # Group by contract address (guards against symbol spoofing; spam clones legitimate symbols)
    summary = (
        df.groupby(["contract", "token", "token_name"])["signed_value"]
        .sum()
        .reset_index()
        .rename(columns={"signed_value": "net_flow"})
    )

    # USD price by contract (spam tokens absent from CoinGecko, price = 0)
    prices = get_prices_by_contract(summary["contract"].tolist())
    summary["price_usd"] = summary["contract"].map(prices).fillna(0.0)
    summary["value_usd"] = (summary["net_flow"] * summary["price_usd"]).round(2)

    # Spam flag: phishing name pattern OR unpriced (not listed on CoinGecko)
    summary["is_spam"] = summary.apply(
        lambda r: is_spam_token(r["token"], r["token_name"]) or r["price_usd"] == 0.0,
        axis=1,
    )

    summary["signal"] = summary["net_flow"].apply(
        lambda x: "🟢 Accumulating" if x > 0 else ("🔴 Distributing" if x < 0 else "⚪ Neutral")
    )
    return summary.sort_values(["is_spam", "value_usd"], ascending=[True, False])


def aggregate_view(wallets: list[dict]) -> pd.DataFrame:
    """Aggregate holdings across the watchlist — how many wallets hold each token."""
    rows = []
    spam_filtered = 0
    for w in wallets:
        data = analyze_wallet(w["address"])
        h = data["holdings"]
        if h.empty:
            continue
        for _, row in h.iterrows():
            if row.get("is_spam", True):
                spam_filtered += 1
                continue
            rows.append({
                "wallet": w["label"],
                "token": row["token"],
                "token_name": row["token_name"],
                "net_flow": row["net_flow"],
                "value_usd": row.get("value_usd", 0.0),
                "signal": row["signal"],
            })

    if not rows:
        return pd.DataFrame(), spam_filtered

    df = pd.DataFrame(rows)
    acc = df[df["signal"] == "🟢 Accumulating"]
    if acc.empty:
        return pd.DataFrame(), spam_filtered

    agg = (
        acc.groupby(["token", "token_name"])
        .agg(
            wallet_count=("wallet", "nunique"),
            total_value_usd=("value_usd", "sum"),
            wallets=("wallet", lambda x: ", ".join(sorted(set(x)))),
        )
        .reset_index()
        # sort by USD value first, wallet count as tiebreaker
        .sort_values(["total_value_usd", "wallet_count"], ascending=[False, False])
    )
    return agg, spam_filtered
