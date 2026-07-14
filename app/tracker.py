"""Position tracker.

Re-prices every open tracked position against the CLOB, computes unrealized
P&L, detects market resolution (the CLOB market payload marks the winning
token), realizes P&L on close, and appends portfolio snapshots for the
performance chart. Runs from the scheduler; dry-run and live positions are
tracked identically but bucketed separately.
"""

import time

import requests

from . import mirror, store

CLOB_API = "https://clob.polymarket.com"
_market_cache: dict[str, dict] = {}


def _fetch_market(condition_id: str) -> dict | None:
    try:
        r = requests.get(f"{CLOB_API}/markets/{condition_id}", timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def _token_state(pos: dict, market: dict) -> tuple[float | None, bool, bool]:
    """Returns (price, market_closed, this_token_won)."""
    tokens = market.get("tokens") or []
    token = None
    if pos.get("token_id"):
        token = next((t for t in tokens if t.get("token_id") == pos["token_id"]), None)
    if token is None and pos.get("outcome_index") is not None \
            and pos["outcome_index"] < len(tokens):
        token = tokens[pos["outcome_index"]]
    if token is None:
        return None, False, False
    price = token.get("price")
    return (float(price) if price is not None else None,
            bool(market.get("closed")),
            bool(token.get("winner")))


def refresh_positions() -> dict:
    """One tracking pass over all open positions. Returns counts for logging."""
    positions = store.open_positions()
    _market_cache.clear()
    updated = closed = errors = 0

    for pos in positions:
        cid = pos["condition_id"]
        if cid not in _market_cache:
            market = _fetch_market(cid)
            if market is None:
                errors += 1
                continue
            _market_cache[cid] = market
        market = _market_cache[cid]

        price, is_closed, won = _token_state(pos, market)
        if price is None and not is_closed:
            errors += 1
            continue

        if is_closed:
            final_price = 1.0 if won else 0.0
            pnl = pos["shares"] * final_price - pos["usd"]
            store.mark_position(pos["id"], final_price, round(pnl, 2),
                                status="won" if won else "lost", closed=True)
            closed += 1
        else:
            pnl = pos["shares"] * price - pos["usd"]
            store.mark_position(pos["id"], price, round(pnl, 2))
            updated += 1
            # Auto-exits: ceiling → floor → stop-loss % → hold cap
            settings = store.get_settings()
            stop_pct = settings.get("stop_loss_pct") or 0
            hold_days = settings.get("max_hold_days") or 0
            loss_pct = ((price - pos["entry_price"]) / pos["entry_price"] * 100
                        if pos["entry_price"] else 0)
            if pos.get("ceiling") and price >= pos["ceiling"]:
                mirror.execute_sell({**pos, "last_price": price}, reason="ceiling")
                closed += 1
            elif pos.get("floor") and price <= pos["floor"]:
                mirror.execute_sell({**pos, "last_price": price}, reason="floor")
                closed += 1
            elif stop_pct > 0 and loss_pct <= -stop_pct:
                mirror.execute_sell({**pos, "last_price": price},
                                    reason=f"stop-loss {loss_pct:.0f}%")
                closed += 1
            elif hold_days > 0 and time.time() - pos["ts"] > hold_days * 86400:
                mirror.execute_sell({**pos, "last_price": price}, reason="hold cap")
                closed += 1

    # Snapshot per mode (open book value + cumulative realized)
    summary = store.performance_summary()
    for mode, s in summary.items():
        if s["open_count"] or s["realized"]:
            store.add_snapshot(mode, s["open_cost"],
                               s["open_cost"] + s["unrealized"], s["realized"])

    return {"updated": updated, "closed": closed, "errors": errors}


def whale_exit_check(fresh_signals: list[dict], required_misses: int = 2) -> list[dict]:
    """Called after each sweep. If a position's signal has been absent from
    `required_misses` consecutive sweeps, the consensus behind it unwound —
    exit with the whales (when the setting is on)."""
    if not store.get_settings().get("exit_with_whales"):
        return []
    present = {s["id"] for s in fresh_signals}
    results = []
    for rec in store.bump_missing_sweeps(present):
        if rec["missing_sweeps"] >= required_misses:
            pos = store.get_position(rec["id"])
            if pos and pos["status"] == "open":
                results.append(mirror.execute_sell(pos, reason="whale exit"))
    return results
