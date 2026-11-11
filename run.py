"""
Entry point: builds a NautilusTrader TradingNode connected to Bybit and runs
the BTC ATM short straddle.

    python run.py --level 1      # naked straddle, no automated exits
    python run.py --level 2      # + 1% stop-loss / 2% take-profit

Press Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLACEHOLDERS = {"", "your_api_key_here", "your_api_secret_here"}


def load_dotenv(path: Path) -> None:
    """Tiny .env loader (KEY=VALUE lines) so we don't need an extra package."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BTC ATM short straddle on Bybit (NautilusTrader)")
    p.add_argument("--level", type=int, choices=[1, 2], default=1,
                   help="1 = naked straddle, 2 = with stop-loss / take-profit")
    p.add_argument("--config", default=str(ROOT / "config.toml"), help="path to config.toml")
    p.add_argument("--env", choices=["testnet", "demo"], default=None,
                   help="override BYBIT_ENV from .env")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv(ROOT / ".env")

    with open(args.config, "rb") as f:
        settings = tomllib.load(f)

    env = (args.env or os.environ.get("BYBIT_ENV", "testnet")).lower()
    if env not in ("testnet", "demo"):
        sys.exit(f"BYBIT_ENV must be 'testnet' or 'demo' (got '{env}'). Mainnet is disabled on purpose.")

    api_key = os.environ.get("BYBIT_API_KEY", "")
    api_secret = os.environ.get("BYBIT_API_SECRET", "")
    if api_key in PLACEHOLDERS or api_secret in PLACEHOLDERS:
        sys.exit(
            "BYBIT_API_KEY / BYBIT_API_SECRET missing.\n"
            "Copy .env.example to .env and paste your Bybit keys there (see README.md).",
        )

    # Imported here so the error messages above work even if nautilus isn't installed yet.
    try:
        from nautilus_trader.adapters.bybit import BYBIT
        from nautilus_trader.adapters.bybit import BybitDataClientConfig
        from nautilus_trader.adapters.bybit import BybitExecClientConfig
        from nautilus_trader.adapters.bybit import BybitLiveDataClientFactory
        from nautilus_trader.adapters.bybit import BybitLiveExecClientFactory
        from nautilus_trader.adapters.bybit import BybitProductType
        from nautilus_trader.config import InstrumentProviderConfig
        from nautilus_trader.config import LiveExecEngineConfig
        from nautilus_trader.config import LoggingConfig
        from nautilus_trader.config import TradingNodeConfig
        from nautilus_trader.live.node import TradingNode
        from nautilus_trader.model.identifiers import TraderId
    except ImportError as e:
        sys.exit(
            f"Could not import NautilusTrader ({e}).\n"
            "Install the pinned version:  pip install -r requirements.txt",
        )

    from straddle.strategy import ShortStraddle
    from straddle.strategy import ShortStraddleConfig

    bybit = settings.get("bybit", {})
    strat = settings.get("strategy", {})
    risk = settings.get("risk", {})

    is_testnet = env == "testnet"
    is_demo = env == "demo"

    data_cfg = BybitDataClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        # OPTION = the contracts we trade, LINEAR = BTCUSDT perp for the reference price
        product_types=[BybitProductType.OPTION, BybitProductType.LINEAR],
        instrument_provider=InstrumentProviderConfig(load_all=True),
        testnet=is_testnet,
        demo=is_demo,
    )
    exec_cfg = BybitExecClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        product_types=[BybitProductType.OPTION],
        instrument_provider=InstrumentProviderConfig(load_all=True),
        testnet=is_testnet,
        demo=is_demo,
    )

    node_cfg = TradingNodeConfig(
        trader_id=TraderId("STRADDLE-001"),
        logging=LoggingConfig(
            log_level="INFO",
            log_level_file="DEBUG",
            log_directory=str(ROOT / "logs"),
        ),
        exec_engine=LiveExecEngineConfig(
            reconciliation=True,               # pull existing orders/positions from Bybit at start
            reconciliation_lookback_mins=1440,
        ),
        data_clients={BYBIT: data_cfg},
        exec_clients={BYBIT: exec_cfg},
        timeout_connection=60.0,
        timeout_reconciliation=20.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
        timeout_post_stop=5.0,
    )

    strategy_cfg = ShortStraddleConfig(
        reference_instrument_id=bybit.get("reference_instrument", "BTCUSDT-LINEAR.BYBIT"),
        base_currency=strat.get("base_currency", "BTC"),
        settle_coin=strat.get("settle_coin", "USDC"),
        expiry=strat.get("expiry", ""),
        min_time_to_expiry_hours=float(strat.get("min_time_to_expiry_hours", 1.0)),
        quantity=float(strat.get("quantity", 0.01)),
        use_market_orders=bool(strat.get("use_market_orders", False)),
        entry_slippage_pct=float(strat.get("entry_slippage_pct", 0.0)),
        exit_slippage_pct=float(strat.get("exit_slippage_pct", 0.0)),
        max_entry_spread_pct=float(strat.get("max_entry_spread_pct", 0.0)),
        entry_timeout_secs=float(strat.get("entry_timeout_secs", 60)),
        order_reprice_secs=float(strat.get("order_reprice_secs", 15)),
        max_buyback_attempts=int(strat.get("max_buyback_attempts", 20)),
        status_interval_secs=float(strat.get("status_interval_secs", 10)),
        enable_risk_management=(args.level == 2),
        stop_loss_pct=float(risk.get("stop_loss_pct", 1.0)),
        take_profit_pct=float(risk.get("take_profit_pct", 2.0)),
        close_positions_on_stop=bool(strat.get("close_positions_on_stop", False)),
    )

    print(f"Environment: {env.upper()} | Level: {args.level} | Logs: {ROOT / 'logs'}")

    node = TradingNode(config=node_cfg)
    node.trader.add_strategy(ShortStraddle(config=strategy_cfg))
    node.add_data_client_factory(BYBIT, BybitLiveDataClientFactory)
    node.add_exec_client_factory(BYBIT, BybitLiveExecClientFactory)
    node.build()

    try:
        node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
