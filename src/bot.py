"""Interactive Telegram bot — the mobile interface to the tracker.

The Streamlit dashboard needs a laptop and an SSH tunnel; this bot is what you
query from your phone. It long-polls the Telegram API (no inbound port, no
public webhook) and answers a handful of read-only commands plus the drill-down
buttons attached to consensus alerts.

Design notes:
- **Authorization.** The bot replies only to chat IDs in ``TELEGRAM_CHAT_ID``
  (comma-separated). Anyone else who finds the bot is ignored — the token alone
  must not grant access to your positions.
- **Reuse.** Every command reads through the same ``src.db`` functions the
  dashboard uses; the bot adds formatting, not new analytics.
- **Pure formatters.** ``format_*`` take plain data and return HTML strings, so
  they are unit-tested without touching Telegram or the database.
"""
import logging
import os
import time
from decimal import Decimal

from src import telegram_api as tg
from src.db import (
    find_contracts_for_symbol,
    get_all_wallets,
    get_latest_scores,
    get_pnl_coverage,
    get_pnl_history,
    get_recent_token_accumulations,
    get_token_net_holders,
)

log = logging.getLogger(__name__)

COMMANDS = [
    ("top", "Best-performing watched wallets"),
    ("wallet", "A wallet's PnL & positions — /wallet <name>"),
    ("token", "Who holds a token — /token <symbol>"),
    ("movers", "Recent consensus accumulations"),
    ("help", "Show all commands"),
]


# ── Authorization ──────────────────────────────────────────────────────────────

def allowed_chat_ids() -> set[str]:
    raw = os.getenv("TELEGRAM_CHAT_ID", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def is_authorized(sender_id, allowed: set[str] | None = None) -> bool:
    allowed = allowed_chat_ids() if allowed is None else allowed
    return str(sender_id) in allowed


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _fmt_usd(x) -> str:
    if x is None:
        return "—"
    x = float(x)
    sign = "-" if x < 0 else ""
    a = abs(x)
    if a >= 1e9:
        body = f"{a / 1e9:.2f}B"
    elif a >= 1e6:
        body = f"{a / 1e6:.2f}M"
    elif a >= 1e3:
        body = f"{a / 1e3:.1f}K"
    else:
        body = f"{a:.0f}"
    return f"{sign}${body}"


def _fmt_pct(x) -> str:
    return "—" if x is None else f"{float(x) * 100:.0f}%"


def _fmt_amount(x) -> str:
    x = float(x)
    a = abs(x)
    if a >= 1e6:
        return f"{x / 1e6:.2f}M"
    if a >= 1e3:
        return f"{x / 1e3:.1f}K"
    return f"{x:,.2f}"


def _pnl_emoji(x) -> str:
    if x is None or float(x) == 0:
        return "➖"
    return "🟢" if float(x) > 0 else "🔴"


def _short(contract: str) -> str:
    return contract[:6] + "…" + contract[-4:]


def _label_map(wallets: list[dict]) -> dict:
    return {w["address"].lower(): w["label"] for w in wallets}


# ── Wallet resolution ──────────────────────────────────────────────────────────

def resolve_wallet(query: str, wallets: list[dict]) -> dict | None:
    """Match a /wallet argument against label (substring) or address (prefix)."""
    q = query.strip().lower()
    if not q:
        return None
    for w in wallets:  # exact address first
        if w["address"].lower() == q:
            return w
    for w in wallets:  # address prefix
        if w["address"].lower().startswith(q) and len(q) >= 4:
            return w
    for w in wallets:  # label substring
        if q in w["label"].lower():
            return w
    return None


# ── Formatters (pure) ──────────────────────────────────────────────────────────

def format_help() -> str:
    lines = ["<b>🐳 Smart Money Bot</b>", "Read-only access to your wallet tracker.", ""]
    lines += [f"/{c} — {d}" for c, d in COMMANDS]
    lines += ["", "<i>Numbers are FIFO realized PnL on daily-close prices — indicative, not exact accounting.</i>"]
    return "\n".join(lines)


def format_top(scores: list[dict], labels: dict, limit: int = 10) -> str:
    if not scores:
        return "No scores yet — run the daily job first."
    lines = ["<b>🏆 Top wallets</b> <i>(by composite score)</i>", ""]
    for i, s in enumerate(scores[:limit], 1):
        label = labels.get(s["wallet_address"].lower(), s["wallet_address"][:10] + "…")
        pnl = s.get("realized_pnl")
        lines.append(
            f"{i}. <b>{label}</b>  {_pnl_emoji(pnl)} {_fmt_usd(pnl)}\n"
            f"     win {_fmt_pct(s.get('win_rate'))} · {s.get('trade_count', 0)} trades"
        )
    return "\n".join(lines)


def format_wallet(wallet: dict, history: list[dict], coverage: dict,
                  score: dict | None) -> str:
    label = wallet["label"]
    total = sum((h["realized_pnl"] for h in history), Decimal("0"))
    lines = [f"<b>{label}</b>", f"<code>{_short(wallet['address'])}</code>", ""]
    lines.append(f"Realized PnL: {_pnl_emoji(total)} <b>{_fmt_usd(total)}</b>")
    if score:
        lines.append(f"Win rate: {_fmt_pct(score.get('win_rate'))} · {score.get('trade_count', 0)} closed trades")
    cov = coverage.get("coverage")
    if cov is not None:
        lines.append(f"Coverage: {_fmt_pct(cov)} <i>(share of sells with known cost basis)</i>")
    if history:
        ranked = sorted(history, key=lambda h: float(h["realized_pnl"]), reverse=True)
        winners = [h for h in ranked if float(h["realized_pnl"]) > 0][:3]
        losers = [h for h in ranked if float(h["realized_pnl"]) < 0][-3:]
        if winners:
            lines += ["", "<b>Top wins</b>"]
            lines += [f"  🟢 ${h['symbol']}  {_fmt_usd(h['realized_pnl'])}" for h in winners]
        if losers:
            lines += ["", "<b>Top losses</b>"]
            lines += [f"  🔴 ${h['symbol']}  {_fmt_usd(h['realized_pnl'])}" for h in losers]
    else:
        lines += ["", "<i>No closed trades in the 12-month window.</i>"]
    return "\n".join(lines)


def format_token(symbol: str, contract: str, holders: list[dict]) -> str:
    if not holders:
        return f"No watched wallet holds <b>${symbol}</b> right now."
    lines = [f"<b>${symbol}</b> — held by {len(holders)} watched wallet(s)",
             f"<code>{_short(contract)}</code>", ""]
    for h in holders:
        name = h.get("label") or (h["wallet_address"][:10] + "…")
        lines.append(f"  • <b>{name}</b> — net {_fmt_amount(h['net_amount'])}")
    return "\n".join(lines)


def format_movers(accumulations: list[dict], labels: dict, hours: int) -> str:
    if not accumulations:
        return f"No consensus accumulation in the last {hours}h."
    lines = [f"<b>📈 Movers</b> <i>(≥2 wallets, last {hours}h)</i>", ""]
    for acc in accumulations:
        symbol = acc.get("symbol") or "?"
        addrs = [a for a in (acc.get("wallets") or "").split(",") if a]
        names = ", ".join(labels.get(a.lower(), a[:8] + "…") for a in addrs[:4])
        more = f" +{len(addrs) - 4}" if len(addrs) > 4 else ""
        lines.append(f"<b>${symbol}</b> — {acc.get('wallet_count', len(addrs))} wallets\n     {names}{more}")
    return "\n".join(lines)


# ── Command handlers ───────────────────────────────────────────────────────────

def _alert_buttons_for(contract: str) -> dict:
    return tg.inline_keyboard([[("📊 Holders", f"tok:{contract}"), ("🏆 Top", "top")]])


def cmd_top(chat_id, args) -> None:
    wallets = get_all_wallets()
    tg.send_message(chat_id, format_top(get_latest_scores(), _label_map(wallets)))


def cmd_wallet(chat_id, args) -> None:
    wallets = get_all_wallets()
    if not args:
        tg.send_message(chat_id, "Usage: <code>/wallet &lt;name or address&gt;</code>")
        return
    w = resolve_wallet(" ".join(args), wallets)
    if not w:
        tg.send_message(chat_id, f"No watched wallet matches “{' '.join(args)}”. Try /top for names.")
        return
    addr = w["address"]
    score = next((s for s in get_latest_scores() if s["wallet_address"].lower() == addr.lower()), None)
    tg.send_message(chat_id, format_wallet(w, get_pnl_history(addr), get_pnl_coverage(addr), score))


def _send_token_view(chat_id, contract: str, symbol: str | None = None) -> None:
    holders = get_token_net_holders(contract)
    if symbol is None:
        symbol = next((h.get("symbol") for h in holders if h.get("symbol")), "?")
    tg.send_message(chat_id, format_token(symbol, contract, holders))


def cmd_token(chat_id, args) -> None:
    if not args:
        tg.send_message(chat_id, "Usage: <code>/token &lt;symbol&gt;</code>  e.g. /token PEPE")
        return
    symbol = args[0].lstrip("$")
    contracts = find_contracts_for_symbol(symbol)
    if not contracts:
        tg.send_message(chat_id, f"No transfers of <b>${symbol}</b> among watched wallets.")
        return
    _send_token_view(chat_id, contracts[0]["contract"], symbol)


def cmd_movers(chat_id, args) -> None:
    hours = 48
    wallets = get_all_wallets()
    accs = get_recent_token_accumulations(hours=hours)
    tg.send_message(chat_id, format_movers(accs, _label_map(wallets), hours))


def cmd_help(chat_id, args) -> None:
    tg.send_message(chat_id, format_help())


HANDLERS = {
    "start": cmd_help,
    "help": cmd_help,
    "top": cmd_top,
    "wallet": cmd_wallet,
    "token": cmd_token,
    "movers": cmd_movers,
}


# ── Routing ────────────────────────────────────────────────────────────────────

def parse_command(text: str) -> tuple[str | None, list[str]]:
    if not text or not text.startswith("/"):
        return None, []
    parts = text.strip().split()
    cmd = parts[0][1:].split("@")[0].lower()  # strip '/' and '@botname'
    return cmd, parts[1:]


def handle_message(msg: dict, allowed: set[str]) -> None:
    sender = (msg.get("from") or {}).get("id") or (msg.get("chat") or {}).get("id")
    if not is_authorized(sender, allowed):
        log.warning("Ignoring message from unauthorized id=%s", sender)
        return
    chat_id = msg["chat"]["id"]
    cmd, args = parse_command(msg.get("text", ""))
    handler = HANDLERS.get(cmd)
    if handler is None:
        tg.send_message(chat_id, "Unknown command. /help for the list.")
        return
    handler(chat_id, args)


def handle_callback(cb: dict, allowed: set[str]) -> None:
    sender = (cb.get("from") or {}).get("id")
    cb_id = cb.get("id")
    if not is_authorized(sender, allowed):
        tg.answer_callback_query(cb_id, "Not authorized")
        return
    chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
    data = cb.get("data", "")
    tg.answer_callback_query(cb_id)
    if data == "top":
        cmd_top(chat_id, [])
    elif data.startswith("tok:"):
        _send_token_view(chat_id, data[4:])


def handle_update(update: dict, allowed: set[str]) -> None:
    try:
        if "message" in update:
            handle_message(update["message"], allowed)
        elif "callback_query" in update:
            handle_callback(update["callback_query"], allowed)
    except Exception as e:  # one bad update must never kill the loop
        log.exception("Error handling update: %s", e)


# ── Main loop ──────────────────────────────────────────────────────────────────

def run_bot() -> None:
    allowed = allowed_chat_ids()
    if not allowed:
        raise SystemExit("TELEGRAM_CHAT_ID is empty — refusing to start an unauthenticated bot.")
    me = tg.get_me()
    if not me.get("ok"):
        raise SystemExit("Telegram getMe failed — check TELEGRAM_BOT_TOKEN.")
    tg.set_my_commands(COMMANDS)
    log.info("Bot @%s started; authorized chats: %s", me["result"].get("username"), allowed)

    offset = None
    while True:
        try:
            updates = tg.get_updates(offset=offset, timeout=30)
        except Exception as e:
            log.error("get_updates failed: %s; backing off", e)
            time.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            handle_update(update, allowed)
