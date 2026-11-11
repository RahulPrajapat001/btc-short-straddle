"""
Pure-Python helpers for finding the ATM straddle.

Nothing in this file imports NautilusTrader, so it can be unit-tested on any
machine (see tests/test_logic.py).

Bybit option symbols look like this (Nautilus adds the "-OPTION" suffix):

    BTC-27MAR26-70000-P-OPTION         -> USDC-settled put
    BTC-27MAR26-70000-C-USDT-OPTION    -> USDT-settled call

    <base>-<expiry date>-<strike>-<C|P>[-<settle coin>]-OPTION
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone

# Bybit options expire at 08:00 UTC on the expiry date.
BYBIT_EXPIRY_HOUR_UTC = 8


@dataclass(frozen=True)
class OptionInfo:
    symbol: str          # raw symbol, e.g. "BTC-27MAR26-70000-C-OPTION"
    base: str            # "BTC"
    expiry_code: str     # "27MAR26"
    expiry_ns: int       # expiry as unix nanoseconds (UTC)
    strike: float        # 70000.0
    kind: str            # "C" or "P"
    settle: str          # "USDC" or "USDT"


@dataclass(frozen=True)
class StraddleChoice:
    call_symbol: str
    put_symbol: str
    strike: float
    expiry_code: str
    expiry_ns: int
    settle: str
    reference_price: float


def parse_option_symbol(symbol: str, expiry_ns: int | None = None) -> OptionInfo | None:
    """
    Parse a Bybit option symbol. Returns None if it is not an option symbol.

    `expiry_ns` can be passed in from the Nautilus instrument (exact value);
    otherwise it is derived from the date in the symbol at 08:00 UTC.
    """
    parts = symbol.split("-")
    if parts and parts[-1] == "OPTION":
        parts = parts[:-1]
    if len(parts) not in (4, 5):
        return None

    base, expiry_code, strike_str, kind = parts[0], parts[1], parts[2], parts[3]
    settle = parts[4] if len(parts) == 5 else "USDC"

    if kind not in ("C", "P"):
        return None
    try:
        strike = float(strike_str)
        expiry_dt = datetime.strptime(expiry_code, "%d%b%y").replace(
            hour=BYBIT_EXPIRY_HOUR_UTC,
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None

    if expiry_ns is None:
        expiry_ns = int(expiry_dt.timestamp() * 1_000_000_000)

    return OptionInfo(
        symbol=symbol,
        base=base,
        expiry_code=expiry_code.upper(),
        expiry_ns=expiry_ns,
        strike=strike,
        kind=kind,
        settle=settle.upper(),
    )


def choose_atm_straddle(
    options: list[OptionInfo],
    reference_price: float,
    now_ns: int,
    base: str = "BTC",
    settle: str = "USDC",
    min_time_to_expiry_hours: float = 1.0,
    expiry_code: str | None = None,
) -> StraddleChoice | None:
    """
    Pick the ATM call + put with the SAME strike and SAME expiry.

    1. Keep only `base` options with the requested settle coin that expire
       more than `min_time_to_expiry_hours` from now.
    2. Pick the expiry: `expiry_code` if given, otherwise the nearest one that
       has at least one strike with BOTH a call and a put listed.
    3. Inside that expiry, pick the strike closest to `reference_price`
       (ties go to the lower strike).
    """
    if reference_price <= 0:
        return None

    min_expiry_ns = now_ns + int(min_time_to_expiry_hours * 3600 * 1_000_000_000)

    # expiry_ns -> strike -> {"C": OptionInfo, "P": OptionInfo}
    chain: dict[int, dict[float, dict[str, OptionInfo]]] = {}
    for opt in options:
        if opt.base != base or opt.settle != settle.upper():
            continue
        if opt.expiry_ns <= min_expiry_ns:
            continue
        if expiry_code and opt.expiry_code != expiry_code.upper():
            continue
        chain.setdefault(opt.expiry_ns, {}).setdefault(opt.strike, {})[opt.kind] = opt

    for exp_ns in sorted(chain):
        complete = {k: v for k, v in chain[exp_ns].items() if "C" in v and "P" in v}
        if not complete:
            continue
        strike = min(complete, key=lambda k: (abs(k - reference_price), k))
        call, put = complete[strike]["C"], complete[strike]["P"]
        return StraddleChoice(
            call_symbol=call.symbol,
            put_symbol=put.symbol,
            strike=strike,
            expiry_code=call.expiry_code,
            expiry_ns=exp_ns,
            settle=call.settle,
            reference_price=reference_price,
        )
    return None


def list_expiries(options: list[OptionInfo], base: str = "BTC", settle: str = "USDC") -> list[str]:
    """Sorted list of expiry codes available (for logging / debugging)."""
    seen: dict[int, str] = {}
    for opt in options:
        if opt.base == base and opt.settle == settle.upper():
            seen[opt.expiry_ns] = opt.expiry_code
    return [seen[k] for k in sorted(seen)]
