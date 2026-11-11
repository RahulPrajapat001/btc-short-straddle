# Walkthrough — how this project works

Read this before the review call. It explains the full execution flow and
answers the 10 questions from the assignment, pointing to the exact code.

---

## 0. The strategy in one paragraph

A **short straddle** = sell an ATM **Call** + sell an ATM **Put** (same strike,
same expiry). We collect two premiums up front. We make money if BTC stays near
the strike (both options lose value over time), and we lose money if BTC moves
sharply in either direction. To close, we **buy back** both options.

---

## 1. The big picture

```
        Bybit                               NautilusTrader (one process)
 ┌───────────────────┐        ┌──────────────────────────────────────────────────┐
 │ REST: instruments │◄──────►│ BybitDataClient ──► DataEngine ──┐               │
 │ WS public: quotes │───────►│                                  │  MessageBus   │
 │                   │        │                                  ▼               │
 │ REST/WS: orders   │◄───────│ BybitExecClient ◄── ExecEngine ◄─ RiskEngine     │
 │ WS private: fills │───────►│        │              │          ▲               │
 └───────────────────┘        │        └─events──► ExecEngine ───┤               │
                              │                       │          │               │
                              │               Cache (instruments, orders,        │
                              │                      positions) + Portfolio      │
                              │                                  │               │
                              │                    ShortStraddle strategy        │
                              │      on_quote_tick / on_order_* / on_position_*  │
                              └──────────────────────────────────────────────────┘
```

Everything is **event-driven**: the strategy never polls the exchange. It
reacts to callbacks (`on_quote_tick`, `on_order_filled`, ...) and to a 1-second
timer.

---

## 2. Files

| File | What's inside |
|---|---|
| `run.py` | Reads `.env` + `config.toml`, builds the `TradingNode` with Bybit data/exec clients, adds the strategy, runs it. |
| `straddle/strategy.py` | The `ShortStraddle` strategy: a state machine (see §4). |
| `straddle/selection.py` | Parses option symbols and picks the ATM strike + expiry. No Nautilus imports, so it is unit-testable. |
| `straddle/risk.py` | Entry premium, SL/TP levels, exit check, tick rounding. Also pure Python. |
| `tests/` | Unit tests + offline simulation of the state machine. |

---

## 3. Execution flow, start to finish

1. `python run.py --level 2`
2. `run.py` loads keys from `.env`, builds `BybitDataClientConfig` (product types
   **OPTION + LINEAR**) and `BybitExecClientConfig` (product type **OPTION**),
   with `testnet=True` or `demo=True`.
3. `TradingNode(config)` creates the core components: MessageBus, Cache, Clock,
   DataEngine, RiskEngine, ExecutionEngine, Portfolio, Trader.
4. `node.add_data_client_factory / add_exec_client_factory(BYBIT, ...)` registers
   how to create the Bybit clients; `node.build()` creates them.
5. `node.run()` starts the asyncio event loop:
   - clients **connect** (HTTP + WebSockets, authenticated with the API key);
   - the **instrument provider** loads every option + linear instrument into the **Cache** (`load_all=True`);
   - **reconciliation** pulls existing orders/positions from Bybit so Nautilus' state matches the exchange;
   - the Trader starts the strategy → **`on_start()`**.
6. `on_start()` subscribes to **BTCUSDT-LINEAR** quotes (the reference price) and starts a 1-second timer.
7. First BTC quote → `on_quote_tick()` → reference price = mid → **`_select_straddle()`**:
   - reads all `*-OPTION` instruments from the cache;
   - parses `BTC-13MAR26-70000-C-OPTION` → base, expiry, strike, kind, settle coin;
   - picks the **nearest expiry** that has both a call and a put at the same strike, then the **strike closest to the BTC price**;
   - subscribes to quotes for the chosen call and put.
8. When both legs have a bid → **`_try_enter()`** creates two **SELL limit orders at the bid** and submits them. State → `ENTERING`.
9. Bybit sends back acks and fills → `on_order_accepted`, `on_order_filled`.
   When both entry orders are finished:
   - equal fills → `ACTIVE` (entry premium = avg call price + avg put price);
   - unequal / one leg only → `REBALANCING` (see Q10).
10. **Level 2:** on every leg quote, `_check_risk()` computes
    `buyback = call ask + put ask` and compares it to the entry premium:
    `>= +1%` → STOP_LOSS, `<= -2%` → TAKE_PROFIT → state `EXITING`.
11. Exit: reduce-only **BUY** orders at the ask; re-priced every 15 s if not filled.
    When both legs are flat → `DONE`, and the realized PnL is printed.

---

## 4. The state machine (`straddle/strategy.py`)

```
WAITING_FOR_REFERENCE ──BTC price──► WAITING_FOR_LEG_QUOTES ──bids on both──► ENTERING
                                                                                  │
                  ┌───────────── both legs filled equally ◄───────────────────────┤
                  ▼                                                               │ unequal / one leg
               ACTIVE ◄──────────── legs equal again ─────────── REBALANCING ◄────┘
                  │ (Level 2) SL or TP                                 │ nothing left
                  ▼                                                    ▼
               EXITING ──both legs flat──► DONE                      FAILED
```

Why a state machine: every event (quote, fill, cancel, timer) can arrive at
any time. The state decides what each event is allowed to do. For example, a
quote in `ACTIVE` checks SL/TP, while the same quote in `EXITING` only helps
re-send a buy-back. The check `if self.state != State.ACTIVE: return` in
`_start_exit()` is what makes a **second exit impossible**.

---

## 5. The 10 review questions

### Q1. How does NautilusTrader start and initialize?
`run.py` builds a `TradingNodeConfig` (trader id, logging, exec engine with
reconciliation, Bybit data + exec client configs) and creates a `TradingNode`.
The node creates the kernel (MessageBus, Cache, Clock, Data/Risk/Execution
engines, Portfolio, Trader). We register the Bybit factories, call
`node.build()` to create the clients, then `node.run()`. That connects the
clients, loads instruments, reconciles state with Bybit, and finally calls the
strategy's `on_start()`. `Ctrl+C` → `on_stop()` → `node.dispose()`.

### Q2. How is the Bybit integration configured?
In `run.py`:
- `BybitDataClientConfig(product_types=[OPTION, LINEAR], testnet=..., demo=..., instrument_provider=InstrumentProviderConfig(load_all=True))`.
  OPTION gives us the options; LINEAR gives us the BTCUSDT perpetual for the reference price.
- `BybitExecClientConfig(product_types=[OPTION], ...)` sends orders to Bybit's `option` category.
- Keys come from `.env` (`BYBIT_API_KEY`, `BYBIT_API_SECRET`); `BYBIT_ENV` chooses testnet or demo. Mainnet is blocked in `run.py`.

### Q3. How does NautilusTrader receive market data?
The Bybit **data client** keeps a public WebSocket open to Bybit. Incoming
messages are parsed into Nautilus objects (`QuoteTick` = best bid/ask + sizes),
passed to the **DataEngine**, and published on the **MessageBus** to every
subscriber. Instrument definitions (strike, expiry, tick size, min qty) come
from Bybit's REST API at startup.

### Q4. How are BTC option instruments identified and subscribed to?
At startup, all option instruments are loaded into the **Cache**. In
`_select_straddle()` we read `self.cache.instruments(venue=BYBIT)`, keep symbols
ending in `-OPTION`, and parse them in `selection.py`:
`BTC-13MAR26-70000-C-OPTION` → base `BTC`, expiry `13MAR26`, strike `70000`,
kind `C`, settle `USDC` (USDT options have `-USDT-` before `-OPTION`).
`choose_atm_straddle()` picks the nearest valid expiry and the strike closest to
the BTC price, requiring **both** a call and a put at that strike. Then
`subscribe_quote_ticks(call_id)` and `subscribe_quote_ticks(put_id)` tell the
data client to subscribe on the WebSocket.

### Q5. How does the strategy receive market data events?
Through callbacks. Nautilus calls `on_quote_tick(tick)` for every quote of
every instrument we subscribed to. Our `on_quote_tick` checks
`tick.instrument_id`:
- **reference** → update BTC mid price (and pick the straddle the first time);
- **call/put** → store the quote, then depending on state: try to enter, check SL/TP, or continue a buy-back.

### Q6. How are orders submitted to Bybit?
`_make_order()` uses `self.order_factory.limit(...)` (or `.market(...)`) with
the instrument id, side, quantity and price. Price and quantity are snapped to
the instrument's tick/lot size (`round_to_increment`). Then
`self.submit_order(order)` → **RiskEngine** pre-trade checks → **ExecutionEngine**
→ **Bybit execution client** → Bybit order API (category `option`).
Entry = SELL at bid. Exits = BUY at ask with `reduce_only=True`.

### Q7. How does the system know whether an order was accepted or executed?
Bybit pushes order and execution updates on its **private WebSocket**. The
execution client turns them into Nautilus events, `OrderAccepted`,
`OrderRejected`, `OrderCanceled`, `OrderFilled` (one per fill, so partial fills
arrive as several events). The ExecutionEngine updates the order in the Cache
(status, filled qty, avg price) and calls our handlers: `on_order_accepted`,
`on_order_filled`, etc. We also ask the cache, e.g.
`self.cache.order(id).is_closed`, to know if an order is finished.

### Q8. How are positions tracked?
Two layers:
1. **Nautilus:** the ExecutionEngine applies every fill to a `Position` object
   (Bybit options are one-way = NETTING, so one position per instrument). It
   emits `PositionOpened/Changed/Closed` (we log them) and the Portfolio
   tracks PnL.
2. **Our own fill ledger** (`self.short_qty`, `sell_notional`, `buy_notional`),
   updated in `on_order_filled`. Buy-back sizes come from this ledger, so they
   are exact and don't depend on when the cached position is updated relative
   to our callback. The `[STATUS]` line prints both (`short=...` and
   `(cache ...)`) so you can see they agree.

### Q9. How are the Stop-Loss and Take-Profit implemented?
- **Entry premium** (per contract) = average SELL price of the call + average SELL price of the put.
  Both legs always have the same quantity, so per-contract numbers are consistent.
- **Buy-back cost** = current **ask** of call + **ask** of put (closing a short means buying, and you buy at the ask).
- `SL level = entry × 1.01`, `TP level = entry × 0.98` (`risk.py → risk_levels`).
- Checked on **every** leg quote in `_check_risk()` → `check_exit()`. A zero or missing ask never triggers.
- On trigger, `_start_exit()` sets state `EXITING`, sets each leg's **target quantity to 0**, and calls `_drive_all()`.
- `_drive_leg()` sends a reduce-only BUY for `(current short − target)`, with **at most one open buy-back per leg**.
  If it isn't filled within `order_reprice_secs`, the timer cancels it; the cancel event re-enters `_drive_leg()`, which sends a new order at the new ask for only the **remaining** quantity.
- **Duplicate exits are prevented** by: (1) state check in `_start_exit`, (2) one open buy-back per leg, (3) sizing from the fill ledger, (4) `reduce_only=True`, so Bybit itself refuses to flip us long.

**Important observation:** we sell at the bid but value at the ask, so the
**spread is an instant paper loss**. If the spread is more than 1%, the SL fires
immediately. The strategy warns about this, and `max_entry_spread_pct` lets you
only enter in tight markets.

### Q10. What happens when one leg executes but the other doesn't?
This is "legging risk": a lone short call or put is not a straddle, and its
risk is one-sided.
1. Entry orders get `entry_timeout_secs` (60 s) to fill.
2. Then the timer **cancels** whatever is still open.
3. When both entry orders are finished, `_progress_entry()` compares filled quantities:
   - **equal** → `ACTIVE`;
   - **unequal** (e.g. call 0.05, put 0.03) → target both legs to **0.03**, buy back the extra 0.02 call → `REBALANCING` → `ACTIVE` with a balanced straddle;
   - **one leg zero** → target both to 0 → buy back the filled leg → `FAILED` (flat, no naked option left).
4. The buy-back uses the same `_drive_leg()` mechanism (reduce-only, re-pricing, retries on reject).

This happens in Level 1 too, because a one-legged position is an execution
problem, not a stop-loss.

---

## 6. Design decisions worth mentioning

- **Limit orders at the touch, not market orders by default:** option books are thin, and a market order can fill far from fair value. `use_market_orders = true` is available.
- **Reference price = BTCUSDT perpetual mid:** liquid and always streaming. (Bybit's options actually settle on the BTC index; the perp is a close proxy.)
- **Nearest expiry by default:** most liquid; `min_time_to_expiry_hours` avoids options about to expire.
- **Pure logic split out** (`selection.py`, `risk.py`) so it can be tested without an exchange.
- **Mainnet disabled** in `run.py`, since this is an assignment.

## 7. Limitations / what I'd do next

- Fees aren't in the strategy's PnL.
- A restart doesn't adopt an already-open straddle (only warns). Next step: rebuild state from reconciled positions.
- SL/TP is on the ask; a mark-price or mid-based trigger with a confirmation delay would avoid spread-driven exits.
- Add Greeks (delta/vega) monitoring, or turn it into an iron fly with protective wings to cap risk.

## 8. How it was tested

- `tests/test_logic.py`: symbol parsing, ATM choice, expiry/settle filtering, SL/TP thresholds, tick rounding.
- `tests/simulate_state_machine.py`: runs the real strategy class against fake Nautilus objects and replays happy path + TP, SL with partial exit fill + re-price, one-leg fill, unequal partials + rejected buy-back retry, Level 1 no-exit, and wide spread.
- Live: run on Bybit testnet/demo with `--level 1`, then `--level 2`, and read the `[STATUS]` lines and the logs.
