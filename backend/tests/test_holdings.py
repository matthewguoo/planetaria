"""The account overview's one list: every broker position stamped with the
plan that protects it — or not. On a live account `protected: False` is
the row that matters."""

from app.services.trade_service import merge_holdings


def _pos(symbol, qty=100.0, occ=None, **extra):
    row = {
        "symbol": symbol, "qty": qty, "side": 1 if qty > 0 else -1,
        "asset_class": "option" if occ else "stock", "avg_entry_price": 10.0,
        "current_price": 11.0, "market_value": 1100.0, "unrealized_pl": 100.0,
        "unrealized_plpc": 0.1, "unrealized_intraday_pl": 20.0, "change_today": 0.02,
        "lastday_price": 10.8, "cost_basis": 1000.0, "occ": occ,
    }
    row.update(extra)
    return row


def test_managed_positions_carry_their_plan_and_protection():
    plans = [{
        "id": "plan-1", "status": "filled", "sl_premium": 9.0, "tp_premium": 14.0,
        "time_stop_utc": "2026-10-01T19:55:00Z",
        "legs": [{"symbol": "AAPX", "side": 1}],
    }, {
        "id": "plan-2", "status": "filled", "sl_premium": None, "tp_premium": None,
        "time_stop_utc": "2026-10-01T19:55:00Z",
        "legs": [{"symbol": "SPCU", "side": 1}],
    }]
    rows = merge_holdings([_pos("AAPX"), _pos("SPCU"), _pos("PLTZ")], plans)
    by = {r["symbol"]: r for r in rows}
    assert by["AAPX"]["plan_id"] == "plan-1" and by["AAPX"]["protected"] is True
    assert by["AAPX"]["sl"] == 9.0 and by["AAPX"]["tp"] == 14.0
    # a stopless plan is NOT protection
    assert by["SPCU"]["plan_id"] == "plan-2" and by["SPCU"]["protected"] is False
    assert by["PLTZ"]["plan_id"] is None and by["PLTZ"]["protected"] is False
    assert by["PLTZ"]["underlying"] == "PLTZ"


def test_options_group_under_their_underlying():
    occ = {"underlying": "SPY", "expiry": "2026-09-18", "right": "C", "strike": 600.0}
    rows = merge_holdings([_pos("SPY260918C00600000", qty=1.0, occ=occ)], [])
    assert rows[0]["underlying"] == "SPY"
    assert rows[0]["asset_class"] == "option"
    assert rows[0]["protected"] is False


def test_broker_fields_pass_through_untouched():
    rows = merge_holdings([_pos("AVGG", change_today=-0.031)], [])
    assert rows[0]["change_today"] == -0.031
    assert rows[0]["unrealized_intraday_pl"] == 20.0
