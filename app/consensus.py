"""Whale consensus engine — leaderboard → positions → consensus signals.

Whale discovery fans out across all leaderboard categories and both time
periods (MONTH + ALL), since /v1/leaderboard caps at 50 rows per request.
Position fetching runs in a small thread pool so a several-hundred-whale
sweep stays in the couple-of-minutes range.
"""

import hashlib
import math
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

DATA_API = "https://data-api.polymarket.com"

CATEGORIES = ["OVERALL", "POLITICS", "SPORTS", "ESPORTS", "CRYPTO", "CULTURE",
              "MENTIONS", "WEATHER", "ECONOMICS", "TECH", "FINANCE"]

DEFAULT_CONFIG = {
    "min_roi": 0.02,
    "min_whales": 3,
    "dominance": 0.75,
    "min_position_usd": 500.0,
    "min_book_fraction": 0.005,
    "price_floor": 0.05,
    "price_ceiling": 0.95,
    "max_positions_per_whale": 300,
    "request_delay": 0.15,      # per worker thread
    "workers": 4,               # parallel position fetchers
    "max_whales": 500,          # hard bound on sweep size (top by weight)
}


class ConsensusEngine:
    def __init__(self, config: dict | None = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "whalemirror/0.2"})
        self._lock = threading.Lock()

    # ── HTTP ──────────────────────────────────────────────────────────────
    def _get(self, path: str, **params):
        url = f"{DATA_API}{path}"
        for attempt in range(4):
            try:
                r = self.session.get(url, params=params, timeout=20)
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException:
                if attempt == 3:
                    return None
                time.sleep(1.5 ** attempt)
        return None

    # ── Stage 1: whales ──────────────────────────────────────────────────
    def _fetch_board(self, period: str, category: str) -> list[dict]:
        """One leaderboard page, normalized. Tries offset pagination
        defensively — if the API ignores offset (returns the same first
        address), we stop after page one."""
        rows_out, offset, first_seen = [], 0, None
        while True:
            params = {"category": category, "timePeriod": period,
                      "orderBy": "PNL", "limit": 50}
            if offset:
                params["offset"] = offset
            data = self._get("/v1/leaderboard", **params)
            rows = data if isinstance(data, list) else (data or {}).get("data") or []
            if not rows:
                break
            first_addr = (rows[0].get("proxyWallet") or "").lower()
            if offset and first_addr == first_seen:
                break  # offset unsupported — same page came back
            if not offset:
                first_seen = first_addr
            for row in rows:
                addr = row.get("proxyWallet") or row.get("address") or row.get("wallet")
                if not addr:
                    continue
                rows_out.append({
                    "address": addr.lower(),
                    "name": row.get("name") or row.get("userName") or row.get("pseudonym") or addr[:10],
                    "pnl": float(row.get("amount") or row.get("pnl") or row.get("profit") or 0),
                    "volume": float(row.get("volume") or row.get("vol") or 0),
                })
            if len(rows) < 50 or offset >= 200:   # stop at 5 pages / board
                break
            offset += 50
        return rows_out

    def select_whales(self, progress=None) -> dict[str, dict]:
        cfg = self.config
        boards: dict[str, list[dict]] = {}
        for pi, period in enumerate(("MONTH", "ALL")):
            for ci, cat in enumerate(CATEGORIES):
                if progress:
                    progress(pi * len(CATEGORIES) + ci + 1, 2 * len(CATEGORIES),
                             f"leaderboard {period}/{cat}")
                boards[f"{period}:{cat}"] = self._fetch_board(period, cat)
                time.sleep(cfg["request_delay"])

        if not any(boards.values()):
            raise RuntimeError("Leaderboard fetch failed on all endpoint variants")

        month_addrs = {w["address"] for k, b in boards.items() if k.startswith("MONTH") for w in b}
        all_addrs = {w["address"] for k, b in boards.items() if k.startswith("ALL") for w in b}

        merged: dict[str, dict] = {}
        for key, board in boards.items():
            _, cat = key.split(":")
            for w in board:
                cur = merged.setdefault(w["address"],
                                        {**w, "pnl": 0.0, "volume": 0.0, "categories": set()})
                cur["pnl"] = max(cur["pnl"], w["pnl"])
                cur["volume"] = max(cur["volume"], w["volume"])
                if cat != "OVERALL":
                    cur["categories"].add(cat)

        kept = {}
        for addr, w in merged.items():
            roi = w["pnl"] / w["volume"] if w["volume"] > 0 else 0.0
            if w["volume"] > 0 and roi < cfg["min_roi"]:
                continue  # market-maker profile: big volume, thin edge
            consistency = 1.5 if (addr in month_addrs and addr in all_addrs) else 1.0
            w["roi"] = roi
            w["weight"] = math.log10(1 + max(w["pnl"], 0)) * consistency
            w["categories"] = sorted(w["categories"])
            kept[addr] = w

        # Bound sweep size: keep the highest-weight whales
        if len(kept) > cfg["max_whales"]:
            top = sorted(kept.items(), key=lambda kv: kv[1]["weight"], reverse=True)
            kept = dict(top[: cfg["max_whales"]])
        return kept

    # ── Stage 2: positions ───────────────────────────────────────────────
    def fetch_positions(self, address: str) -> list[dict]:
        cfg = self.config
        data = self._get("/positions", user=address, sizeThreshold=1, limit=500)
        if not data:
            return []
        rows = data if isinstance(data, list) else data.get("positions") or data.get("data") or []
        if len(rows) > cfg["max_positions_per_whale"]:
            return []
        book_value = sum(float(p.get("currentValue") or 0) for p in rows) or 1.0
        keep = []
        for p in rows:
            value = float(p.get("currentValue") or 0)
            cur_price = float(p.get("curPrice") or 0)
            if p.get("redeemable"):
                continue
            if value < max(cfg["min_position_usd"], cfg["min_book_fraction"] * book_value):
                continue
            if not (cfg["price_floor"] <= cur_price <= cfg["price_ceiling"]):
                continue
            keep.append({
                "condition_id": p.get("conditionId"),
                "outcome": p.get("outcome"),
                "outcome_index": p.get("outcomeIndex"),
                "title": p.get("title") or "",
                "value": value,
                "avg_price": float(p.get("avgPrice") or 0),
                "cur_price": cur_price,
                "end_date": p.get("endDate"),
            })
        return keep

    # ── Stage 3: consensus ───────────────────────────────────────────────
    def run(self, progress=None) -> list[dict]:
        cfg = self.config
        whales = self.select_whales(progress=progress)
        max_weight = max((w["weight"] for w in whales.values()), default=1.0) or 1.0

        side_book = defaultdict(list)
        condition_totals = defaultdict(float)
        done = {"n": 0}

        def scan(item):
            addr, w = item
            positions = self.fetch_positions(addr)
            time.sleep(cfg["request_delay"])
            with self._lock:
                done["n"] += 1
                if progress:
                    progress(done["n"], len(whales), f"whale {w['name']}")
                for p in positions:
                    key = (p["condition_id"], p["outcome_index"])
                    side_book[key].append({**p, "whale": w["name"],
                                           "whale_weight": w["weight"] / max_weight})
                    condition_totals[p["condition_id"]] += p["value"]

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            futures = [pool.submit(scan, item) for item in whales.items()]
            for f in as_completed(futures):
                f.result()  # propagate exceptions

        signals = []
        for (condition_id, outcome_index), holdings in side_book.items():
            wallets = {h["whale"] for h in holdings}
            if len(wallets) < cfg["min_whales"]:
                continue
            side_value = sum(h["value"] for h in holdings)
            dominance = side_value / condition_totals[condition_id]
            if dominance < cfg["dominance"]:
                continue
            cur_price = holdings[0]["cur_price"]
            avg_entry = sum(h["avg_price"] * h["value"] for h in holdings) / side_value
            score = (len(wallets) * dominance
                     * (1 + sum(h["whale_weight"] for h in holdings) / len(holdings))
                     * math.log10(1 + side_value))
            sig_id = hashlib.sha1(f"{condition_id}:{outcome_index}".encode()).hexdigest()[:16]
            signals.append({
                "id": sig_id,
                "title": holdings[0]["title"],
                "outcome": holdings[0]["outcome"],
                "condition_id": condition_id,
                "outcome_index": outcome_index,
                "whale_count": len(wallets),
                "whales": sorted(wallets),
                "whale_dollars": round(side_value),
                "dominance": round(dominance, 3),
                "avg_whale_entry": round(avg_entry, 3),
                "current_price": round(cur_price, 3),
                "entry_drift": round(cur_price - avg_entry, 3),
                "end_date": holdings[0]["end_date"],
                "score": round(score, 2),
            })
        signals.sort(key=lambda s: s["score"], reverse=True)
        return signals
