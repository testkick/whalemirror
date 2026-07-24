"""Mirror executor.

Resolves a signal's (condition_id, outcome_index) to a CLOB token_id, then
places a buy through py-clob-client — or simulates it in dry-run mode.

Every execution path passes through the same guardrails:
  - dry_run flag (default ON)
  - per-trade USD cap
  - rolling daily USD cap (live orders only)
  - slippage guard vs the price captured at signal time
  - de-dup: a signal is only auto-mirrored once
"""

from datetime import datetime, timezone

import requests

from . import store


def _days_to_end(end_date) -> float | None:
    if not end_date:
        return None
    try:
        d = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (d - datetime.now(timezone.utc)).total_seconds() / 86400
    except ValueError:
        return None

CLOB_API = "https://clob.polymarket.com"

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    CLOB_AVAILABLE = True
except ImportError:  # app still runs (dry-run only) without the client installed
    CLOB_AVAILABLE = False

_client_cache: dict[str, object] = {}


def resolve_token_id(condition_id: str, outcome_index: int) -> tuple[str | None, float | None]:
    """Look up the ERC-1155 token id and current midpoint for an outcome."""
    try:
        r = requests.get(f"{CLOB_API}/markets/{condition_id}", timeout=15)
        r.raise_for_status()
        market = r.json()
        tokens = market.get("tokens") or []
        if outcome_index is None or outcome_index >= len(tokens):
            return None, None
        token = tokens[outcome_index]
        token_id = token.get("token_id")
        price = token.get("price")
        return token_id, (float(price) if price is not None else None)
    except requests.RequestException:
        return None, None


def _get_client():
    creds = store.load_credentials()
    if not creds:
        raise RuntimeError("No trading credentials configured")
    if not CLOB_AVAILABLE:
        raise RuntimeError("py-clob-client not installed in this image")
    cache_key = creds["funder_address"]
    if cache_key not in _client_cache:
        client = ClobClient(
            CLOB_API,
            key=creds["private_key"],
            chain_id=137,
            signature_type=creds["signature_type"],
            funder=creds["funder_address"],
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        _client_cache[cache_key] = client
    return _client_cache[cache_key]


def execute_mirror(signal: dict, usd: float | None = None, manual: bool = False) -> dict:
    """Mirror one signal. Returns a result dict and writes to the mirror log."""
    settings = store.get_settings()
    usd = float(usd or settings["per_trade_usd"])
    mode = "dry_run" if settings["dry_run"] else "live"

    def fail(status: str, detail: str, price: float = 0.0):
        store.log_mirror(signal, usd, price, mode, status, detail)
        return {"status": status, "detail": detail, "mode": mode}

    # Guardrails ----------------------------------------------------------
    if usd <= 0 or usd > settings["per_trade_usd"] * 4:
        return fail("skipped", f"size {usd} outside sane bounds")
    if settings.get("mirroring_paused"):
        return fail("skipped", "mirroring is paused")
    price_now = signal["current_price"]
    lo, hi = settings["min_entry_price"], settings["max_entry_price"]
    if not (lo <= price_now <= hi):
        return fail("skipped", f"entry band: price {price_now:.3f} outside [{lo:.2f}, {hi:.2f}]", price_now)
    enabled_cats = settings.get("enabled_categories") or []
    if enabled_cats:
        cat = store.classify_category(signal.get("title"), signal.get("category"))
        if cat not in enabled_cats:
            return fail("skipped", f"category filter: '{cat}' not in enabled categories")
    max_days = settings.get("max_days_to_resolution") or 0
    if max_days:
        days = _days_to_end(signal.get("end_date"))
        if days is not None and days > max_days:
            return fail("skipped", f"time horizon: resolves in {days:.0f}d > {max_days:.0f}d cap")
    enabled = settings.get("enabled_categories") or []
    if enabled:
        cat = store.classify_category(signal.get("title"), signal.get("category"))
        if cat not in enabled:
            return fail("skipped", f"category focus: '{cat}' not in enabled categories")
    conflict = store.open_position_conflict(
        signal["condition_id"], signal["outcome_index"], store.event_key_for(signal),
        title=signal.get("title", ""), outcome=signal.get("outcome", ""))
    if conflict:
        return fail("skipped",
                    f"self-hedge: already hold '{conflict['title']}' → {conflict['outcome']}")
    if mode == "live":
        spent = store.spent_today_usd()
        if spent + usd > settings["daily_cap_usd"]:
            return fail("skipped", f"daily cap hit ({spent:.0f} + {usd:.0f} > {settings['daily_cap_usd']:.0f})")

    token_id, live_price = resolve_token_id(signal["condition_id"], signal["outcome_index"])
    if not token_id:
        return fail("error", "could not resolve token_id for outcome")

    ref_price = signal["current_price"]
    if live_price is not None and live_price - ref_price > settings["max_slippage"]:
        return fail("skipped",
                    f"slippage guard: live {live_price:.3f} vs signal {ref_price:.3f}",
                    live_price)
    fill_price = live_price if live_price is not None else ref_price

    # Execution -----------------------------------------------------------
    if mode == "dry_run":
        detail = f"DRY RUN: would buy ${usd:.2f} of '{signal['outcome']}' at ~{fill_price:.3f} (token {token_id[:12]}…)"
        store.log_mirror(signal, usd, fill_price, mode, "ok", detail)
        store.add_position(signal, usd, fill_price, token_id, mode)
        return {"status": "ok", "detail": detail, "mode": mode}

    try:
        client = _get_client()
        order = client.create_market_order(MarketOrderArgs(
            token_id=token_id,
            amount=usd,          # USD collateral for a market BUY
            side=BUY,
        ))
        resp = client.post_order(order, OrderType.FOK)
        detail = f"LIVE: ${usd:.2f} '{signal['outcome']}' @ ~{fill_price:.3f} → {resp}"
        store.log_mirror(signal, usd, fill_price, mode, "ok", str(detail))
        store.add_position(signal, usd, fill_price, token_id, mode)
        return {"status": "ok", "detail": detail, "mode": mode}
    except Exception as e:  # noqa: BLE001 — surface everything to the log
        return fail("error", f"order failed: {e}", fill_price)


def auto_mirror_pass(signals: list[dict]) -> list[dict]:
    """Called by the scheduler after each refresh. Mirrors new qualifying signals."""
    settings = store.get_settings()
    if not settings.get("setup_complete"):
        return []   # first-run: nothing mirrors until preferences are confirmed
    if settings.get("mirroring_paused"):
        return []   # master pause
    if not settings["auto_mirror"]:
        return []
    already = store.mirrored_signal_ids()
    results = []
    for s in signals:
        if s["id"] in already:
            continue
        if s.get("signal_type") == "followed" and not settings.get("auto_mirror_followed"):
            continue
        if s["score"] < settings["min_score_to_mirror"]:
            continue
        results.append({"signal": s["title"], **execute_mirror(s)})
    return results


def execute_sell(pos: dict, reason: str = "manual") -> dict:
    """Exit a tracked position. Executes in the mode the position was OPENED in:
    dry-run positions always sell simulated, live positions always sell real."""
    mode = pos["mode"]
    pseudo_signal = {"id": pos["signal_id"], "title": pos["title"], "outcome": pos["outcome"]}

    live_price, _ = None, None
    token_id, live_price = resolve_token_id(pos["condition_id"], pos["outcome_index"])
    price = live_price if live_price is not None else (pos.get("last_price") or pos["entry_price"])
    proceeds = pos["shares"] * price
    pnl = round(proceeds - pos["usd"], 2)

    if mode == "dry_run":
        detail = (f"DRY RUN SELL ({reason}): {pos['shares']:.1f} sh '{pos['outcome']}' "
                  f"at ~{price:.3f} → ${proceeds:.2f} ({'+' if pnl >= 0 else ''}{pnl:.2f})")
        store.log_mirror(pseudo_signal, proceeds, price, mode, "ok", detail, side="SELL")
        store.mark_position(pos["id"], price, pnl, status="sold", closed=True, reason=reason)
        return {"status": "ok", "detail": detail, "mode": mode}

    if not token_id:
        return {"status": "error", "detail": "could not resolve token for sell", "mode": mode}
    try:
        client = _get_client()
        order = client.create_market_order(MarketOrderArgs(
            token_id=token_id,
            amount=pos["shares"],   # shares for a market SELL
            side=SELL,
        ))
        resp = client.post_order(order, OrderType.FOK)
        detail = (f"LIVE SELL ({reason}): {pos['shares']:.1f} sh '{pos['outcome']}' "
                  f"at ~{price:.3f} → {resp}")
        store.log_mirror(pseudo_signal, proceeds, price, mode, "ok", str(detail), side="SELL")
        store.mark_position(pos["id"], price, pnl, status="sold", closed=True, reason=reason)
        return {"status": "ok", "detail": detail, "mode": mode}
    except Exception as e:  # noqa: BLE001
        detail = f"sell failed ({reason}): {e}"
        store.log_mirror(pseudo_signal, proceeds, price, mode, "error", detail, side="SELL")
        return {"status": "error", "detail": detail, "mode": mode}
