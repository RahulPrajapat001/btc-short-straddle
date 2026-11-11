"""
Pure-Python risk maths for the short straddle (Level 2).

We SOLD the call and the put, so:
  - combined ENTRY premium  = avg sell price of call + avg sell price of put
  - combined BUYBACK cost   = current ASK of call + current ASK of put
    (to close a short you must BUY, and you buy at the ask)

Both values are "per contract", which is valid because both legs always hold
the same quantity (the strategy enforces this).

  Stop-Loss   : buyback >= entry * (1 + stop_loss_pct/100)    -> losing money
  Take-Profit : buyback <= entry * (1 - take_profit_pct/100)  -> making money
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING
from decimal import ROUND_FLOOR
from decimal import Decimal

EXIT_NONE = "NONE"
EXIT_STOP_LOSS = "STOP_LOSS"
EXIT_TAKE_PROFIT = "TAKE_PROFIT"


@dataclass(frozen=True)
class RiskLevels:
    entry_premium: float
    stop_loss_level: float
    take_profit_level: float


def risk_levels(entry_premium: float, stop_loss_pct: float, take_profit_pct: float) -> RiskLevels:
    return RiskLevels(
        entry_premium=entry_premium,
        stop_loss_level=entry_premium * (1.0 + stop_loss_pct / 100.0),
        take_profit_level=entry_premium * (1.0 - take_profit_pct / 100.0),
    )


def check_exit(buyback_cost: float, levels: RiskLevels) -> str:
    """Return which exit (if any) the current combined buyback cost triggers."""
    if buyback_cost <= 0:
        return EXIT_NONE  # no valid quote -> never act on garbage data
    if buyback_cost >= levels.stop_loss_level:
        return EXIT_STOP_LOSS
    if buyback_cost <= levels.take_profit_level:
        return EXIT_TAKE_PROFIT
    return EXIT_NONE


def change_pct(entry_premium: float, buyback_cost: float) -> float:
    """% change of buyback cost vs entry premium (+ is bad for a short)."""
    if entry_premium <= 0:
        return 0.0
    return (buyback_cost / entry_premium - 1.0) * 100.0


def unrealized_pnl(entry_premium: float, buyback_cost: float, quantity: float) -> float:
    """PnL if we closed now at the ask (fees not included)."""
    return (entry_premium - buyback_cost) * quantity


def round_to_increment(value: float, increment: float, up: bool) -> float:
    """
    Snap a price/qty onto the exchange grid (e.g. BTC option tick = 5).
    `up=True` rounds up (use for BUY limit prices), False rounds down
    (use for SELL limit prices and for quantities).
    """
    if increment <= 0:
        return value
    inc = Decimal(str(increment))
    steps = (Decimal(str(value)) / inc).to_integral_value(
        rounding=ROUND_CEILING if up else ROUND_FLOOR,
    )
    return float(steps * inc)
