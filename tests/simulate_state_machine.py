"""
Offline simulation of the strategy's state machine.

It replaces NautilusTrader with tiny fake classes (so it runs without internet,
without Bybit and without nautilus installed) and replays scenarios:
happy path + take-profit, stop-loss with partial fill + re-pricing,
one leg not filling, unequal partial fills, Level 1 never exiting,
and the wide-spread case.

    python tests/simulate_state_machine.py

Run it in its own process - it injects fake `nautilus_trader` modules.
"""
import sys, types, itertools, enum
from decimal import Decimal
from datetime import datetime, timezone

from pathlib import Path
PROJECT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, PROJECT)

# ---------------- stubs ----------------
def mod(name):
    m = types.ModuleType(name); sys.modules[name] = m; return m

for n in ["nautilus_trader", "nautilus_trader.model", "nautilus_trader.trading"]:
    mod(n)

cfg = mod("nautilus_trader.config")
class StrategyConfig:
    def __init_subclass__(cls, **kw): pass
    def __init__(self, **kw):
        for k, v in kw.items(): setattr(self, k, v)
    def __getattr__(self, k):
        v = type(self).__dict__.get(k)
        if v is None:
            for c in type(self).__mro__:
                if k in c.__dict__: return c.__dict__[k]
            raise AttributeError(k)
        return v
cfg.StrategyConfig = StrategyConfig

enums = mod("nautilus_trader.model.enums")
class OrderSide(enum.Enum): BUY = 1; SELL = 2
class TimeInForce(enum.Enum): GTC = 1; IOC = 2
class PositionSide(enum.Enum): SHORT = 1
enums.OrderSide, enums.TimeInForce = OrderSide, TimeInForce

ids = mod("nautilus_trader.model.identifiers")
class Sym:
    def __init__(s, v): s.value = v
class InstrumentId:
    def __init__(s, v): s.v = v; s.symbol = Sym(v.rsplit(".", 1)[0])
    @classmethod
    def from_str(cls, v): return cls(v)
    def __eq__(s, o): return isinstance(o, InstrumentId) and o.v == s.v
    def __hash__(s): return hash(s.v)
    def __str__(s): return s.v
    __repr__ = __str__
class Venue:
    def __init__(s, v): s.v = v
    def __str__(s): return s.v
ids.InstrumentId, ids.Venue, ids.ClientOrderId = InstrumentId, Venue, str

data = mod("nautilus_trader.model.data")
class QuoteTick:
    def __init__(s, iid, bid, ask): s.instrument_id, s.bid_price, s.ask_price = iid, bid, ask
data.QuoteTick = QuoteTick

class Inst:
    def __init__(s, sym):
        s.id = InstrumentId(sym + ".BYBIT"); s.size_increment = 0.01; s.price_increment = 5.0; s.min_quantity = 0.01
    def make_qty(s, q): return Decimal(str(round(q, 2)))
    def make_price(s, p): return p

class Order:
    def __init__(s, coid, iid, side, qty, price, reduce_only):
        s.client_order_id, s.instrument_id, s.side, s.quantity, s.price, s.reduce_only = coid, iid, side, qty, price, reduce_only
        s.filled = Decimal(0); s.is_closed = False
    def __repr__(s): return f"Order({s.client_order_id} {s.side.name} {s.quantity} {s.instrument_id} @{s.price} ro={s.reduce_only})"

class Log:
    def __init__(s): s.lines = []
    def _p(s, lvl, m): s.lines.append((lvl, m)); print(f"  [{lvl}] {m}")
    def info(s, m): s._p("INF", m)
    def warning(s, m): s._p("WRN", m)
    def error(s, m): s._p("ERR", m)

class Clock:
    def __init__(s): s.t = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp() * 1e9)
    def timestamp_ns(s): return s.t
    def set_timer(s, name, interval, callback): s.cb = callback
    def cancel_timer(s, name): pass

class Cache:
    def __init__(s, insts): s.insts = insts; s.orders = {}
    def instrument(s, iid): return next((i for i in s.insts if i.id == iid), None)
    def instruments(s, venue=None): return s.insts
    def order(s, coid): return s.orders.get(coid)
    def positions_open(s, **kw): return []

class Factory:
    def __init__(s, cache): s.cache = cache; s.n = itertools.count(1)
    def limit(s, instrument_id, order_side, quantity, price, time_in_force, reduce_only):
        o = Order(f"O-{next(s.n)}", instrument_id, order_side, quantity, price, reduce_only); s.cache.orders[o.client_order_id] = o; return o
    def market(s, instrument_id, order_side, quantity, time_in_force, reduce_only):
        return s.limit(instrument_id, order_side, quantity, None, time_in_force, reduce_only)

strat_mod = mod("nautilus_trader.trading.strategy")
class Strategy:
    def __init__(s, config):
        s.config = config; s.log = Log(); s.clock = Clock(); s.submitted = []; s.cancels = []; s.id = "S"
    def subscribe_quote_ticks(s, iid): pass
    def submit_order(s, o): s.submitted.append(o); print(f"  >> submit {o}")
    def cancel_order(s, o): s.cancels.append(o); print(f"  >> cancel {o.client_order_id}")
    def cancel_all_orders(s, iid): pass
    def close_all_positions(s, iid): pass
    def stop(s): pass
strat_mod.Strategy = Strategy

from straddle.strategy import ShortStraddle, ShortStraddleConfig, State

# ---------------- harness ----------------
class Ev: pass
def fill(st, o, qty):
    qty = Decimal(str(qty)); o.filled += qty
    if o.filled >= o.quantity: o.is_closed = True
    e = Ev(); e.client_order_id = o.client_order_id; e.instrument_id = o.instrument_id
    e.last_qty = qty; e.last_px = o.price if o.price is not None else 100; e.order_side = o.side
    st.on_order_filled(e)
def cancel(st, o):
    o.is_closed = True; e = Ev(); e.client_order_id = o.client_order_id; st.on_order_canceled(e)
def reject(st, o):
    o.is_closed = True; e = Ev(); e.client_order_id = o.client_order_id; e.reason = "test"; st.on_order_rejected(e)
def advance(st, secs):
    st.clock.t += int(secs * 1e9); st.clock.cb(None)

def make(level=2, **kw):
    syms = ["BTCUSDT-LINEAR", "BTC-13MAR26-70000-C-OPTION", "BTC-13MAR26-70000-P-OPTION",
            "BTC-13MAR26-72000-C-OPTION", "BTC-13MAR26-72000-P-OPTION"]
    insts = [Inst(x) for x in syms]
    st = ShortStraddle(ShortStraddleConfig(enable_risk_management=(level == 2), **kw))
    st.cache = Cache(insts); st.order_factory = Factory(st.cache)
    st.on_start()
    st.on_quote_tick(QuoteTick(InstrumentId("BTCUSDT-LINEAR.BYBIT"), 70400, 70402))
    assert st.state == State.WAITING_FOR_LEG_QUOTES and st.strike == 70000, (st.state, st.strike)
    return st

def q(st, leg, bid, ask):
    st.on_quote_tick(QuoteTick(leg, bid, ask))

def check(cond, msg):
    print(("  OK  " if cond else "  FAIL ") + msg)
    if not cond: raise SystemExit(1)

print("\n=== 1. Happy path + take profit ===")
st = make()
q(st, st.call_id, 1000, 1002); q(st, st.put_id, 900, 902)
check(st.state == State.ENTERING and len(st.submitted) == 2, "two sells submitted")
c, p = st.submitted
check(c.price == 1000 and p.price == 900 and c.side == OrderSide.SELL, "sell at bid, tick-rounded")
fill(st, c, 0.01); check(st.state == State.ENTERING, "still entering after one leg")
fill(st, p, 0.01); check(st.state == State.ACTIVE and st.levels.entry_premium == 1900, "active, entry=1900")
q(st, st.call_id, 900, 950); q(st, st.put_id, 800, 862)  # 1812 <= 1862 TP
check(st.state == State.EXITING and st.exit_reason == "TAKE_PROFIT", "TP triggered")
n = len(st.submitted)
q(st, st.call_id, 900, 950); q(st, st.put_id, 800, 862); advance(st, 1)
check(len(st.submitted) == n, "no duplicate exit orders on repeated ticks")
bc, bp = st.submitted[2], st.submitted[3]
check(bc.side == OrderSide.BUY and bc.reduce_only and bc.price == 950, "reduce-only buy at ask (tick 5)")
fill(st, bc, 0.01); fill(st, bp, 0.01)
check(st.state == State.DONE, "DONE after both buybacks")

print("\n=== 2. Stop loss, partial exit fill, reprice ===")
st = make(quantity=0.05)
q(st, st.call_id, 1000, 1002); q(st, st.put_id, 900, 902)
c, p = st.submitted; fill(st, c, 0.05); fill(st, p, 0.05)
q(st, st.call_id, 1020, 1030)  # buyback = 1030+902 = 1932 >= 1919 SL
check(st.exit_reason == "STOP_LOSS", "SL triggered")
bc, bp = st.submitted[2], st.submitted[3]
fill(st, bc, 0.02)   # partial
advance(st, 16)      # reprice -> cancel
check(bc in st.cancels and bp in st.cancels, "stale buybacks cancelled for reprice")
cancel(st, bc)
new = st.submitted[-1]
check(new.instrument_id == st.call_id and new.quantity == Decimal("0.03"), "resubmitted only remaining 0.03")
fill(st, new, 0.03); fill(st, bp, 0.05)
check(st.state == State.DONE, "DONE")

print("\n=== 3. Only one leg fills -> entry timeout -> close filled leg ===")
st = make()
q(st, st.call_id, 1000, 1002); q(st, st.put_id, 900, 902)
c, p = st.submitted; fill(st, c, 0.01)
advance(st, 61)
check(p in st.cancels, "unfilled put cancelled")
cancel(st, p)
check(st.state == State.REBALANCING, "rebalancing")
b = st.submitted[-1]
check(b.instrument_id == st.call_id and b.side == OrderSide.BUY and b.quantity == Decimal("0.01"), "buy back the lonely call")
fill(st, b, 0.01)
check(st.state == State.FAILED, "flat, finished as FAILED (no straddle)")

print("\n=== 4. Unequal partials -> trim to matched size -> ACTIVE ===")
st = make(quantity=0.05)
q(st, st.call_id, 1000, 1002); q(st, st.put_id, 900, 902)
c, p = st.submitted; fill(st, c, 0.05); fill(st, p, 0.03)
advance(st, 61); cancel(st, p)
b = st.submitted[-1]
check(b.instrument_id == st.call_id and b.quantity == Decimal("0.02"), "buy back extra 0.02 call")
reject(st, b)
b2 = st.submitted[-1]
check(b2 is not b and b2.quantity == Decimal("0.02"), "retried after reject")
fill(st, b2, 0.02)
check(st.state == State.ACTIVE and st.position_qty == Decimal("0.03"), "active with 0.03 each")

print("\n=== 5. Level 1 never exits ===")
st = make(level=1)
q(st, st.call_id, 1000, 1002); q(st, st.put_id, 900, 902)
c, p = st.submitted; fill(st, c, 0.01); fill(st, p, 0.01)
q(st, st.call_id, 5000, 5100)
check(st.state == State.ACTIVE and len(st.submitted) == 2, "no exit in level 1")
advance(st, 10)

print("\n=== 6. Wide spread: warning, and guard blocks entry ===")
st = make()
q(st, st.call_id, 1000, 1020); q(st, st.put_id, 900, 920)
c, p = st.submitted; fill(st, c, 0.01); fill(st, p, 0.01)
check(any("spread" in m for _, m in st.log.lines), "wide-spread warning logged")
check(st.exit_reason == "STOP_LOSS", "spec-literal: SL fires on spread alone")
st = make(max_entry_spread_pct=0.5)
q(st, st.call_id, 1000, 1020); q(st, st.put_id, 900, 920)
check(st.state == State.WAITING_FOR_LEG_QUOTES and not st.submitted, "guard waits on wide spread")
q(st, st.call_id, 1000, 1002); q(st, st.put_id, 900, 902)
check(st.state == State.ENTERING, "enters when spread tightens")
print("\nALL SIMULATIONS PASSED")
