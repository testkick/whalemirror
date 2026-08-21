"""Live sports state tracker (Option B: background consumer).

Polymarket exposes an authoritative pregame-vs-live flag ONLY on the sports
WebSocket feed (wss://sports-api.polymarket.com/ws), keyed by gameId — there is
no single-point REST check on condition_id. So we run two background tasks:

  1. Gamma sync  — periodically mirrors the Gamma catalog to build a
     gameId -> [condition_id, ...] map (a game has many markets).
  2. WS consumer — holds the sports socket open and caches each gameId's live
     state, reconnecting with backoff on drop.

The mirror gate then answers is_live(condition_id) synchronously off an
in-memory cache. Everything here is designed to FAIL OPEN AND VISIBLE: when we
don't have a confident answer (socket down, game unmapped, stale data), we
report "unknown" and let the caller decide — the default policy mirrors the bet
but flags it, rather than silently halting sports mirroring on any hiccup.
Health is exposed for /healthz so a dead feed is never silent.

This module has NO import dependency on the rest of the app (store/mirror), so
it can be tested in isolation and can never break the mirror path by importing.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

SPORTS_WS = "wss://sports-api.polymarket.com/ws"
GAMMA_API = "https://gamma-api.polymarket.com"
STALE_SECS = 300           # a game's live state older than this is "unknown"
GAMMA_SYNC_SECS = 600      # rebuild the gameId->CID map every 10 min


class LiveStateTracker:
    def __init__(self):
        # gameId -> {"live": bool, "ended": bool, "status": str, "ts": float}
        self._game_state: dict[str, dict] = {}
        # condition_id -> gameId  (reverse of the gamma map, for O(1) lookup)
        self._cid_to_game: dict[str, str] = {}
        # gameId -> set[condition_id]
        self._game_to_cids: dict[str, set] = {}
        self._health = {
            "ws_connected": False,
            "last_ws_msg_ts": 0.0,
            "last_gamma_sync_ts": 0.0,
            "reconnects": 0,
            "games_tracked": 0,
            "cids_mapped": 0,
            "last_error": None,
        }
        self._stop = False

    # ── public, synchronous read used by the mirror gate ──────────────────
    def is_live(self, condition_id: str) -> tuple[bool | None, str]:
        """Return (live, reason). live is True/False if we KNOW, or None if we
        can't say confidently (unmapped, stale, or socket down). The caller
        decides what to do with None — see gate policy in mirror.py."""
        gid = self._cid_to_game.get(condition_id)
        if gid is None:
            return None, "market not mapped to a game (non-sport or catalog lag)"
        st = self._game_state.get(gid)
        if st is None:
            return None, "game mapped but no live state received yet"
        if time.time() - st["ts"] > STALE_SECS:
            return None, f"live state stale ({round(time.time()-st['ts'])}s old)"
        if st.get("ended"):
            return False, "game ended"
        return bool(st.get("live")), "live" if st.get("live") else "pregame"

    def health(self) -> dict:
        h = dict(self._health)
        h["games_tracked"] = len(self._game_state)
        h["cids_mapped"] = len(self._cid_to_game)
        h["ws_msg_age_secs"] = (round(time.time() - h["last_ws_msg_ts"])
                                if h["last_ws_msg_ts"] else None)
        h["gamma_sync_age_secs"] = (round(time.time() - h["last_gamma_sync_ts"])
                                    if h["last_gamma_sync_ts"] else None)
        return h

    # ── ingestion (called by the WS consumer; separated for testability) ──
    def apply_message(self, msg: dict[str, Any]) -> None:
        """Update cache from one parsed WS message. Tolerant of shape: pulls
        gameId + live/ended/status if present, ignores anything else."""
        gid = str(msg.get("gameId") or msg.get("game_id") or "")
        if not gid:
            return
        self._game_state[gid] = {
            "live": bool(msg.get("live", False)),
            "ended": bool(msg.get("ended", False)),
            "status": str(msg.get("status") or ""),
            "ts": time.time(),
        }
        self._health["last_ws_msg_ts"] = time.time()

    def apply_gamma_catalog(self, markets: list[dict]) -> None:
        """Rebuild the gameId<->condition_id maps from a Gamma markets dump.
        Each market may carry a gameId and a conditionId; we index both ways."""
        cid_to_game, game_to_cids = {}, {}
        for m in markets:
            cid = m.get("conditionId") or m.get("condition_id")
            gid = m.get("gameId") or m.get("game_id") or m.get("gameID")
            if not cid or not gid:
                continue
            gid = str(gid)
            cid_to_game[cid] = gid
            game_to_cids.setdefault(gid, set()).add(cid)
        # atomic swap so readers never see a half-built map
        self._cid_to_game = cid_to_game
        self._game_to_cids = game_to_cids
        self._health["last_gamma_sync_ts"] = time.time()

    # ── background tasks ──────────────────────────────────────────────────
    async def run_gamma_sync(self, fetch_markets) -> None:
        """fetch_markets() -> list[dict]; injected so tests can stub it."""
        while not self._stop:
            try:
                markets = await asyncio.to_thread(fetch_markets)
                if markets:
                    self.apply_gamma_catalog(markets)
            except Exception as e:  # noqa: BLE001
                self._health["last_error"] = f"gamma sync: {e}"
            await asyncio.sleep(GAMMA_SYNC_SECS)

    async def run_ws_consumer(self, connect=None) -> None:
        """Hold the sports WS open, caching state. `connect` is injected for
        tests; defaults to a real websockets connection. Reconnects with
        exponential backoff, capped."""
        if connect is None:
            connect = self._default_connect
        backoff = 1.0
        while not self._stop:
            try:
                async with connect() as ws:
                    self._health["ws_connected"] = True
                    self._health["last_error"] = None
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        # feed may send a list of updates or a single object
                        if isinstance(msg, list):
                            for m in msg:
                                if isinstance(m, dict):
                                    self.apply_message(m)
                        elif isinstance(msg, dict):
                            self.apply_message(msg)
            except Exception as e:  # noqa: BLE001
                self._health["last_error"] = f"ws: {e}"
            self._health["ws_connected"] = False
            self._health["reconnects"] += 1
            await asyncio.sleep(min(backoff, 30))
            backoff *= 2

    def _default_connect(self):
        import websockets
        return websockets.connect(SPORTS_WS, ping_interval=20, ping_timeout=20)

    def stop(self):
        self._stop = True


# module-level singleton the app shares
tracker = LiveStateTracker()
