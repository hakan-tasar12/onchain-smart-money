"""Wallet scoring: win rate (35%), realized PnL (30%), early entry (20%), diversity (15%).
Scores are percentile ranks within the current watchlist.
"""
import logging

from src.db import get_pnl_history, upsert_score

log = logging.getLogger(__name__)

MIN_TRADES = 10  # composite is None below this trade count


def _percentile_rank(value: float, all_values: list[float]) -> float:
    if not all_values:
        return 0.5
    return sum(v <= value for v in all_values) / len(all_values)


def _compute_metrics(wallet_address: str) -> dict | None:
    # Returns None if the wallet has fewer than MIN_TRADES closed trades.
    history = get_pnl_history(wallet_address)
    if len(history) < MIN_TRADES:
        return None

    winners = [h for h in history if h["realized_pnl"] > 0]
    win_rate = len(winners) / len(history)
    total_pnl = sum(h["realized_pnl"] for h in history)

    winner_contracts = {h["contract"] for h in history if h["realized_pnl"] > 0}
    all_contracts = {h["contract"] for h in history}
    diversity = len(winner_contracts) / len(all_contracts) if all_contracts else 0.0

    # fraction of winning trades held under 30 days; falls back to close_ts if buy time is missing
    thirty_days = 30 * 24 * 3600
    from src.db import get_conn
    early_wins = 0
    for h in winners:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT MIN(acquired_ts) FROM pnl_lots WHERE wallet_address = ? AND contract = ?",
                (wallet_address, h["contract"]),
            ).fetchone()
        first_acquired = row[0] if row and row[0] else h["close_ts"]
        hold_duration = h["close_ts"] - first_acquired
        if 0 < hold_duration <= thirty_days:
            early_wins += 1

    early_entry = early_wins / len(winners) if winners else 0.0

    return {
        "win_rate": win_rate,
        "realized_pnl": total_pnl,
        "early_entry": early_entry,
        "diversity": diversity,
        "trade_count": len(history),
    }


def run_scoring(watchlist_path: str = "watchlist.txt") -> None:
    from src.wallet import load_watchlist
    wallets = load_watchlist(watchlist_path)

    wallet_metrics: dict[str, dict] = {}
    for w in wallets:
        addr = w["address"].lower()
        m = _compute_metrics(addr)
        if m:
            wallet_metrics[addr] = m
        else:
            trade_count = len(get_pnl_history(addr))
            log.info(f"{w['label']}: {trade_count}/{MIN_TRADES} trades — score skipped")
            upsert_score(addr, None, None, None, None, None, trade_count)

    if not wallet_metrics:
        log.info("No wallets with sufficient trade history")
        return

    all_win_rates  = [m["win_rate"]    for m in wallet_metrics.values()]
    all_pnls       = [m["realized_pnl"] for m in wallet_metrics.values()]
    all_early      = [m["early_entry"]  for m in wallet_metrics.values()]
    all_diversity  = [m["diversity"]    for m in wallet_metrics.values()]

    for addr, m in wallet_metrics.items():
        composite = (
            _percentile_rank(m["win_rate"],    all_win_rates)  * 35 +
            _percentile_rank(m["realized_pnl"], all_pnls)      * 30 +
            _percentile_rank(m["early_entry"],  all_early)     * 20 +
            _percentile_rank(m["diversity"],    all_diversity) * 15
        )
        upsert_score(
            wallet_address=addr,
            win_rate=m["win_rate"],
            realized_pnl=m["realized_pnl"],
            early_entry=m["early_entry"],
            diversity=m["diversity"],
            composite=composite,
            trade_count=m["trade_count"],
        )
        log.info(f"{addr[:10]}...: composite={composite:.1f} ({m['trade_count']} trades)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [scoring] %(message)s")
    run_scoring()
