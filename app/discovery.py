"""Whale discovery beyond the leaderboard.

Three sources feed the pool:
  1. Leaderboards (category fan-out) — handled by ConsensusEngine.select_whales.
  2. Holders — top token holders of the highest-volume markets (Gamma → /holders).
  3. Large trades — wallets behind outsized recent fills (/trades).

Wallets from sources 2 and 3 don't come with PnL, so we compute our own
performance metrics from /closed-positions (realized PnL, win rate, ROI) and
cache the score in SQLite with a TTL. Each sweep scores only a bounded batch
of new or stale wallets, so the pool warms up over the first few sweeps and
stays cheap thereafter.
"""

import math
import time

import requests

from . import store

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

DISCOVERY_DEFAULTS = {
    "holders_markets": 100,       # top markets by volume to scan
    "holders_per_market": 20,
    "min_trade_usd": 2000.0,      # fill size that flags a wallet as interesting
    "trades_limit": 500,
    "score_batch": 150,           # max wallets scored per sweep
    "score_ttl_hours": 24.0,
    "min_self_trades": 10,        # closed positions required to qualify
    "min_self_pnl": 10000.0,      # realized USD required to qualify
}


def _get(session: requests.Session, url: str, **params):
    for attempt in range(3):
        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 2:
                return None
            time.sleep(1.5 ** attempt)
    return None


# ── Source: top markets → holders ─────────────────────────────────────────
def top_markets(session, limit: int) -> list[str]:
    """Condition IDs of the highest-volume open markets, via Gamma."""
    variants = [
        {"order": "volume24hr", "ascending": "false", "limit": limit,
         "closed": "false", "active": "true"},
        {"order": "volumeNum", "ascending": "false", "limit": limit, "closed": "false"},
        {"order": "volume", "ascending": "false", "limit": limit, "closed": "false"},
    ]
    for params in variants:
        data = _get(session, f"{GAMMA_API}/markets", **params)
        rows = data if isinstance(data, list) else (data or {}).get("data") or []
        cids = [m.get("conditionId") or m.get("condition_id") for m in rows]
        cids = [c for c in cids if c]
        if cids:
            return cids
    return []


def _walk_holders(node, out: dict):
    """Holders payload shape has varied; walk it and collect wallet/name pairs."""
    if isinstance(node, dict):
        addr = node.get("proxyWallet") or node.get("address") or node.get("wallet")
        if addr:
            out.setdefault(addr.lower(),
                           node.get("name") or node.get("pseudonym") or addr[:10])
        for v in node.values():
            _walk_holders(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_holders(v, out)


def holder_wallets(session, condition_ids: list[str], per_market: int,
                   delay: float, progress=None) -> dict[str, str]:
    found: dict[str, str] = {}
    for i, cid in enumerate(condition_ids, 1):
        if progress:
            progress(i, len(condition_ids), f"holders {i}/{len(condition_ids)}")
        data = _get(session, f"{DATA_API}/holders", market=cid, limit=per_market)
        if data:
            _walk_holders(data, found)
        time.sleep(delay)
    return found


# ── Source: large trades ──────────────────────────────────────────────────
def large_trade_wallets(session, limit: int, min_usd: float) -> dict[str, str]:
    data = _get(session, f"{DATA_API}/trades", limit=limit, takerOnly="true")
    rows = data if isinstance(data, list) else (data or {}).get("data") or []
    found: dict[str, str] = {}
    for t in rows:
        try:
            usd = float(t.get("size") or 0) * float(t.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if usd < min_usd:
            continue
        addr = t.get("proxyWallet") or t.get("maker") or t.get("wallet")
        if addr:
            found.setdefault(addr.lower(),
                             t.get("name") or t.get("pseudonym") or addr[:10])
    return found


# ── Self-computed scoring from closed positions ───────────────────────────
def score_wallet(session, address: str) -> dict:
    """Compute realized PnL, win rate, ROI from a wallet's closed positions.
    Returns a record ready for the wallet cache; qualified=0 if it fails
    the whale bar or the endpoint yields nothing."""
    rows = []
    for path in ("/closed-positions", "/closedPositions"):
        data = _get(session, f"{DATA_API}{path}", user=address, limit=200)
        rows = data if isinstance(data, list) else (data or {}).get("data") or []
        if rows:
            break

    realized = volume = 0.0
    wins = total = 0
    for p in rows:
        pnl = p.get("cashPnl", p.get("realizedPnl", p.get("pnl")))
        if pnl is None:
            continue
        pnl = float(pnl)
        cost = abs(float(p.get("initialValue") or p.get("totalBought")
                         or p.get("size") or 0))
        realized += pnl
        volume += cost
        total += 1
        if pnl > 0:
            wins += 1

    win_rate = wins / total if total else 0.0
    roi = realized / volume if volume > 0 else 0.0
    weight = math.log10(1 + max(realized, 0)) * (0.5 + win_rate)
    return {
        "address": address, "realized": realized, "volume": volume,
        "roi": roi, "win_rate": win_rate, "trades": total,
        "weight": weight, "last_scored": time.time(),
    }


# ── Pool assembly ─────────────────────────────────────────────────────────
def build_pool(engine, progress=None) -> dict[str, dict]:
    """Merged whale pool: leaderboard whales + qualified discovered wallets."""
    cfg = {**DISCOVERY_DEFAULTS, **engine.config}
    session = engine.session

    # 1. Leaderboard whales (authoritative PnL — never re-scored by us)
    whales = engine.select_whales(progress=progress)
    for w in whales.values():
        w.setdefault("sources", set()).add("leaderboard")

    # 2. Holder discovery
    if progress:
        progress(0, 1, "top markets by volume")
    cids = top_markets(session, cfg["holders_markets"])
    discovered = holder_wallets(session, cids, cfg["holders_per_market"],
                                cfg["request_delay"], progress=progress)
    for addr in discovered:
        discovered[addr] = (discovered[addr], "holders")

    # 3. Large-trade discovery
    if progress:
        progress(0, 1, "scanning large trades")
    for addr, name in large_trade_wallets(session, cfg["trades_limit"],
                                          cfg["min_trade_usd"]).items():
        discovered.setdefault(addr, (name, "trades"))

    # Score the batch of new/stale discovered wallets (leaderboard addrs excluded)
    candidates = [a for a in discovered if a not in whales]
    stale_before = time.time() - cfg["score_ttl_hours"] * 3600
    to_score = store.wallets_needing_score(candidates, stale_before)[: cfg["score_batch"]]
    for i, addr in enumerate(to_score, 1):
        if progress:
            progress(i, len(to_score), f"scoring wallet {i}/{len(to_score)}")
        rec = score_wallet(session, addr)
        name, source = discovered[addr]
        rec["name"], rec["source"] = name, source
        rec["qualified"] = int(
            rec["trades"] >= cfg["min_self_trades"]
            and rec["realized"] >= cfg["min_self_pnl"]
            and rec["roi"] >= cfg["min_roi"]          # same MM filter as leaderboard
        )
        store.upsert_wallet(rec)
        time.sleep(cfg["request_delay"])

    # Merge every qualified cached wallet into the pool
    for rec in store.qualified_wallets():
        addr = rec["address"]
        if addr in whales:
            whales[addr]["sources"].add(rec["source"])
            continue
        whales[addr] = {
            "address": addr, "name": rec["name"],
            "pnl": rec["realized"], "volume": rec["volume"],
            "roi": rec["roi"], "weight": rec["weight"],
            "categories": [], "sources": {rec["source"]},
        }

    # Bound the sweep to the highest-weight whales
    if len(whales) > cfg["max_whales"]:
        top = sorted(whales.items(), key=lambda kv: kv[1]["weight"], reverse=True)
        whales = dict(top[: cfg["max_whales"]])
    for w in whales.values():
        w["sources"] = sorted(w["sources"])
    return whales
