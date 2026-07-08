"""Whale consensus engine — leaderboard → positions → consensus signals."""

import hashlib
import math
import time
from collections import defaultdict

import requests

DATA_API = "https://data-api.polymarket.com"

DEFAULT_CONFIG = {
    "top": 50,
    "min_roi": 0.02,
    "min_whales": 3,
    "dominance": 0.75,
    "min_position_usd": 500.0,
    "min_book_fraction": 0.005,
    "price_floor": 0.05,
    "price_ceiling": 0.95,
    "max_positions_per_whale": 300,
    "request_delay": 0.25,
}


class ConsensusEngine:
    def __init__(self, config: dict | None = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "whalemirror/0.1"})

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
    def _fetch_leaderboard(self, window: str, limit: int) -> list[dict]:
        # Current API: /v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=50
        period = {"1d": "DAY", "7d": "WEEK", "30d": "MONTH", "all": "ALL"}.get(window, "MONTH")
        variants = [
            ("/v1/leaderboard", {"category": "OVERALL", "timePeriod": period,
                                 "orderBy": "PNL", "limit": min(limit, 50)}),
            # legacy shapes kept as fallbacks
            ("/leaderboard", {"window": window, "limit": limit, "rankType": "profit"}),
            ("/leaderboard", {"period": window, "limit": limit, "type": "pnl"}),
        ]
        for path, params in variants:
            data = self._get(path, **params)
            if not data:
                continue
            rows = data if isinstance(data, list) else data.get("leaderboard") or data.get("data") or []
            out = []
            for row in rows:
                addr = row.get("proxyWallet") or row.get("address") or row.get("user") or row.get("wallet")
                if not addr:
                    continue
                out.append({
                    "address": addr.lower(),
                    "name": row.get("name") or row.get("userName") or row.get("pseudonym") or addr[:10],
                    "pnl": float(row.get("amount") or row.get("pnl") or row.get("profit") or 0),
                    "volume": float(row.get("volume") or row.get("vol") or 0),
                })
            if out:
                return out
        return []

    def select_whales(self) -> dict[str, dict]:
        cfg = self.config
        board_30d = self._fetch_leaderboard("30d", cfg["top"])
        board_all = self._fetch_leaderboard("all", cfg["top"])
        if not board_30d and not board_all:
            raise RuntimeError("Leaderboard fetch failed on all endpoint variants")

        addrs_30d = {w["address"] for w in board_30d}
        addrs_all = {w["address"] for w in board_all}

        merged: dict[str, dict] = {}
        for w in board_30d + board_all:
            cur = merged.setdefault(w["address"], {**w, "pnl": 0.0, "volume": 0.0})
            cur["pnl"] = max(cur["pnl"], w["pnl"])
            cur["volume"] = max(cur["volume"], w["volume"])

        kept = {}
        for addr, w in merged.items():
            roi = w["pnl"] / w["volume"] if w["volume"] > 0 else 0.0
            if w["volume"] > 0 and roi < cfg["min_roi"]:
                continue
            consistency = 1.5 if (addr in addrs_30d and addr in addrs_all) else 1.0
            w["roi"] = roi
            w["weight"] = math.log10(1 + max(w["pnl"], 0)) * consistency
            kept[addr] = w
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
        whales = self.select_whales()
        max_weight = max((w["weight"] for w in whales.values()), default=1.0) or 1.0

        side_book = defaultdict(list)
        condition_totals = defaultdict(float)

        for i, (addr, w) in enumerate(whales.items(), 1):
            if progress:
                progress(i, len(whales), w["name"])
            for p in self.fetch_positions(addr):
                key = (p["condition_id"], p["outcome_index"])
                side_book[key].append({**p, "whale": w["name"],
                                       "whale_weight": w["weight"] / max_weight})
                condition_totals[p["condition_id"]] += p["value"]
            time.sleep(cfg["request_delay"])

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
