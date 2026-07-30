"""Position tracker.

Marks every open position to the LIVE market and computes unrealized P&L.

Pricing (fixed): the previous version read the `price` field off
/markets/{condition_id}, which is a slow snapshot that lagged the order book
badly (it would show 0.45 while the market traded at 0.96). We now mark each
position at its LIVE midpoint via /midpoint?token_id=, which is exactly the
number Polymarket shows as the implied probability. Because Polymarket falls
back to the last-trade price when the bid/ask spread is wider than $0.10
(common during fast in-game swings), we spread-check and fall back to
/price?side=SELL when the book is too wide to trust the midpoint.

Resolution/winner detection still comes from /markets (a settled-state flag,
which that endpoint reports fine). Runs from the scheduler on its own fast
cadence; dry-run and live positions are tracked identically but bucketed
separately.
"""

import time

import requests

from . import mirror, store

CLOB_API = "https://clob.polymarket.com"
WIDE_SPREAD = 0.10          # Polymarket shows last-trade instead of midpoint past this
_market_cache: dict[str, dict] = {}
_session = requests.Session()
_session.headers.update({"User-Agent": "whalemirror-tracker/1.0"})


def _fetch_market(condition_id: str) -> dict | None:
    """Slow snapshot — used ONLY for resolution/winner detection, not pricing."""
    try:
        r = _session.get(f"{CLOB_API}/markets/{condition_id}", timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def _get_float(url: str, key: str, **params) -> float | None:
    try:
        r = _session.get(url, params=params, timeout=8)
        r.raise_for_status()
        v = r.json().get(key)
        return float(v) if v is not None else None
    except (requests.RequestException, ValueError, TypeError):
        return None


def live_price(token_id: str) -> float | None:
    """Live mark for a token, matching what Polymarket displays.

    Prefer the midpoint. If the book is wide (spread > $0.10) the midpoint is
    unreliable, so fall back to the sell-side quote (what you'd actually get
    exiting). Returns None if the token can't be priced at all.
    """
    if not token_id:
        return None
    mid = _get_float(f"{CLOB_API}/midpoint", "mid", token_id=token_id)
    bid = _get_float(f"{CLOB_API}/price", "price", token_id=token_id, side="SELL")
    ask = _get_float(f"{CLOB_API}/price", "price", token_id=token_id, side="BUY")

    if bid is not None and ask is not None:
        spread = ask - bid
        if spread > WIDE_SPREAD:
            # Book too wide to trust the midpoint — use the executable sell side.
            return bid
        if mid is not None:
            return mid
        return (bid + ask) / 2
    # Partial data
    return mid if mid is not None else bid


def sell_quote(token_id: str) -> float | None:
    """The price a market SELL would actually fill at (highest bid)."""
    return _get_float(f"{CLOB_API}/price", "price", token_id=token_id, side="SELL")


def _resolution_state(pos: dict) -> tuple[bool, bool]:
    """(market_closed, this_token_won) from the /markets snapshot."""
    cid = pos["condition_id"]
    if cid not in _market_cache:
        m = _fetch_market(cid)
        _market_cache[cid] = m or {}
    market = _market_cache[cid]
    if not market.get("closed"):
        return False, False
    tokens = market.get("tokens") or []
    token = None
    if pos.get("token_id"):
        token = next((t for t in tokens if t.get("token_id") == pos["token_id"]), None)
    if token is None and pos.get("outcome_index") is not None \
            and pos["outcome_index"] < len(tokens):
        token = tokens[pos["outcome_index"]]
    return True, bool(token and token.get("winner"))


def refresh_positions() -> dict:
    """One tracking pass over all open positions. Returns counts for logging."""
    positions = store.open_positions()
    _market_cache.clear()
    settings = store.get_settings()
    stop_pct = settings.get("stop_loss_pct") or 0
    hold_days = settings.get("max_hold_days") or 0
    updated = closed = errors = 0

    for pos in positions:
        # Resolution first (cheap settled-state check).
        is_closed, won = _resolution_state(pos)
        if is_closed:
            final_price = 1.0 if won else 0.0
            pnl = pos["shares"] * final_price - pos["usd"]
            store.mark_position(pos["id"], final_price, round(pnl, 2),
                                status="won" if won else "lost", closed=True)
            closed += 1
            continue

        # Live mark.
        price = live_price(pos.get("token_id"))
        if price is None:
            errors += 1
            continue

        pnl = pos["shares"] * price - pos["usd"]
        store.mark_position(pos["id"], price, round(pnl, 2))
        updated += 1

        # Auto-exits: ceiling → floor → stop-loss % → hold cap.
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


    # Snapshot per mode (open book value + cumulative realized).
    summary = store.performance_summary()
    for mode, s in summary.items():
        # Always snapshot when there's any activity in the mode (open OR any
        # closed history), so the series has no gaps that make the chart lag.
        if s["open_count"] or s["realized"] or s.get("cost_basis"):
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
