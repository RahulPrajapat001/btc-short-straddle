"""
BTC ATM Short Straddle strategy for NautilusTrader (Bybit options).

Level 1 (enable_risk_management = False)
    connect -> get BTC reference price -> find ATM call+put (same strike,
    same expiry) -> SELL both -> track acks, fills and positions forever.

Level 2 (enable_risk_management = True)
    Level 1 + continuously compare the combined ASK (cost to buy both legs
    back) with the combined entry premium:
        Stop-Loss   when buyback cost rises 1% above entry premium
        Take-Profit when buyback cost falls 2% below entry premium
    and then buy back both legs with reduce-only orders.

The whole strategy is a small state machine:

    WAITING_FOR_REFERENCE  -> waiting for the first BTC price
    WAITING_FOR_LEG_QUOTES -> ATM legs chosen, waiting for bid/ask on both
    ENTERING               -> two SELL orders working
    REBALANCING            -> only one leg (or unequal size) filled; buying
                              back the extra so both legs are equal
    ACTIVE                 -> straddle open (Level 2: SL/TP being monitored)
    EXITING                -> SL/TP hit; buying back both legs
    DONE / FAILED          -> finished

Every buy-back goes through ONE function (`_drive_leg`) which only ever has
ONE open buy-back order per leg and sizes it from our own fill ledger. That
is what prevents duplicate exits and over-buying on partial fills.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from enum import Enum

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.trading.strategy import Strategy

from straddle.risk import EXIT_NONE
from straddle.risk import RiskLevels
from straddle.risk import change_pct
from straddle.risk import check_exit
from straddle.risk import risk_levels
from straddle.risk import round_to_increment
from straddle.risk import unrealized_pnl
from straddle.selection import choose_atm_straddle
from straddle.selection import list_expiries
from straddle.selection import parse_option_symbol

NANOS = 1_000_000_000
ZERO = Decimal(0)
TIMER_NAME = "STRADDLE-TIMER"


class State(Enum):
    WAITING_FOR_REFERENCE = "WAITING_FOR_REFERENCE"
    WAITING_FOR_LEG_QUOTES = "WAITING_FOR_LEG_QUOTES"
    ENTERING = "ENTERING"
    REBALANCING = "REBALANCING"
    ACTIVE = "ACTIVE"
    EXITING = "EXITING"
    DONE = "DONE"
    FAILED = "FAILED"


class ShortStraddleConfig(StrategyConfig, frozen=True):
    # --- market selection ---
    venue: str = "BYBIT"
    reference_instrument_id: str = "BTCUSDT-LINEAR.BYBIT"
    base_currency: str = "BTC"
    settle_coin: str = "USDC"              # "USDC" or "USDT" option family
    expiry: str = ""                       # e.g. "27MAR26"; empty = nearest expiry
    min_time_to_expiry_hours: float = 1.0  # skip options about to expire

    # --- orders ---
    quantity: float = 0.01                 # contracts per leg (BTC options min = 0.01)
    use_market_orders: bool = False        # False = marketable LIMIT at bid/ask
    entry_slippage_pct: float = 0.0        # sell at bid*(1 - x%)
    exit_slippage_pct: float = 0.0         # buy at ask*(1 + x%)
    max_entry_spread_pct: float = 0.0      # only enter if combined (ask-bid)/bid <= x%; 0 = off
    entry_timeout_secs: float = 60.0       # cancel unfilled entry after this
    order_reprice_secs: float = 15.0       # re-price a resting buy-back after this
    max_buyback_attempts: int = 20         # per leg

    # --- monitoring ---
    status_interval_secs: float = 10.0

    # --- Level 2 risk management ---
    enable_risk_management: bool = False
    stop_loss_pct: float = 1.0
    take_profit_pct: float = 2.0

    close_positions_on_stop: bool = False


class ShortStraddle(Strategy):
    def __init__(self, config: ShortStraddleConfig) -> None:
        super().__init__(config)
        self.venue = Venue(config.venue)
        self.ref_id = InstrumentId.from_str(config.reference_instrument_id)

        self.state = State.WAITING_FOR_REFERENCE
        self.state_since_ns = 0
        self.ref_price: float | None = None

        # Chosen legs
        self.call_id: InstrumentId | None = None
        self.put_id: InstrumentId | None = None
        self.call_inst = None
        self.put_inst = None
        self.strike: float | None = None
        self.quotes: dict[InstrumentId, QuoteTick] = {}

        # Orders we own: client_order_id -> "ENTRY" | "BUYBACK"
        self.my_orders: dict[ClientOrderId, str] = {}
        self.entry_orders: list[ClientOrderId] = []
        self.entry_started_ns = 0
        self.entry_cancel_sent = False

        # Buy-back control (one open buy-back order per leg at most)
        self.target_qty: dict[InstrumentId, Decimal | None] = {}
        self.buyback_order: dict[InstrumentId, ClientOrderId] = {}
        self.buyback_sent_ns: dict[ClientOrderId, int] = {}
        self.buyback_attempts: dict[InstrumentId, int] = {}
        self.cancel_sent: set[ClientOrderId] = set()

        # Our own fill ledger (source of truth for sizing buy-backs)
        self.short_qty: dict[InstrumentId, Decimal] = {}
        self.sell_qty: dict[InstrumentId, Decimal] = {}
        self.sell_notional: dict[InstrumentId, float] = {}
        self.buy_notional: dict[InstrumentId, float] = {}

        # Straddle bookkeeping
        self.position_qty: Decimal = ZERO
        self.levels: RiskLevels | None = None
        self.exit_reason: str | None = None
        self.last_status_ns = 0
        self._last_warn_ns: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def on_start(self) -> None:
        mode = "LEVEL 2 (SL/TP ON)" if self.config.enable_risk_management else "LEVEL 1 (naked)"
        self.log.info(f"Starting BTC ATM short straddle - {mode}")

        if self.cache.instrument(self.ref_id) is None:
            self.log.error(
                f"Reference instrument {self.ref_id} not found in cache. "
                "Make sure LINEAR is in the data client's product_types.",
            )
            self._set_state(State.FAILED)
            self.stop()
            return

        self._warn_about_existing_positions()

        self.subscribe_quote_ticks(self.ref_id)
        self.clock.set_timer(
            name=TIMER_NAME,
            interval=timedelta(seconds=1),
            callback=self._on_timer,
        )
        self._set_state(State.WAITING_FOR_REFERENCE)

    def on_stop(self) -> None:
        try:
            self.clock.cancel_timer(TIMER_NAME)
        except Exception:  # timer may not exist if start failed
            pass

        for iid in self._legs():
            self.cancel_all_orders(iid)
            if self.config.close_positions_on_stop:
                self.log.warning(f"close_positions_on_stop=True -> closing {iid}")
                self.close_all_positions(iid)

        self._log_status(force=True)
        self.log.info("Strategy stopped. Open positions (if any) remain on Bybit.")

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #
    def on_quote_tick(self, tick: QuoteTick) -> None:
        iid = tick.instrument_id

        if iid == self.ref_id:
            bid, ask = float(tick.bid_price), float(tick.ask_price)
            if bid > 0 and ask > 0:
                self.ref_price = (bid + ask) / 2.0
            if self.state == State.WAITING_FOR_REFERENCE and self.ref_price:
                self._select_straddle()
            return

        if iid not in self._legs():
            return

        self.quotes[iid] = tick

        if self.state == State.WAITING_FOR_LEG_QUOTES:
            self._try_enter()
        elif self.state == State.ACTIVE:
            self._check_risk()
        elif self.state in (State.REBALANCING, State.EXITING):
            self._progress()  # fresh ask may unblock a pending buy-back

    # ------------------------------------------------------------------ #
    # Step 1: find the ATM straddle
    # ------------------------------------------------------------------ #
    def _select_straddle(self) -> None:
        options = []
        for inst in self.cache.instruments(venue=self.venue):
            symbol = inst.id.symbol.value
            if not symbol.endswith("-OPTION"):
                continue
            exp_ns = getattr(inst, "expiration_ns", None)
            info = parse_option_symbol(symbol, int(exp_ns) if exp_ns else None)
            if info is not None:
                options.append(info)

        self.log.info(
            f"Loaded {len(options)} option instruments. "
            f"{self.config.base_currency}/{self.config.settle_coin} expiries: "
            f"{list_expiries(options, self.config.base_currency, self.config.settle_coin)}",
        )

        choice = choose_atm_straddle(
            options=options,
            reference_price=self.ref_price,
            now_ns=self.clock.timestamp_ns(),
            base=self.config.base_currency,
            settle=self.config.settle_coin,
            min_time_to_expiry_hours=self.config.min_time_to_expiry_hours,
            expiry_code=self.config.expiry or None,
        )
        if choice is None:
            self.log.error(
                "No ATM straddle found (no expiry with both a call and a put at the same strike). "
                "Check settle_coin / expiry in config.toml.",
            )
            self._set_state(State.FAILED)
            return

        self.call_id = InstrumentId.from_str(f"{choice.call_symbol}.{self.venue}")
        self.put_id = InstrumentId.from_str(f"{choice.put_symbol}.{self.venue}")
        self.call_inst = self.cache.instrument(self.call_id)
        self.put_inst = self.cache.instrument(self.put_id)
        self.strike = choice.strike

        for iid in self._legs():
            self.short_qty[iid] = ZERO
            self.sell_qty[iid] = ZERO
            self.sell_notional[iid] = 0.0
            self.buy_notional[iid] = 0.0
            self.target_qty[iid] = None
            self.buyback_attempts[iid] = 0

        self.log.info(
            f"ATM selected: ref={self.ref_price:,.2f} -> strike={choice.strike:,.0f} "
            f"expiry={choice.expiry_code} | CALL={self.call_id} | PUT={self.put_id}",
        )

        self.subscribe_quote_ticks(self.call_id)
        self.subscribe_quote_ticks(self.put_id)
        self._set_state(State.WAITING_FOR_LEG_QUOTES)

    # ------------------------------------------------------------------ #
    # Step 2: sell both legs
    # ------------------------------------------------------------------ #
    def _try_enter(self) -> None:
        cq, pq = self.quotes.get(self.call_id), self.quotes.get(self.put_id)
        if cq is None or pq is None:
            return
        if float(cq.bid_price) <= 0 or float(pq.bid_price) <= 0:
            self._warn_throttled("no_bid", "Waiting: one of the legs has no bid yet, can't sell.")
            return

        # We SELL at the bid but value the buy-back at the ASK, so the spread
        # is an instant "loss" on paper. If the spread is wider than the
        # stop-loss %, Level 2 would stop out immediately.
        spread_pct = self._combined_spread_pct()
        if self.config.max_entry_spread_pct > 0 and spread_pct is not None and spread_pct > self.config.max_entry_spread_pct:
            self._warn_throttled(
                "spread",
                f"Waiting: combined spread {spread_pct:.2f}% > max_entry_spread_pct {self.config.max_entry_spread_pct}%",
            )
            return

        qty = round_to_increment(
            self.config.quantity,
            float(self.call_inst.size_increment),
            up=False,
        )
        min_qty = float(self.call_inst.min_quantity) if self.call_inst.min_quantity else 0.0
        if qty <= 0 or qty < min_qty:
            self.log.error(f"quantity={self.config.quantity} is below exchange minimum {min_qty}")
            self._set_state(State.FAILED)
            return

        call_order = self._make_order(self.call_inst, OrderSide.SELL, qty, float(cq.bid_price),
                                      self.config.entry_slippage_pct, reduce_only=False)
        put_order = self._make_order(self.put_inst, OrderSide.SELL, qty, float(pq.bid_price),
                                     self.config.entry_slippage_pct, reduce_only=False)

        # Change state BEFORE submitting so no other tick can enter twice.
        self._set_state(State.ENTERING)
        self.entry_started_ns = self.clock.timestamp_ns()

        for order in (call_order, put_order):
            self.my_orders[order.client_order_id] = "ENTRY"
            self.entry_orders.append(order.client_order_id)
            self.log.info(f"SUBMIT ENTRY {order}")
            self.submit_order(order)

    # ------------------------------------------------------------------ #
    # Order / position events  (this is the "tracking" part)
    # ------------------------------------------------------------------ #
    def on_order_accepted(self, event) -> None:
        if event.client_order_id in self.my_orders:
            self.log.info(f"ACK  {event.client_order_id} accepted by Bybit (venue id {event.venue_order_id})")

    def on_order_rejected(self, event) -> None:
        if event.client_order_id in self.my_orders:
            self.log.error(f"REJECTED {event.client_order_id}: {event.reason}")
            self._progress()

    def on_order_denied(self, event) -> None:
        if event.client_order_id in self.my_orders:
            self.log.error(f"DENIED (pre-trade risk) {event.client_order_id}: {event.reason}")
            self._progress()

    def on_order_canceled(self, event) -> None:
        if event.client_order_id in self.my_orders:
            self.log.info(f"CANCELED {event.client_order_id}")
            self._progress()

    def on_order_expired(self, event) -> None:
        if event.client_order_id in self.my_orders:
            self.log.info(f"EXPIRED {event.client_order_id}")
            self._progress()

    def on_order_filled(self, event) -> None:
        coid = event.client_order_id
        if coid not in self.my_orders:
            return
        iid = event.instrument_id
        qty = Decimal(str(event.last_qty))
        px = float(event.last_px)

        # Update our own ledger first - buy-back sizing is based on this,
        # not on the cached position (which updates *after* this event).
        if event.order_side == OrderSide.SELL:
            self.short_qty[iid] += qty
            self.sell_qty[iid] += qty
            self.sell_notional[iid] += px * float(qty)
        else:
            self.short_qty[iid] -= qty
            self.buy_notional[iid] += px * float(qty)

        self.log.info(
            f"FILL {self.my_orders[coid]} {event.order_side.name} {qty} {iid} @ {px} "
            f"-> short now {self.short_qty[iid]}",
        )
        self._progress()

    def on_position_opened(self, event) -> None:
        if event.instrument_id in self._legs():
            self.log.info(f"POSITION OPENED  {event.instrument_id} {event.side.name} qty={event.quantity} avg={event.avg_px_open}")

    def on_position_changed(self, event) -> None:
        if event.instrument_id in self._legs():
            self.log.info(f"POSITION CHANGED {event.instrument_id} qty={event.quantity} uPnL={event.unrealized_pnl}")

    def on_position_closed(self, event) -> None:
        if event.instrument_id in self._legs():
            self.log.info(f"POSITION CLOSED  {event.instrument_id} realized={event.realized_pnl}")

    # ------------------------------------------------------------------ #
    # Central state progression (idempotent - safe to call any time)
    # ------------------------------------------------------------------ #
    def _progress(self) -> None:
        if self.state == State.ENTERING:
            self._progress_entry()
        elif self.state == State.REBALANCING:
            if self._legs_at_target():
                if self.position_qty > 0:
                    self.log.info(f"Rebalanced: both legs short {self.position_qty}")
                    self._activate()
                else:
                    self.log.warning("Rebalanced to flat: nothing left to manage.")
                    self._finish(State.FAILED)
            else:
                self._drive_all()
        elif self.state == State.EXITING:
            if self._legs_at_target():
                self._finish(State.DONE)
            else:
                self._drive_all()

    def _progress_entry(self) -> None:
        orders = [self.cache.order(c) for c in self.entry_orders]
        if any(o is None or not o.is_closed for o in orders):
            return  # still working

        cq, pq = self.short_qty[self.call_id], self.short_qty[self.put_id]
        if cq == ZERO and pq == ZERO:
            self.log.error("Entry finished with NO fills on either leg.")
            self._set_state(State.FAILED)
            return
        if cq == pq:
            self.position_qty = cq
            self._activate()
            return

        # One leg filled (more) than the other -> legging risk.
        matched = min(cq, pq)
        self.log.warning(
            f"LEG MISMATCH: call short {cq}, put short {pq}. "
            f"Buying back the extra so both legs = {matched}.",
        )
        self.position_qty = matched
        for iid in self._legs():
            self.target_qty[iid] = matched
        self._set_state(State.REBALANCING)
        self._drive_all()

    def _activate(self) -> None:
        call_avg = self.sell_notional[self.call_id] / float(self.sell_qty[self.call_id])
        put_avg = self.sell_notional[self.put_id] / float(self.sell_qty[self.put_id])
        entry_premium = call_avg + put_avg
        self.levels = risk_levels(entry_premium, self.config.stop_loss_pct, self.config.take_profit_pct)
        self._set_state(State.ACTIVE)

        msg = (
            f"STRADDLE OPEN: short {self.position_qty} x strike {self.strike:,.0f} | "
            f"call avg {call_avg:.2f} + put avg {put_avg:.2f} = entry premium {entry_premium:.2f}"
        )
        if self.config.enable_risk_management:
            msg += f" | SL if buyback >= {self.levels.stop_loss_level:.2f}, TP if <= {self.levels.take_profit_level:.2f}"
        else:
            msg += " | Level 1: no automated exits."
        self.log.info(msg)

        spread_pct = self._combined_spread_pct()
        if (
            self.config.enable_risk_management
            and spread_pct is not None
            and spread_pct >= self.config.stop_loss_pct
        ):
            self.log.warning(
                f"Bid/ask spread is {spread_pct:.2f}% (>= stop-loss {self.config.stop_loss_pct}%). "
                "The stop-loss will probably trigger right away - see README 'Spread vs 1% stop-loss'.",
            )
        self._check_risk()

    # ------------------------------------------------------------------ #
    # Level 2: risk check on every leg quote
    # ------------------------------------------------------------------ #
    def _check_risk(self) -> None:
        if self.state != State.ACTIVE or not self.config.enable_risk_management:
            return
        buyback = self._buyback_cost()
        if buyback is None:
            return
        reason = check_exit(buyback, self.levels)
        if reason != EXIT_NONE:
            self._start_exit(reason, buyback)

    def _start_exit(self, reason: str, buyback: float) -> None:
        if self.state != State.ACTIVE:  # duplicate-exit guard
            return
        self._set_state(State.EXITING)
        self.exit_reason = reason
        self.log.warning(
            f"{reason} TRIGGERED: buyback {buyback:.2f} vs entry {self.levels.entry_premium:.2f} "
            f"({change_pct(self.levels.entry_premium, buyback):+.2f}%). Closing both legs.",
        )
        for iid in self._legs():
            self.target_qty[iid] = ZERO
        self._drive_all()

    # ------------------------------------------------------------------ #
    # Buy-backs (used by both rebalancing and SL/TP exits)
    # ------------------------------------------------------------------ #
    def _drive_all(self) -> None:
        for iid in self._legs():
            self._drive_leg(iid)

    def _drive_leg(self, iid: InstrumentId) -> None:
        target = self.target_qty.get(iid)
        if target is None:
            return
        if self._has_open_buyback(iid):
            return  # ONE open buy-back per leg -> no duplicates
        excess = self.short_qty[iid] - target
        if excess <= ZERO:
            return
        if self.buyback_attempts[iid] >= self.config.max_buyback_attempts:
            self._warn_throttled(f"max_{iid}", f"Max buy-back attempts reached for {iid}. MANUAL ACTION NEEDED.")
            return

        quote = self.quotes.get(iid)
        ask = float(quote.ask_price) if quote is not None else 0.0
        if not self.config.use_market_orders and ask <= 0:
            self._warn_throttled(f"noask_{iid}", f"No ask for {iid} yet - will retry buy-back.")
            return

        inst = self._inst(iid)
        order = self._make_order(inst, OrderSide.BUY, float(excess), ask,
                                 self.config.exit_slippage_pct, reduce_only=True)
        self.my_orders[order.client_order_id] = "BUYBACK"
        self.buyback_order[iid] = order.client_order_id
        self.buyback_sent_ns[order.client_order_id] = self.clock.timestamp_ns()
        self.buyback_attempts[iid] += 1
        self.log.info(f"SUBMIT BUYBACK (attempt {self.buyback_attempts[iid]}) {order}")
        self.submit_order(order)

    def _has_open_buyback(self, iid: InstrumentId) -> bool:
        coid = self.buyback_order.get(iid)
        if coid is None:
            return False
        order = self.cache.order(coid)
        return order is not None and not order.is_closed

    def _legs_at_target(self) -> bool:
        for iid in self._legs():
            if self._has_open_buyback(iid):
                return False
            target = self.target_qty.get(iid) or ZERO
            if self.short_qty[iid] != target:
                return False
        return True

    # ------------------------------------------------------------------ #
    # Timer: entry timeout, re-pricing, status
    # ------------------------------------------------------------------ #
    def _on_timer(self, event) -> None:
        now = self.clock.timestamp_ns()

        # 1) Entry timeout -> cancel whatever is unfilled; _progress_entry
        #    then handles a possible one-legged fill.
        if (
            self.state == State.ENTERING
            and not self.entry_cancel_sent
            and now - self.entry_started_ns > self.config.entry_timeout_secs * NANOS
        ):
            self.entry_cancel_sent = True
            self.log.warning("Entry timeout - cancelling unfilled entry orders.")
            for coid in self.entry_orders:
                self._cancel_if_open(coid)

        # 2) Re-price buy-backs that are resting too long (cancel -> the
        #    cancel event triggers a fresh order at the new ask).
        if self.state in (State.REBALANCING, State.EXITING):
            for iid in self._legs():
                coid = self.buyback_order.get(iid)
                if coid is None or not self._has_open_buyback(iid):
                    continue
                age = now - self.buyback_sent_ns.get(coid, now)
                if age > self.config.order_reprice_secs * NANOS:
                    self.log.info(f"Buy-back {coid} not filled in {self.config.order_reprice_secs}s - re-pricing")
                    self._cancel_if_open(coid)
            self._progress()

        # 3) Waiting too long for something?
        if self.state == State.WAITING_FOR_LEG_QUOTES and now - self.state_since_ns > 60 * NANOS:
            self._warn_throttled("leg_quotes", "Still waiting for bid/ask on both legs (illiquid market?).")

        # 4) Periodic status line
        self._log_status()

    def _cancel_if_open(self, coid: ClientOrderId) -> None:
        order = self.cache.order(coid)
        if order is None or order.is_closed or coid in self.cancel_sent:
            return
        self.cancel_sent.add(coid)
        self.cancel_order(order)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _make_order(self, inst, side: OrderSide, qty: float, quote_px: float,
                    slippage_pct: float, reduce_only: bool):
        quantity = inst.make_qty(round_to_increment(qty, float(inst.size_increment), up=False))

        if self.config.use_market_orders:
            return self.order_factory.market(
                instrument_id=inst.id,
                order_side=side,
                quantity=quantity,
                time_in_force=TimeInForce.IOC,
                reduce_only=reduce_only,
            )

        tick = float(inst.price_increment)
        if side == OrderSide.SELL:
            px = round_to_increment(quote_px * (1 - slippage_pct / 100.0), tick, up=False)
            px = max(px, tick)
        else:
            px = round_to_increment(quote_px * (1 + slippage_pct / 100.0), tick, up=True)

        return self.order_factory.limit(
            instrument_id=inst.id,
            order_side=side,
            quantity=quantity,
            price=inst.make_price(px),
            time_in_force=TimeInForce.GTC,
            reduce_only=reduce_only,
        )

    def _buyback_cost(self) -> float | None:
        cq, pq = self.quotes.get(self.call_id), self.quotes.get(self.put_id)
        if cq is None or pq is None:
            return None
        call_ask, put_ask = float(cq.ask_price), float(pq.ask_price)
        if call_ask <= 0 or put_ask <= 0:
            return None
        return call_ask + put_ask

    def _combined_spread_pct(self) -> float | None:
        cq, pq = self.quotes.get(self.call_id), self.quotes.get(self.put_id)
        if cq is None or pq is None:
            return None
        bid = float(cq.bid_price) + float(pq.bid_price)
        ask = float(cq.ask_price) + float(pq.ask_price)
        if bid <= 0 or ask <= 0:
            return None
        return (ask / bid - 1.0) * 100.0

    def _finish(self, state: State) -> None:
        realized = sum(self.sell_notional[i] - self.buy_notional[i] for i in self._legs())
        self._set_state(state)
        self.log.info(
            f"STRADDLE FINISHED ({self.exit_reason or state.value}). "
            f"Realized PnL (before fees): {realized:.4f} {self.config.settle_coin}",
        )
        self._log_status(force=True)

    def _log_status(self, force: bool = False) -> None:
        now = self.clock.timestamp_ns()
        if not force and now - self.last_status_ns < self.config.status_interval_secs * NANOS:
            return
        self.last_status_ns = now

        parts = [f"[STATUS] state={self.state.value}"]
        if self.ref_price:
            parts.append(f"BTC={self.ref_price:,.2f}")
        for name, iid in (("CALL", self.call_id), ("PUT", self.put_id)):
            if iid is None:
                continue
            q = self.quotes.get(iid)
            bid_ask = f"{q.bid_price}/{q.ask_price}" if q else "-/-"
            venue_pos = sum(
                (Decimal(str(p.quantity)) for p in self.cache.positions_open(instrument_id=iid, strategy_id=self.id)),
                ZERO,
            )
            parts.append(f"{name} bid/ask={bid_ask} short={self.short_qty.get(iid, ZERO)} (cache {venue_pos})")

        if self.levels is not None and self.state in (State.ACTIVE, State.EXITING):
            buyback = self._buyback_cost()
            if buyback is not None:
                parts.append(
                    f"entry={self.levels.entry_premium:.2f} buyback={buyback:.2f} "
                    f"({change_pct(self.levels.entry_premium, buyback):+.2f}%) "
                    f"uPnL={unrealized_pnl(self.levels.entry_premium, buyback, float(self.position_qty)):.4f}",
                )
            if self.config.enable_risk_management:
                parts.append(f"SL>={self.levels.stop_loss_level:.2f} TP<={self.levels.take_profit_level:.2f}")
        self.log.info(" | ".join(parts))

    def _warn_about_existing_positions(self) -> None:
        for pos in self.cache.positions_open(venue=self.venue):
            self.log.warning(
                f"Existing open position found on start (NOT managed by this run): "
                f"{pos.instrument_id} {pos.side.name} {pos.quantity}",
            )

    def _set_state(self, state: State) -> None:
        if state != self.state:
            self.log.info(f"STATE {self.state.value} -> {state.value}")
        self.state = state
        self.state_since_ns = self.clock.timestamp_ns()

    def _warn_throttled(self, key: str, msg: str, every_secs: float = 30.0) -> None:
        now = self.clock.timestamp_ns()
        if now - self._last_warn_ns.get(key, 0) >= every_secs * NANOS:
            self._last_warn_ns[key] = now
            self.log.warning(msg)

    def _legs(self) -> list[InstrumentId]:
        return [i for i in (self.call_id, self.put_id) if i is not None]

    def _inst(self, iid: InstrumentId):
        return self.call_inst if iid == self.call_id else self.put_inst
