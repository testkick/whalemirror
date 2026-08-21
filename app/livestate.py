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
        # Do the WS gameIds and the Gamma game-keys share format? This is the
        # crux: if these two keyspaces don't overlap, mapping is 0 and the
        # filter can't gate. Surface the overlap directly.
        ws_ids = set((h.get("ws_diag") or {}).get("sample_game_ids") or [])
        gamma_keys = set((h.get("gamma_diag") or {}).get("sample_game_keys") or [])
        h["keyspace_overlap"] = sorted(ws_ids & gamma_keys)
        h["ws_key_format"] = next(iter(ws_ids), None)
        h["gamma_key_format"] = next(iter(gamma_keys), None)
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
        # sample the WS gameId format + any co-keys, to compare against Gamma
        diag = self._health.setdefault("ws_diag", {"sample_game_ids": [], "sample_msg_keys": []})
        if len(diag["sample_game_ids"]) < 5 and gid not in diag["sample_game_ids"]:
            diag["sample_game_ids"].append(gid)
        if not diag["sample_msg_keys"] and isinstance(msg, dict):
            diag["sample_msg_keys"] = sorted(msg.keys())[:25]

    @staticmethod
    def _extract_game_key(m: dict) -> str | None:
        """Find a game identifier on a Gamma market, trying several shapes:
          1. top-level gameId variants
          2. a game/event slug of the form {league}-{away}-{home}-{date}
             (e.g. 'nfl-nyj-pit-2026-08-21'), found top-level or in events[]
          3. an event id/ticker inside the events[] array
        Returns the normalized game key, or None for non-game markets (futures,
        awards, props — which correctly never map)."""
        import re
        slug_re = re.compile(r"^[a-z]{2,4}-[a-z]{2,4}-[a-z]{2,4}-\d{4}-\d{2}-\d{2}$")

        # 1. explicit top-level game id
        for k in ("gameId", "game_id", "gameID"):
            v = m.get(k)
            if v:
                return str(v)

        # 2. slug pattern, top-level
        for k in ("slug", "gameSlug", "eventSlug"):
            v = m.get(k)
            if v and slug_re.match(str(v)):
                return str(v)

        # 3. dig into events[]
        for ev in (m.get("events") or []):
            if not isinstance(ev, dict):
                continue
            for k in ("gameId", "game_id", "gameID"):
                v = ev.get(k)
                if v:
                    return str(v)
            for k in ("slug", "ticker"):
                v = ev.get(k)
                if v and slug_re.match(str(v)):
                    return str(v)
        return None

    def apply_gamma_catalog(self, markets: list[dict]) -> None:
        """Rebuild the gameKey<->condition_id maps from a Gamma markets dump,
        plus capture diagnostics so /healthz can reveal the real data shape."""
        cid_to_game, game_to_cids = {}, {}
        # diagnostics
        sample_keys, slug_examples, with_events, with_gameid = [], [], 0, 0
        game_market_dump = None

        def _id_like(d, prefix=""):
            """Collect every field whose key or value looks like an id, so we can
            spot a 6065514-style WS gameId wherever it hides."""
            out = {}
            if not isinstance(d, dict):
                return out
            for k, v in d.items():
                kl = k.lower()
                if any(w in kl for w in ("id", "game", "event", "slug", "ticker", "start")):
                    # keep scalars; note nested dicts/lists shallowly
                    if isinstance(v, (str, int, float, bool)) or v is None:
                        out[prefix + k] = v
                    elif isinstance(v, dict):
                        out[prefix + k] = f"<dict keys={sorted(v.keys())[:12]}>"
                    elif isinstance(v, list):
                        out[prefix + k] = f"<list len={len(v)}>"
            return out

        for i, m in enumerate(markets):
            if i == 0 and isinstance(m, dict):
                sample_keys = sorted(m.keys())[:40]
            if m.get("events"):
                with_events += 1
            if any(m.get(k) for k in ("gameId", "game_id", "gameID")):
                with_gameid += 1

            # Deep-dump the FIRST market that looks like an actual game (has a
            # gameStartTime, or a league-team-team-date slug). This is where the
            # real join key should be if it exists.
            if game_market_dump is None and isinstance(m, dict):
                looks_game = bool(m.get("gameStartTime")) or bool(
                    self._extract_game_key(m))
                if looks_game:
                    dump = {"market_id_fields": _id_like(m),
                            "gameStartTime": m.get("gameStartTime"),
                            "question": str(m.get("question") or "")[:60]}
                    evs = m.get("events") or []
                    if evs and isinstance(evs[0], dict):
                        dump["event0_id_fields"] = _id_like(evs[0])
                        dump["event0_all_keys"] = sorted(evs[0].keys())[:40]
                    game_market_dump = dump

            cid = m.get("conditionId") or m.get("condition_id")
            gid = self._extract_game_key(m)
            if gid and len(slug_examples) < 5:
                slug_examples.append(gid)
            if not cid or not gid:
                continue
            cid_to_game[cid] = gid
            game_to_cids.setdefault(gid, set()).add(cid)
        self._cid_to_game = cid_to_game
        self._game_to_cids = game_to_cids
        self._health["last_gamma_sync_ts"] = time.time()
        # expose what the sync actually saw, so we can fix mapping from fact
        self._health["gamma_diag"] = {
            "markets_seen": len(markets),
            "markets_with_events_field": with_events,
            "markets_with_gameid_field": with_gameid,
            "game_keys_extracted": len(cid_to_game),
            "sample_market_keys": sample_keys,
            "sample_game_keys": slug_examples,
            "game_market_dump": game_market_dump,
        }

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
