"""Does the paper API accept on-close orders (TIF=cls)? The close_fade
candidate's execution gate — MOC entry is the whole strategy shape.

House pattern (verify_equity_paths / verify_short_paths): submit the
smallest real probe, record the broker's verbatim answer, cancel
immediately. Probes: (1) 1-share SPY MOC (market, tif=cls); (2) 1-share
SPY LOC at a $400 limit (cannot fill against a ~$570 close even if the
cancel raced). Both cancelled on acceptance; run outside RTH so nothing
can execute before the cancel lands.

Run: python scripts/verify_cls_tif.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpaca.trading.client import TradingClient  # noqa: E402
from alpaca.trading.enums import OrderSide, TimeInForce  # noqa: E402
from alpaca.trading.requests import (  # noqa: E402
    LimitOrderRequest,
    MarketOrderRequest,
)

from app.config import get_settings  # noqa: E402


def probe(client: TradingClient, label: str, req) -> None:
    print(f"\n--- {label} ---")
    try:
        o = client.submit_order(req)
    except Exception as exc:                              # noqa: BLE001
        print(f"REJECTED at submit: {exc}")
        return
    print(f"ACCEPTED: id={o.id} status={o.status} tif={o.time_in_force} "
          f"type={o.order_type} qty={o.qty}")
    try:
        client.cancel_order_by_id(o.id)
        refreshed = client.get_order_by_id(o.id)
        print(f"cancel -> status={refreshed.status}")
    except Exception as exc:                              # noqa: BLE001
        print(f"CANCEL FAILED (manual cleanup needed, id={o.id}): {exc}")


def main() -> None:
    s = get_settings()
    client = TradingClient(s.alpaca_api_key, s.alpaca_secret_key,
                           paper=True)
    clock = client.get_clock()
    print(f"market open now: {clock.is_open} (probe intended for closed "
          "hours; nothing can fill before the cancel)")
    probe(client, "MOC: market + TIF=cls",
          MarketOrderRequest(symbol="SPY", qty=1, side=OrderSide.BUY,
                             time_in_force=TimeInForce.CLS))
    probe(client, "LOC: limit 400.00 + TIF=cls",
          LimitOrderRequest(symbol="SPY", qty=1, side=OrderSide.BUY,
                            time_in_force=TimeInForce.CLS,
                            limit_price=400.00))


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
