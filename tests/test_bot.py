"""Unit tests for the Telegram bot's pure logic: auth, routing, resolution, and
formatting. No network or database — the formatters take plain data, so they are
verified in isolation.
"""
from decimal import Decimal

from src import bot


# ── Authorization ──────────────────────────────────────────────────────────────

def test_is_authorized_matches_allowed_ids():
    allowed = {"123", "456"}
    assert bot.is_authorized(123, allowed) is True       # int coerced to str
    assert bot.is_authorized("456", allowed) is True
    assert bot.is_authorized(999, allowed) is False
    assert bot.is_authorized(None, allowed) is False


def test_empty_allowlist_authorizes_nobody():
    assert bot.is_authorized(123, set()) is False


# ── Command parsing ────────────────────────────────────────────────────────────

def test_parse_command_strips_slash_botname_and_splits_args():
    assert bot.parse_command("/top") == ("top", [])
    assert bot.parse_command("/wallet Machi Big Brother") == ("wallet", ["Machi", "Big", "Brother"])
    assert bot.parse_command("/token@my_bot PEPE") == ("token", ["PEPE"])
    assert bot.parse_command("hello") == (None, [])
    assert bot.parse_command("") == (None, [])


# ── Wallet resolution ──────────────────────────────────────────────────────────

WALLETS = [
    {"address": "0xABC0000000000000000000000000000000000001", "label": "Machi Big Brother"},
    {"address": "0xDEF0000000000000000000000000000000000002", "label": "James Fickel"},
]


def test_resolve_wallet_by_label_substring_case_insensitive():
    assert bot.resolve_wallet("machi", WALLETS)["label"] == "Machi Big Brother"
    assert bot.resolve_wallet("fickel", WALLETS)["label"] == "James Fickel"


def test_resolve_wallet_by_address_prefix_and_exact():
    assert bot.resolve_wallet("0xabc0000000000000000000000000000000000001", WALLETS)["label"] == "Machi Big Brother"
    assert bot.resolve_wallet("0xdef0", WALLETS)["label"] == "James Fickel"


def test_resolve_wallet_no_match_returns_none():
    assert bot.resolve_wallet("nonexistent", WALLETS) is None
    assert bot.resolve_wallet("", WALLETS) is None


# ── Formatting helpers ─────────────────────────────────────────────────────────

def test_fmt_usd_scales_and_signs():
    assert bot._fmt_usd(0) == "$0"
    assert bot._fmt_usd(1234) == "$1.2K"
    assert bot._fmt_usd(2_500_000) == "$2.50M"
    assert bot._fmt_usd(-3_000_000_000) == "-$3.00B"
    assert bot._fmt_usd(None) == "—"
    assert bot._fmt_usd(Decimal("1500")) == "$1.5K"  # Decimal accepted


def test_pnl_emoji():
    assert bot._pnl_emoji(5) == "🟢"
    assert bot._pnl_emoji(-5) == "🔴"
    assert bot._pnl_emoji(0) == "➖"
    assert bot._pnl_emoji(None) == "➖"


# ── Formatters ─────────────────────────────────────────────────────────────────

def test_format_top_lists_wallets_with_pnl():
    scores = [
        {"wallet_address": "0xabc0000000000000000000000000000000000001",
         "realized_pnl": 124909.0, "win_rate": 0.43, "trade_count": 12},
    ]
    labels = {"0xabc0000000000000000000000000000000000001": "Tetranode"}
    out = bot.format_top(scores, labels)
    assert "Tetranode" in out and "$124.9K" in out and "43%" in out


def test_format_top_empty():
    assert "No scores" in bot.format_top([], {})


def test_format_wallet_sums_decimal_pnl_and_shows_coverage():
    wallet = {"address": "0xabc0000000000000000000000000000000000001", "label": "Tetranode"}
    history = [
        {"symbol": "PEPE", "realized_pnl": Decimal("100000")},
        {"symbol": "WBTC", "realized_pnl": Decimal("-25000")},
    ]
    coverage = {"coverage": 0.4}
    score = {"win_rate": 0.5, "trade_count": 2}
    out = bot.format_wallet(wallet, history, coverage, score)
    assert "Tetranode" in out
    assert "$75.0K" in out          # 100k - 25k summed exactly via Decimal
    assert "Coverage: 40%" in out
    assert "PEPE" in out and "WBTC" in out


def test_format_wallet_no_history():
    wallet = {"address": "0xabc0000000000000000000000000000000000001", "label": "Quiet"}
    out = bot.format_wallet(wallet, [], {"coverage": None}, None)
    assert "No closed trades" in out


def test_format_token_lists_holders():
    holders = [
        {"wallet_address": "0xabc", "label": "Machi", "net_amount": 5_000_000, "symbol": "PEPE"},
        {"wallet_address": "0xdef", "label": "Fickel", "net_amount": 1200, "symbol": "PEPE"},
    ]
    out = bot.format_token("PEPE", "0x1234567890abcdef1234567890abcdef12345678", holders)
    assert "$PEPE" in out and "Machi" in out and "5.00M" in out and "Fickel" in out


def test_format_token_no_holders():
    assert "No watched wallet" in bot.format_token("PEPE", "0xabc", [])


def test_format_movers_lists_consensus():
    accs = [{"symbol": "PEPE", "wallet_count": 2,
             "wallets": "0xabc0000000000000000000000000000000000001,0xdef0000000000000000000000000000000000002"}]
    labels = {"0xabc0000000000000000000000000000000000001": "Machi",
              "0xdef0000000000000000000000000000000000002": "Fickel"}
    out = bot.format_movers(accs, labels, 48)
    assert "$PEPE" in out and "Machi" in out and "Fickel" in out


def test_format_movers_empty():
    assert "No consensus" in bot.format_movers([], {}, 48)


def test_format_help_lists_all_commands():
    out = bot.format_help()
    for cmd, _ in bot.COMMANDS:
        assert f"/{cmd}" in out
