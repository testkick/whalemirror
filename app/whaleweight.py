"""Whale weighting from OUR mirror results — Bayesian-shrunk, ROI-based, decayed.

Design principles (see the long design discussion):
  1. SHRINK toward the population mean by sample size — a whale at 80% on 10 bets
     is pulled most of the way back to the ~average; on 300 bets it barely moves.
     This structurally refuses to trust small samples (the whole failure mode).
  2. Weight on ROI, not win rate — win rate lies (61% wins, 1.9% ROI paradox).
  3. DECAY old bets gently (half-life) so recent form counts more, without
     chasing noise.
  4. BOUNDED, gentle output — best whale ~2-3x the worst, never winner-take-all,
     so a cold streak or a lucky run can't dominate the book.

CRITICAL: this computes a SHADOW signal. It ranks whales and can be inspected,
but it does NOT feed the live consensus score until an out-of-sample check shows
high-weighted whales actually predict better future results. The selection
engine is untouched. Everything here is measurement first.
"""

from __future__ import annotations

import math
import time

# Tunables — deliberately conservative defaults.
SHRINKAGE_STRENGTH = 50.0   # pseudo-bets pulling toward the mean; higher = more skeptical
HALF_LIFE_DAYS = 21.0       # a bet this old counts half as much as a fresh one
WEIGHT_FLOOR = 0.5          # bounded range: worst whale
WEIGHT_CEIL = 2.5           # best whale — ~5x floor at the extremes, gentle in practice
MIN_BETS_TO_RANK = 10       # below this we don't even show a weight (too little data)


def _decay(ts: float, now: float) -> float:
    """Exponential recency weight in [0,1]; 1.0 = now, 0.5 = one half-life ago."""
    age_days = max(0.0, (now - ts) / 86400.0)
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def compute_weights(rows: list[dict], population_roi: float | None = None,
                    now: float | None = None) -> dict[str, dict]:
    """rows: settled mirror positions with a whale attribution. Each row needs
    address, name, pnl, usd, closed_ts. Returns address -> weight record.

    A whale may co-sign many positions; we aggregate their decayed ROI, shrink
    it toward the population ROI, and map to a bounded multiplier."""
    now = now or time.time()

    # population ROI = decayed dollar-weighted ROI across all attributed bets
    tot_pnl = tot_stake = 0.0
    per_whale: dict[str, dict] = {}
    for r in rows:
        addr = (r.get("address") or "").lower()
        if not addr:
            continue
        pnl = float(r.get("pnl") or 0.0)
        usd = float(r.get("usd") or 0.0)
        if usd <= 0:
            continue
        d = _decay(float(r.get("closed_ts") or r.get("ts") or now), now)
        w = per_whale.setdefault(addr, {"name": r.get("name") or addr[:10],
                                        "dpnl": 0.0, "dstake": 0.0, "n_eff": 0.0,
                                        "n": 0, "raw_pnl": 0.0, "raw_stake": 0.0})
        w["dpnl"] += pnl * d
        w["dstake"] += usd * d
        w["n_eff"] += d          # decayed sample size -> old bets shrink confidence
        w["raw_pnl"] += pnl
        w["raw_stake"] += usd
        w["n"] += 1
        tot_pnl += pnl * d
        tot_stake += usd * d

    # Population baseline: dollar-weighted decayed ROI across ALL attributed
    # bets. This is the honest "average bet outcome" and the right shrinkage
    # target — it is intentionally NOT filtered to rankable whales, because the
    # baseline should reflect the true average, not a survivor-biased subset.
    pop_roi = population_roi if population_roi is not None else (
        tot_pnl / tot_stake if tot_stake > 0 else 0.0)

    out: dict[str, dict] = {}
    for addr, w in per_whale.items():
        raw_roi = w["dpnl"] / w["dstake"] if w["dstake"] > 0 else 0.0
        # Bayesian shrinkage toward pop_roi, using DECAYED effective sample so a
        # whale whose record is old (low n_eff) gets pulled harder toward the mean.
        n_eff = w["n_eff"]
        shrunk_roi = (raw_roi * n_eff + pop_roi * SHRINKAGE_STRENGTH) / (n_eff + SHRINKAGE_STRENGTH)
        # Map shrunk ROI to a bounded weight. Centered so an average whale = 1.0.
        # A whale +10% above pop ROI moves toward the ceil; -10% toward floor.
        # tanh keeps it gentle and bounded.
        edge = shrunk_roi - pop_roi
        span = (WEIGHT_CEIL - WEIGHT_FLOOR) / 2.0
        center = (WEIGHT_CEIL + WEIGHT_FLOOR) / 2.0
        weight = center + span * math.tanh(edge * 4.0)   # 4.0 = sensitivity (gentle)
        out[addr] = {
            "address": addr, "name": w["name"], "bets": w["n"],
            "raw_roi": round(w["raw_pnl"] / w["raw_stake"] * 100, 1) if w["raw_stake"] else 0.0,
            "shrunk_roi": round(shrunk_roi * 100, 1),
            "weight": round(weight, 3),
            "rankable": w["n"] >= MIN_BETS_TO_RANK,
        }
    return out


def rank(weights: dict[str, dict]) -> list[dict]:
    """Rankable whales, best weight first."""
    rows = [w for w in weights.values() if w["rankable"]]
    return sorted(rows, key=lambda w: w["weight"], reverse=True)


def out_of_sample_check(train_rows: list[dict], test_rows: list[dict]) -> dict:
    """The validation that gates going live: weight whales on TRAIN bets, then
    see whether high-weighted whales actually did better on TEST bets they
    weren't scored on. Returns correlation-ish summary: top-half vs bottom-half
    weighted whales' realized ROI on the held-out test set."""
    w = compute_weights(train_rows)
    ranked = rank(w)
    # Need a real population to split — with too few whales, train/test noise
    # correlates by chance and fakes an edge (the overfitting trap). Require 8+
    # rankable whales per side (16 total) before we'll trust a verdict.
    MIN_PER_SIDE = 8
    if len(ranked) < MIN_PER_SIDE * 2:
        return {"ok": False,
                "reason": f"need {MIN_PER_SIDE*2}+ rankable whales to validate, have {len(ranked)}",
                "rankable_whales": len(ranked)}
    mid = len(ranked) // 2
    top = {r["address"] for r in ranked[:mid]}
    bot = {r["address"] for r in ranked[mid:]}

    def roi_on(addrs):
        pnl = stake = 0.0
        for r in test_rows:
            if (r.get("address") or "").lower() in addrs:
                pnl += float(r.get("pnl") or 0); stake += float(r.get("usd") or 0)
        return (pnl / stake * 100) if stake > 0 else None, stake

    top_roi, top_stake = roi_on(top)
    bot_roi, bot_stake = roi_on(bot)
    spread = (top_roi - bot_roi) if (top_roi is not None and bot_roi is not None) else None
    # A "predict" verdict needs BOTH sides to have real held-out volume AND a
    # spread clearing a conservative noise floor (5 ROI points). This deliberately
    # errs toward "does not predict" — a false "go live" is far costlier than
    # waiting for more data.
    MIN_TEST_STAKE = 500.0   # per side, $ of held-out bets
    NOISE_FLOOR = 5.0        # ROI points; below this the spread is likely noise
    enough_stake = (top_stake >= MIN_TEST_STAKE and bot_stake >= MIN_TEST_STAKE)
    predicts = (spread is not None and spread > NOISE_FLOOR and enough_stake)
    if not enough_stake:
        verdict = "insufficient held-out data — keep collecting before trusting weights"
    elif predicts:
        verdict = "weights predict — top-weighted whales outperformed on held-out bets"
    else:
        verdict = "weights do NOT predict — do not put on live score yet"
    return {
        "ok": True,
        "top_half_test_roi": None if top_roi is None else round(top_roi, 1),
        "bottom_half_test_roi": None if bot_roi is None else round(bot_roi, 1),
        "spread": None if spread is None else round(spread, 1),
        "predicts": predicts,
        "verdict": verdict,
        "top_n": len(top), "bot_n": len(bot),
        "top_test_stake": round(top_stake, 0), "bot_test_stake": round(bot_stake, 0),
        "noise_floor": NOISE_FLOOR, "min_test_stake": MIN_TEST_STAKE,
    }
