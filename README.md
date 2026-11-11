# BTC ATM Short Straddle — NautilusTrader + Bybit

A NautilusTrader strategy that connects to Bybit, finds the **at-the-money (ATM)
BTC option strike**, and **sells one Call + one Put** with the same strike and
expiry (a *short straddle*).

| Level | What it does | Command |
|---|---|---|
| **1** | Sell the straddle and track orders, fills and positions. No automatic exits. | `python run.py --level 1` |
| **2** | Level 1 + automatic **Stop-Loss (1%)** and **Take-Profit (2%)** on the combined premium, partial-fill handling, and no duplicate exits. | `python run.py --level 2` |

> For the full explanation of how everything works (the 10 interview questions),
> read **[WALKTHROUGH.md](WALKTHROUGH.md)**.

---

## 1. Project structure

```
btc-short-straddle/
├── run.py                  <- START HERE: builds the TradingNode and runs it
├── config.toml             <- strategy settings (qty, expiry, SL/TP %, ...)
├── .env.example            <- template for your Bybit API keys
├── requirements.txt        <- nautilus_trader==1.231.0 (pinned)
├── straddle/
│   ├── strategy.py         <- the NautilusTrader Strategy (state machine)
│   ├── selection.py        <- ATM strike / expiry selection (pure Python)
│   └── risk.py             <- SL/TP maths, tick rounding (pure Python)
├── tests/
│   ├── test_logic.py              <- unit tests for selection + risk
│   └── simulate_state_machine.py  <- offline replay of fills / exits / edge cases
├── README.md
└── WALKTHROUGH.md          <- how it works, Q&A for the review call
```

---

## 2. Requirements

- **Python 3.12 or 3.13** (64-bit). Check with `python --version`.
- Windows, macOS or Linux.
- A **Bybit Testnet** account (free, fake money). Bybit **Demo Trading** also works, see 4B.

---

## 3. Install

Open a terminal **inside the project folder** (the folder that has `run.py`).

**Windows (PowerShell / CMD)**
```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

**macOS / Linux**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

You should see `(.venv)` at the start of the terminal line. Each time you open a
new terminal, activate the venv again (the `activate` line) before running.

> **Why the version is pinned:** NautilusTrader's API changes between releases.
> In v2.0 the Bybit configs are renamed (`BybitExecutionClientConfig`,
> `environment=...` instead of `testnet=True`). This project targets **1.231.0**.
> Do not upgrade unless you also update `run.py`.

---

## 4. Get Bybit API keys

Never use real-money (mainnet) keys. `run.py` refuses to run on mainnet on purpose.

### 4A. Testnet (recommended)

1. Go to **https://testnet.bybit.com** and **sign up**. This is a separate
   account from normal bybit.com, so you need to register again.
2. **Get test money:** go to **Assets** and click **"Request Test Coins"** (the faucet).
   You need **USDC** for the default USDC-settled BTC options. If you only get USDT,
   either convert/transfer to USDC on the testnet site, or set
   `settle_coin = "USDT"` in `config.toml` to trade the USDT-settled options.
3. Make sure the account is a **Unified Trading Account (UTA)**, since options need it.
   New testnet accounts normally are. If the site asks you to upgrade, accept.
4. **Create the API key:** click your **profile icon → API → Create New Key →
   System-generated API Keys**.
   - Usage: **API Transaction**
   - Permissions: **Read-Write**
   - Tick **Unified Trading → Orders** and **Positions** (and **Options**, if shown)
   - IP restriction: "No IP restriction" is fine for testnet
5. Copy the **API Key** and the **API Secret**. **The secret is shown only once.**

### 4B. Demo Trading (alternative: real market prices, fake money)

Testnet option order books can be **empty or very thin**. If no fills happen on
testnet, use Bybit **Demo Trading**, which uses real mainnet prices:

1. Log in to normal **bybit.com**, then switch to **Demo Trading** (top-right menu).
2. In Demo Trading, hover your avatar, click **API**, and create a key with the same
   permissions as above.
3. Demo funds are added from the Demo Trading page.
4. In `.env` set `BYBIT_ENV=demo`.

---

## 5. Put the keys in `.env`

1. Make a copy of `.env.example` and name it **`.env`**:
   - Windows: `copy .env.example .env`
   - macOS/Linux: `cp .env.example .env`
2. Open `.env` in any text editor and fill it in:

```ini
BYBIT_ENV=testnet
BYBIT_API_KEY=paste_your_key_here
BYBIT_API_SECRET=paste_your_secret_here
```

No quotes, no spaces. Keep `.env` private: don't upload it or send it to anyone.

---

## 6. Run

```bash
python run.py --level 1     # naked straddle
python run.py --level 2     # with 1% SL / 2% TP
```

Optional: `--env demo` overrides `BYBIT_ENV` for one run.

**Stop:** press **Ctrl+C**. Stopping cancels open orders but **does not close
positions** (Level 1 rule). Close them on the Bybit website, or set
`close_positions_on_stop = true` in `config.toml`.

Logs are printed to the terminal and saved in the `logs/` folder.

### What you should see

```
STATE WAITING_FOR_REFERENCE -> WAITING_FOR_LEG_QUOTES
ATM selected: ref=70,401.00 -> strike=70,000 expiry=13MAR26 | CALL=BTC-13MAR26-70000-C-OPTION.BYBIT | PUT=...
STATE WAITING_FOR_LEG_QUOTES -> ENTERING
SUBMIT ENTRY LimitOrder(SELL 0.01 BTC-13MAR26-70000-C-OPTION.BYBIT ...)
ACK  O-...-1 accepted by Bybit (venue id ...)
FILL ENTRY SELL 0.01 BTC-13MAR26-70000-C-OPTION.BYBIT @ 1000.0 -> short now 0.01
POSITION OPENED  BTC-13MAR26-70000-C-OPTION.BYBIT SHORT qty=0.01 ...
STRADDLE OPEN: short 0.01 x strike 70,000 | call avg 1000.00 + put avg 900.00 = entry premium 1900.00 | SL if buyback >= 1919.00, TP if <= 1862.00
[STATUS] state=ACTIVE | BTC=70,512.00 | CALL bid/ask=1040/1050 ... | entry=1900.00 buyback=1932.00 (+1.68%) uPnL=-0.3200 | SL>=1919.00 TP<=1862.00
STOP_LOSS TRIGGERED: buyback 1932.00 vs entry 1900.00 (+1.68%). Closing both legs.
SUBMIT BUYBACK (attempt 1) LimitOrder(BUY 0.01 ... reduce_only=True)
STRADDLE FINISHED (STOP_LOSS). Realized PnL (before fees): -0.3200 USDC
```

---

## 7. Settings (`config.toml`)

| Setting | Default | Meaning |
|---|---|---|
| `reference_instrument` | `BTCUSDT-LINEAR.BYBIT` | BTC price used to pick the ATM strike (mid of bid/ask) |
| `settle_coin` | `USDC` | Which BTC option family: `USDC` or `USDT` |
| `expiry` | `""` | Force an expiry like `"27MAR26"`; empty = nearest |
| `min_time_to_expiry_hours` | `1.0` | Skip options that expire very soon |
| `quantity` | `0.01` | Contracts per leg (min 0.01 BTC) |
| `use_market_orders` | `false` | `false` = limit at best bid/ask; `true` = market (IOC) |
| `entry_slippage_pct` / `exit_slippage_pct` | `0.0` | Make limits more aggressive by x% |
| `max_entry_spread_pct` | `0.0` (off) | Only enter if combined spread is at most x% |
| `entry_timeout_secs` | `60` | Cancel unfilled entry orders after this |
| `order_reprice_secs` | `15` | Re-price a resting buy-back after this |
| `max_buyback_attempts` | `20` | Per leg; then it stops and asks for manual action |
| `status_interval_secs` | `10` | How often the `[STATUS]` line prints |
| `close_positions_on_stop` | `false` | Close positions on Ctrl+C |
| `stop_loss_pct` / `take_profit_pct` | `1.0` / `2.0` | Level 2 thresholds |

---

## 8. Important: Spread vs 1% stop-loss

We **sell at the bid** but value the buy-back at the **ask** (as the assignment
asks). So right after entry, the buy-back cost is already higher than the entry
premium by the **bid/ask spread**. BTC option spreads are often **more than 1%**,
which means the Level 2 stop-loss can trigger **within seconds**, from the
spread alone and not from a market move.

The strategy logs a warning when this is the case. To avoid it, set e.g.
`max_entry_spread_pct = 0.5` so it only enters when the market is tight. This is
a real trade-off worth discussing in the review.

---

## 9. Troubleshooting

| Problem | Fix |
|---|---|
| `Could not import NautilusTrader` / `cannot import name ...` | Activate the venv and run `pip install -r requirements.txt`. Check `pip show nautilus_trader` says **1.231.0**. |
| `BYBIT_API_KEY / BYBIT_API_SECRET missing` | `.env` doesn't exist or still has placeholder text (see step 5). |
| Auth / signature / `10003` / `10004` errors | Wrong key or secret, or a **testnet key used with `BYBIT_ENV=demo`** (or the reverse). Each environment needs its own key. |
| `No ATM straddle found` | No expiry with both a call and put at the same strike. Try `settle_coin = "USDT"`, or check the log line listing available expiries. |
| Stuck at `Still waiting for bid/ask` / `no bid yet` | Testnet option book is empty. Try **Demo Trading** (4B) or another `expiry`. |
| Orders `REJECTED` (insufficient margin / balance) | Get more test USDC/USDT; make sure the account is **Unified**. |
| Entry orders never fill | Use `use_market_orders = true`, or add `entry_slippage_pct = 2`. |
| `Existing open position found on start` | A position from an earlier run is still open. This run does **not** manage it, so close it on the website. |
| Something else | Check the full log in `logs/` (DEBUG level), then search the error in the [Bybit integration docs](https://nautilustrader.io/docs/latest/integrations/bybit/). |

---

## 10. Tests (no internet / no Bybit needed)

```bash
python tests/test_logic.py               # strike selection + SL/TP maths
python tests/simulate_state_machine.py   # replays fills, partial fills, one-leg fills, SL/TP, retries
```

The simulation replaces NautilusTrader with small fake classes and checks:
happy path + take-profit, stop-loss with a partial exit fill and re-pricing,
only one leg filling, unequal partial fills, Level 1 never exiting, and the
wide-spread case.

---

## 11. Known limitations

- Fees are not included in the PnL printed by the strategy (Bybit's account shows the real PnL).
- If the program is restarted while a straddle is open, the new run does **not** take over those positions. It only warns about them.
- No hedging, Greeks or IV logic. This is a deliberately simple straddle.
- Written against the NautilusTrader **1.231** API, and not tested live from this
  environment. If an attribute or name error appears, it is most likely a version
  difference. Check `pip show nautilus_trader`.

## Resources

- NautilusTrader docs: https://nautilustrader.io/docs/latest/
- Bybit integration: https://nautilustrader.io/docs/latest/integrations/bybit/
- Bybit demo trading: https://bybit-exchange.github.io/docs/v5/demo
- Straddle explained: https://www.investopedia.com/terms/s/straddle.asp
