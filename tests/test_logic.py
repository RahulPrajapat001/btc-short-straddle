"""
Tests for the pure logic (no NautilusTrader / no internet needed).

    python -m pytest tests        # if pytest is installed
    python tests/test_logic.py    # plain python also works
"""

import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from straddle.risk import EXIT_NONE
from straddle.risk import EXIT_STOP_LOSS
from straddle.risk import EXIT_TAKE_PROFIT
from straddle.risk import check_exit
from straddle.risk import risk_levels
from straddle.risk import round_to_increment
from straddle.selection import choose_atm_straddle
from straddle.selection import parse_option_symbol

NOW = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp() * 1e9)


def _chain(symbols):
    return [parse_option_symbol(s) for s in symbols]


def test_parse_usdc_and_usdt_symbols():
    a = parse_option_symbol("BTC-27MAR26-70000-C-OPTION")
    assert (a.base, a.expiry_code, a.strike, a.kind, a.settle) == ("BTC", "27MAR26", 70000.0, "C", "USDC")
    b = parse_option_symbol("BTC-27MAR26-70000-P-USDT-OPTION")
    assert (b.kind, b.settle) == ("P", "USDT")
    assert datetime.fromtimestamp(a.expiry_ns / 1e9, tz=timezone.utc).hour == 8
    assert parse_option_symbol("BTCUSDT-LINEAR") is None


def test_atm_picks_closest_strike_with_both_legs():
    opts = _chain([
        "BTC-27MAR26-69000-C-OPTION", "BTC-27MAR26-69000-P-OPTION",
        "BTC-27MAR26-70000-C-OPTION",                               # no put -> not eligible
        "BTC-27MAR26-71000-C-OPTION", "BTC-27MAR26-71000-P-OPTION",
    ])
    c = choose_atm_straddle(opts, reference_price=70100, now_ns=NOW)
    assert c.strike == 71000 or c.strike == 69000
    assert c.strike == 71000  # |70100-71000|=900 < |70100-69000|=1100
    assert c.call_symbol.endswith("-C-OPTION") and c.put_symbol.endswith("-P-OPTION")


def test_nearest_expiry_and_settle_filter():
    opts = _chain([
        "BTC-28FEB26-70000-C-OPTION", "BTC-28FEB26-70000-P-OPTION",      # already expired vs NOW
        "BTC-06MAR26-70000-C-USDT-OPTION", "BTC-06MAR26-70000-P-USDT-OPTION",
        "BTC-13MAR26-70000-C-OPTION", "BTC-13MAR26-70000-P-OPTION",
    ])
    c = choose_atm_straddle(opts, 70000, NOW, settle="USDC")
    assert c.expiry_code == "13MAR26"
    c = choose_atm_straddle(opts, 70000, NOW, settle="USDT")
    assert c.expiry_code == "06MAR26"
    c = choose_atm_straddle(opts, 70000, NOW, expiry_code="13mar26")
    assert c.expiry_code == "13MAR26"


def test_no_straddle_available():
    assert choose_atm_straddle(_chain(["BTC-13MAR26-70000-C-OPTION"]), 70000, NOW) is None


def test_stop_loss_and_take_profit():
    lv = risk_levels(entry_premium=1000.0, stop_loss_pct=1.0, take_profit_pct=2.0)
    assert lv.stop_loss_level == 1010.0
    assert lv.take_profit_level == 980.0
    assert check_exit(1009.9, lv) == EXIT_NONE
    assert check_exit(1010.0, lv) == EXIT_STOP_LOSS
    assert check_exit(980.0, lv) == EXIT_TAKE_PROFIT
    assert check_exit(0.0, lv) == EXIT_NONE  # bad quote never triggers


def test_round_to_tick():
    assert round_to_increment(1234.0, 5.0, up=False) == 1230.0
    assert round_to_increment(1234.0, 5.0, up=True) == 1235.0
    assert round_to_increment(0.017, 0.01, up=False) == 0.01


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
