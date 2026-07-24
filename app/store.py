"""Persistence: settings, encrypted credentials, signal cache, mirror log.

The Polymarket private key is encrypted at rest with Fernet. The Fernet key is
derived from the APP_SECRET environment variable — the database file alone is
useless without it. Credentials are write-only: they are never returned to the
browser after being saved.
"""

import base64
import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from cryptography.fernet import Fernet

DB_PATH = os.environ.get("DB_PATH", "/data/whalemirror.db")


def _fernet() -> Fernet:
    secret = os.environ.get("APP_SECRET")
    if not secret or len(secret) < 16:
        raise RuntimeError("APP_SECRET env var must be set (16+ chars) to encrypt credentials")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


@contextmanager
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn):
    """Additive column migrations for existing databases."""
    for stmt in (
        "ALTER TABLE mirrors ADD COLUMN side TEXT DEFAULT 'BUY'",
        "ALTER TABLE positions ADD COLUMN floor REAL",
        "ALTER TABLE positions ADD COLUMN ceiling REAL",
        "ALTER TABLE positions ADD COLUMN missing_sweeps INTEGER DEFAULT 0",
        "ALTER TABLE positions ADD COLUMN exit_reason TEXT",
        "ALTER TABLE positions ADD COLUMN category TEXT",
        "ALTER TABLE positions ADD COLUMN event_key TEXT",
        """CREATE TABLE IF NOT EXISTS position_whales (
            position_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            name TEXT,
            PRIMARY KEY (position_id, address)
        )""",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists


def init():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS kv (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS signals (
            id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS followed_whales (
            address TEXT PRIMARY KEY,
            name TEXT,
            added REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            created REAL NOT NULL,
            expires REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS wallets (
            address TEXT PRIMARY KEY,
            name TEXT,
            source TEXT,           -- 'holders' | 'trades'
            realized REAL,
            volume REAL,
            roi REAL,
            win_rate REAL,
            trades INTEGER,
            weight REAL,
            qualified INTEGER DEFAULT 0,
            last_scored REAL
        );
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            signal_id TEXT NOT NULL,
            title TEXT,
            outcome TEXT,
            condition_id TEXT,
            outcome_index INTEGER,
            token_id TEXT,
            mode TEXT,             -- 'dry_run' | 'live'
            usd REAL,              -- cost basis
            entry_price REAL,
            shares REAL,
            status TEXT DEFAULT 'open',   -- 'open' | 'won' | 'lost'
            last_price REAL,
            pnl REAL DEFAULT 0,
            closed_ts REAL
        );
        CREATE TABLE IF NOT EXISTS pnl_snapshots (
            ts REAL NOT NULL,
            mode TEXT NOT NULL,
            cost REAL,             -- open cost basis
            value REAL,            -- open mark-to-market value
            realized REAL          -- cumulative realized pnl
        );
        CREATE TABLE IF NOT EXISTS mirrors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            signal_id TEXT NOT NULL,
            title TEXT,
            outcome TEXT,
            usd REAL,
            price REAL,
            mode TEXT,             -- 'dry_run' | 'live'
            status TEXT,           -- 'ok' | 'skipped' | 'error'
            detail TEXT
        );
        """)
        _migrate(conn)


# ── Settings ──────────────────────────────────────────────────────────────
SETTINGS_DEFAULTS = {
    "auto_mirror": False,
    "auto_mirror_followed": False,
    "dry_run": True,
    "per_trade_usd": 25.0,
    "daily_cap_usd": 100.0,
    "max_slippage": 0.03,          # skip if price moved > 3¢ past the signal
    "exit_with_whales": True,      # sell when the signal's consensus unwinds
    "default_floor_offset": 0.0,   # auto stop: entry − X (0 = off)
    "default_ceiling_offset": 0.0, # auto take-profit: entry + X (0 = off)
    "stop_loss_pct": 0.0,          # close if down N% from entry (0 = off)
    "min_entry_price": 0.0,        # only mirror inside [min, max] price band
    "max_entry_price": 1.0,
    "max_days_to_resolution": 0,   # skip signals ending beyond N days (0 = off)
    "max_hold_days": 0,            # auto-close positions held longer (0 = off)
    "enabled_categories": [],      # [] = all categories allowed
    "setup_complete": False,       # first-run wizard gate
    "mirroring_paused": False,     # master switch: blocks all mirroring
    "enabled_categories": [],      # [] = all categories allowed
    "onboarded": False,            # first-run setup completed
    "min_score_to_mirror": 8.0,
    "refresh_minutes": 15,
    # engine knobs surfaced in the UI
    "min_whales": 3,
    "dominance": 0.75,
}


def get_settings() -> dict:
    with db() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k='settings'").fetchone()
    saved = json.loads(row["v"]) if row else {}
    return {**SETTINGS_DEFAULTS, **saved}


def save_settings(patch: dict):
    allowed = {k: patch[k] for k in patch if k in SETTINGS_DEFAULTS}
    if allowed.get("enabled_categories") is not None and "enabled_categories" in allowed:
        allowed["enabled_categories"] = [str(c) for c in allowed["enabled_categories"]][:20]
    merged = {**get_settings(), **allowed}
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES ('settings', ?)",
                     (json.dumps(merged),))
    return merged


# ── Credentials (write-only, encrypted) ───────────────────────────────────
def save_credentials(private_key: str, funder_address: str, signature_type: int):
    blob = _fernet().encrypt(json.dumps({
        "private_key": private_key.strip(),
        "funder_address": funder_address.strip(),
        "signature_type": int(signature_type),
    }).encode()).decode()
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES ('creds', ?)", (blob,))


def load_credentials() -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k='creds'").fetchone()
    if not row:
        return None
    return json.loads(_fernet().decrypt(row["v"].encode()))


def clear_credentials():
    with db() as conn:
        conn.execute("DELETE FROM kv WHERE k='creds'")


def credentials_status() -> dict:
    """Safe summary for the UI — never includes the key."""
    creds = load_credentials()
    if not creds:
        return {"configured": False}
    return {"configured": True,
            "funder_address": creds["funder_address"][:6] + "…" + creds["funder_address"][-4:],
            "signature_type": creds["signature_type"]}


# ── Signals cache ─────────────────────────────────────────────────────────
def upsert_signals(signals: list[dict]):
    now = time.time()
    with db() as conn:
        for s in signals:
            conn.execute("""
                INSERT INTO signals (id, payload, first_seen, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, last_seen=excluded.last_seen
            """, (s["id"], json.dumps(s), now, now))
        conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES ('last_refresh', ?)", (str(now),))


def get_signals(max_age_hours: float = 6.0) -> list[dict]:
    cutoff = time.time() - max_age_hours * 3600
    with db() as conn:
        rows = conn.execute(
            "SELECT payload, first_seen FROM signals WHERE last_seen >= ? ", (cutoff,)
        ).fetchall()
    out = []
    for r in rows:
        s = json.loads(r["payload"])
        s["first_seen"] = r["first_seen"]
        out.append(s)
    out.sort(key=lambda s: s["score"], reverse=True)
    return out


def last_refresh() -> float | None:
    with db() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k='last_refresh'").fetchone()
    return float(row["v"]) if row else None


# ── Mirror log ────────────────────────────────────────────────────────────
def log_mirror(signal: dict, usd: float, price: float, mode: str, status: str,
               detail: str, side: str = "BUY"):
    with db() as conn:
        conn.execute("""
            INSERT INTO mirrors (ts, signal_id, title, outcome, usd, price, mode, status, detail, side)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (time.time(), signal["id"], signal["title"], signal["outcome"],
              usd, price, mode, status, detail[:500], side))


def mirror_history(limit: int = 100) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM mirrors ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def mirrored_signal_ids() -> set[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT signal_id FROM mirrors WHERE status='ok'").fetchall()
    return {r["signal_id"] for r in rows}


def spent_today_usd() -> float:
    midnight = time.time() - (time.time() % 86400)
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(usd), 0) AS s FROM mirrors "
            "WHERE ts >= ? AND status='ok' AND mode='live' AND side='BUY'",
            (midnight,)).fetchone()
    return float(row["s"])


# ── Position tracking ─────────────────────────────────────────────────────
CATEGORY_KEYWORDS = [
    ("Esports", ["counter-strike", "cs2", "csgo", "dota", "league of legends",
                 "lol:", "valorant", "overwatch", "rocket league", "starcraft",
                 "rainbow six", "call of duty", "(bo1)", "(bo3)", "(bo5)",
                 "map handicap", "esports", "cct ", "lfl ", "lec ", "lcs ",
                 "playoffs -", "regular season -"]),
    ("Tennis", ["atp", "wta", "wimbledon", "roland garros", "australian open",
                "grand slam", "itf", "challenger tour"]),
    ("Soccer", ["premier league", "la liga", "serie a", "bundesliga", "ligue 1",
                "champions league", "europa league", "mls", "fifa",
                "corinthians", "anderlecht", "real madrid", "barcelona",
                "liverpool", "arsenal", "chelsea", "juventus", "bayern"]),
    ("Sports", ["nba", "nfl", "mlb", "nhl", "vs.", " vs ", "o/u", "spread:",
                "dodgers", "yankees", "lakers", "celtics", "premier league",
                "champions league", "world cup", "super bowl", "playoff",
                "braves", "mets", "phillies", "padres", "brewers", "-1.5", "+1.5",
                "moneyline", "to win the", "series", "match", "game "]),
    ("Politics", ["president", "presidential", "election", "senate", "congress",
                  "nomination", "primary", "governor", "democrat", "republican",
                  "impeach", "cabinet", "supreme court", "vote", "poll", "ballot",
                  "trump", "biden", "vance", "aoc", "shapiro", "newsom"]),
    ("Geopolitics", ["ceasefire", "war", "invade", "sanction", "treaty", "nato",
                     "iran", "ukraine", "russia", "china", "israel", "gaza",
                     "hormuz", "strait", "military", "nuclear", "withdrawal",
                     "negotiation", "diplomatic", "border"]),
    ("Crypto", ["bitcoin", "btc", "ethereum", "eth", "solana", "crypto", "coin",
                "token", "defi", "stablecoin", "etf approval"]),
    ("Economics", ["fed", "rate hike", "rate cut", "inflation", "cpi", "gdp",
                   "recession", "unemployment", "interest rate", "jobs report",
                   "s&p", "nasdaq", "dow", "treasury", "tariff"]),
    ("Tech", ["ai ", "openai", "gpt", "llm", "chip", "nvidia", "tesla", "spacex",
              "launch", "iphone", "apple", "google", "microsoft", "model"]),
    ("Culture", ["oscar", "grammy", "movie", "box office", "album", "spotify",
                 "netflix", "celebrity", "award", "rotten tomatoes", "billboard"]),
]


def known_category_labels() -> set[str]:
    return {label for label, _ in CATEGORY_KEYWORDS}


def classify_category(title: str, given: str | None = None) -> str:
    """Infer the market category from the title.

    `given` is only trusted when it matches a KNOWN category label. Upstream
    enrichment has been observed passing the market OUTCOME (e.g. 'Winthrop
    University', 'Atlanta Braves') as the category, which is not a category at
    all — anything unrecognized is ignored and the title is classified instead.
    """
    if given and given.strip():
        g = given.strip()
        for label in known_category_labels():
            if g.lower() == label.lower():
                return label
    t = (title or "").lower()
    import re
    # Structural patterns first — they are more specific than generic keywords.
    # "City: Player A vs Player B" (no league/team markers) -> tennis-style singles
    if (re.match(r"^[a-z .'\-]+:\s+[a-z .'\-]+\s+vs\.?\s+[a-z .'\-]+$", t)
            and not any(k in t for k in ("bo1", "bo3", "bo5", "o/u", "spread",
                                         "handicap", "counter-strike", "lol",
                                         "dota", "valorant"))):
        return "Tennis"
    # "Will <club> win on YYYY-MM-DD?" -> soccer daily fixture
    if re.search(r"will .+ win on \d{4}-\d{2}-\d{2}", t):
        return "Soccer"
    for label, kws in CATEGORY_KEYWORDS:
        if any(k in t for k in kws):
            return label
    return "Uncategorized"


CATEGORY_CHOICES = ["Sports", "Soccer", "Tennis", "Esports", "Politics",
                    "Geopolitics", "Crypto", "Economics", "Tech", "Culture",
                    "Uncategorized"]


def backfill_categories() -> int:
    """Classify positions with a missing OR invalid category. Invalid means a
    value that isn't a known label — e.g. an outcome name written in by a
    previous enrichment bug."""
    valid = known_category_labels() | {"Uncategorized"}
    with db() as conn:
        rows = conn.execute("SELECT id, title, category FROM positions").fetchall()
        n = 0
        for r in rows:
            cur = (r["category"] or "").strip()
            if cur in valid and cur != "Uncategorized":
                continue  # already good
            cat = classify_category(r["title"], None)
            if cat != cur:
                conn.execute("UPDATE positions SET category=? WHERE id=?", (cat, r["id"]))
                n += 1
    return n


def event_key_for(signal: dict) -> str:
    """Group key for 'same underlying event': the Polymarket event slug when
    known (both sides of one game share it), else the condition id."""
    url = signal.get("url") or ""
    if "/event/" in url:
        return "ev:" + url.split("/event/")[1].split("?")[0]
    return "cond:" + (signal.get("condition_id") or "")

def add_position(signal: dict, usd: float, price: float, token_id: str, mode: str):
    shares = usd / price if price > 0 else 0.0
    s = get_settings()
    fo, co = s.get("default_floor_offset", 0), s.get("default_ceiling_offset", 0)
    floor = round(max(price - fo, 0.01), 3) if fo > 0 else None
    ceiling = round(min(price + co, 0.99), 3) if co > 0 else None
    with db() as conn:
        cur = conn.execute("""
            INSERT INTO positions (ts, signal_id, title, outcome, condition_id,
                outcome_index, token_id, mode, usd, entry_price, shares, last_price,
                pnl, floor, ceiling, category, event_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
        """, (time.time(), signal["id"], signal["title"], signal["outcome"],
              signal["condition_id"], signal["outcome_index"], token_id, mode,
              usd, price, shares, price, floor, ceiling,
              classify_category(signal.get("title"), signal.get("category")),
              event_key_for(signal)))
        pid = cur.lastrowid
        for w in signal.get("whale_details") or []:
            conn.execute("INSERT OR IGNORE INTO position_whales (position_id, address, name) VALUES (?, ?, ?)",
                         (pid, w["address"], w["name"]))


def open_positions() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM positions WHERE status='open'").fetchall()
    return [dict(r) for r in rows]


def all_positions(limit: int = 200) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM positions ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def mark_position(pos_id: int, last_price: float, pnl: float,
                  status: str = "open", closed: bool = False, reason: str | None = None):
    with db() as conn:
        if closed:
            conn.execute("UPDATE positions SET last_price=?, pnl=?, status=?, closed_ts=?, exit_reason=? WHERE id=?",
                         (last_price, pnl, status, time.time(), reason, pos_id))
        else:
            conn.execute("UPDATE positions SET last_price=?, pnl=? WHERE id=?",
                         (last_price, pnl, pos_id))


def get_position(pos_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
    return dict(row) if row else None


def set_levels(pos_id: int, floor: float | None, ceiling: float | None):
    with db() as conn:
        conn.execute("UPDATE positions SET floor=?, ceiling=? WHERE id=?",
                     (floor, ceiling, pos_id))


def bump_missing_sweeps(present_signal_ids: set[str]) -> list[dict]:
    """After a sweep: open positions whose signal vanished get missing_sweeps+1,
    present ones reset to 0. Returns open positions with their new counts."""
    with db() as conn:
        rows = conn.execute("SELECT id, signal_id, missing_sweeps FROM positions WHERE status='open'").fetchall()
        out = []
        for r in rows:
            n = 0 if r["signal_id"] in present_signal_ids else (r["missing_sweeps"] or 0) + 1
            conn.execute("UPDATE positions SET missing_sweeps=? WHERE id=?", (n, r["id"]))
            out.append({"id": r["id"], "missing_sweeps": n})
    return out


def add_snapshot(mode: str, cost: float, value: float, realized: float):
    with db() as conn:
        conn.execute("INSERT INTO pnl_snapshots (ts, mode, cost, value, realized) VALUES (?, ?, ?, ?, ?)",
                     (time.time(), mode, cost, value, realized))


def snapshots(mode: str | None = None, limit: int = 2000) -> list[dict]:
    q = "SELECT * FROM pnl_snapshots"
    args: tuple = ()
    if mode:
        q += " WHERE mode=?"
        args = (mode,)
    q += " ORDER BY ts ASC LIMIT ?"
    with db() as conn:
        rows = conn.execute(q, args + (limit,)).fetchall()
    return [dict(r) for r in rows]


def peak_capital_deployed(mode: str) -> float:
    """Max capital simultaneously at risk — the real denominator for ROI when
    winnings recycle into new bets. Walk position opens (+usd) and closes
    (−usd) in time order; track the running peak of concurrent exposure."""
    with db() as conn:
        rows = conn.execute(
            "SELECT ts, usd, closed_ts FROM positions WHERE mode=?", (mode,)).fetchall()
    events = []
    for r in rows:
        events.append((r["ts"], r["usd"]))            # capital goes out
        if r["closed_ts"]:
            events.append((r["closed_ts"], -r["usd"])) # capital comes back
    events.sort(key=lambda e: e[0])
    running = peak = 0.0
    for _, delta in events:
        running += delta
        peak = max(peak, running)
    return round(peak, 2)


def performance_summary() -> dict:
    out = {}
    with db() as conn:
        for mode in ("dry_run", "live"):
            open_rows = conn.execute(
                "SELECT COALESCE(SUM(usd),0) c, COALESCE(SUM(pnl),0) u, COUNT(*) n "
                "FROM positions WHERE status='open' AND mode=?", (mode,)).fetchone()
            closed = conn.execute(
                "SELECT COALESCE(SUM(pnl),0) r, "
                "SUM(CASE WHEN status='won' OR (status='sold' AND pnl>0) THEN 1 ELSE 0 END) w, "
                "SUM(CASE WHEN status='lost' OR (status='sold' AND pnl<=0) THEN 1 ELSE 0 END) l "
                "FROM positions WHERE status!='open' AND mode=?", (mode,)).fetchone()
            wins, losses = closed["w"] or 0, closed["l"] or 0
            closed_cost = conn.execute(
                "SELECT COALESCE(SUM(usd),0) c FROM positions WHERE status!='open' AND mode=?",
                (mode,)).fetchone()["c"]
            day_ago = time.time() - 86400
            d24 = conn.execute(
                "SELECT COALESCE(SUM(pnl),0) r, "
                "SUM(CASE WHEN status='won' OR (status='sold' AND pnl>0) THEN 1 ELSE 0 END) w, "
                "SUM(CASE WHEN status='lost' OR (status='sold' AND pnl<=0) THEN 1 ELSE 0 END) l "
                "FROM positions WHERE status!='open' AND mode=? AND closed_ts >= ?",
                (mode, day_ago)).fetchone()
            opened_24h = conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(usd),0) c FROM positions WHERE mode=? AND ts >= ?",
                (mode, day_ago)).fetchone()
            total = open_rows["u"] + closed["r"]
            cost_basis = open_rows["c"] + closed_cost
            peak_capital = peak_capital_deployed(mode)
            out[mode] = {
                "peak_capital": peak_capital,
                "roi_on_capital": round(total / peak_capital, 4) if peak_capital else 0.0,
                "turnover": round(cost_basis / peak_capital, 2) if peak_capital else 0.0,
                "open_count": open_rows["n"], "open_cost": open_rows["c"],
                "unrealized": round(open_rows["u"], 2),
                "realized": round(closed["r"], 2),
                "total": round(total, 2),
                "wins": wins, "losses": losses,
                "win_rate": round(wins / (wins + losses), 3) if (wins + losses) else None,
                "cost_basis": round(cost_basis, 2),
                "roi_total": round(total / cost_basis, 4) if cost_basis else 0.0,
                "roi_open": round(open_rows["u"] / open_rows["c"], 4) if open_rows["c"] else 0.0,
                "roi_realized": round(closed["r"] / closed_cost, 4) if closed_cost else 0.0,
                "realized_24h": round(d24["r"] or 0, 2),
                "wins_24h": d24["w"] or 0, "losses_24h": d24["l"] or 0,
                "opened_24h": opened_24h["n"], "deployed_24h": round(opened_24h["c"], 2),
            }
    return out


# ── Wallet score cache (discovery) ────────────────────────────────────────
def wallets_needing_score(addresses: list[str], stale_before: float) -> list[str]:
    """Subset of addresses that are unknown or last scored before the cutoff."""
    if not addresses:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT address, last_scored FROM wallets WHERE address IN (%s)"
            % ",".join("?" * len(addresses)), addresses).fetchall()
    known = {r["address"]: r["last_scored"] for r in rows}
    return [a for a in addresses
            if a not in known or (known[a] or 0) < stale_before]


def upsert_wallet(rec: dict):
    with db() as conn:
        conn.execute("""
            INSERT INTO wallets (address, name, source, realized, volume, roi,
                                 win_rate, trades, weight, qualified, last_scored)
            VALUES (:address, :name, :source, :realized, :volume, :roi,
                    :win_rate, :trades, :weight, :qualified, :last_scored)
            ON CONFLICT(address) DO UPDATE SET
                name=excluded.name, source=excluded.source,
                realized=excluded.realized, volume=excluded.volume,
                roi=excluded.roi, win_rate=excluded.win_rate,
                trades=excluded.trades, weight=excluded.weight,
                qualified=excluded.qualified, last_scored=excluded.last_scored
        """, rec)


def qualified_wallets() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM wallets WHERE qualified=1").fetchall()
    return [dict(r) for r in rows]


# ── Sessions (persist across restarts; tokens stored hashed) ─────────────
SESSION_TTL = 7 * 24 * 3600

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def add_session(token: str):
    now = time.time()
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        conn.execute("INSERT OR REPLACE INTO sessions (token_hash, created, expires) VALUES (?, ?, ?)",
                     (_token_hash(token), now, now + SESSION_TTL))


def session_valid(token: str) -> bool:
    if not token:
        return False
    with db() as conn:
        row = conn.execute("SELECT expires FROM sessions WHERE token_hash=?",
                           (_token_hash(token),)).fetchone()
    return bool(row and row["expires"] > time.time())


def remove_session(token: str):
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


# ── Retention janitor (run daily from the scheduler) ─────────────────────
def housekeeping():
    """Prune data nobody needs: signals unseen for 30 days, expired sessions,
    unqualified wallet scores older than 14 days, and downsample snapshots
    older than 7 days to hourly resolution. VACUUM reclaims the disk."""
    now = time.time()
    with db() as conn:
        conn.execute("DELETE FROM signals WHERE last_seen < ?", (now - 30 * 86400,))
        conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        conn.execute("DELETE FROM wallets WHERE qualified=0 AND last_scored < ?",
                     (now - 14 * 86400,))
        # keep one snapshot per mode per hour for anything older than 7 days
        conn.execute("""
            DELETE FROM pnl_snapshots WHERE ts < ? AND rowid NOT IN (
                SELECT MIN(rowid) FROM pnl_snapshots WHERE ts < ?
                GROUP BY mode, CAST(ts / 3600 AS INTEGER)
            )
        """, (now - 7 * 86400, now - 7 * 86400))
    # VACUUM needs its own connection outside a transaction
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()


def db_size_mb() -> float:
    try:
        return round(os.path.getsize(DB_PATH) / 1048576, 2)
    except OSError:
        return 0.0


# ── Followed whales ───────────────────────────────────────────────────────
def followed_whales() -> dict[str, str]:
    with db() as conn:
        rows = conn.execute("SELECT address, name FROM followed_whales").fetchall()
    return {r["address"]: r["name"] for r in rows}


def follow_whale(address: str, name: str):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO followed_whales (address, name, added) VALUES (?, ?, ?)",
                     (address.lower(), name, time.time()))


def unfollow_whale(address: str):
    with db() as conn:
        conn.execute("DELETE FROM followed_whales WHERE address=?", (address.lower(),))


def _game_teams(title: str) -> tuple[str, str] | None:
    """Extract the two teams from a game title like 'A vs. B' or 'A vs B: O/U 8.5'."""
    import re
    m = re.split(r"\s+vs\.?\s+", (title or ""), maxsplit=1)
    if len(m) != 2:
        return None
    away = m[0].strip()
    home = re.split(r"[:\-]", m[1])[0].strip()  # drop ': O/U 8.5' / '- spread'
    if away and home:
        return (away.lower(), home.lower())
    return None


def _is_game(title: str, outcome: str) -> bool:
    """A sports game/match market (spread, moneyline, or total) — as opposed to
    a standalone 'Will X happen?' question. Only these use event-slug linkage,
    because Polymarket groups many INDEPENDENT question-markets (e.g. every 2028
    candidate) under one election event slug, and those are not mutual hedges."""
    t = (title or "").lower()
    if _game_teams(title):
        return True
    if "spread:" in t or "o/u" in t or " vs." in t or " vs " in t:
        return True
    if (outcome or "").lower() in ("over", "under"):
        return True
    return False


def _same_game(a: dict, b_title: str, b_outcome: str, b_event: str) -> bool:
    """Same underlying game: both are game-markets AND (shared event slug OR a
    matched team pair). Never links standalone question-markets."""
    if not (_is_game(a["title"], a.get("outcome")) and _is_game(b_title, b_outcome)):
        return False
    a_event = a.get("event_key") or ""
    if a_event.startswith("ev:") and b_event and a_event == b_event:
        return True
    at, bt = _game_teams(a["title"]), _game_teams(b_title)
    return bool(at and bt and set(at) == set(bt))


def _side_token(outcome: str, title: str) -> str:
    """Normalize what side a bet takes, for complementarity checks.
    Over/Under for totals; else the team/entity named in outcome or spread title."""
    o = (outcome or "").lower().strip()
    if o in ("over", "under"):
        return o
    # spread/moneyline: the named team. Prefer the outcome, else parse 'Spread: X (-1.5)'
    name = o
    if not name or name in ("yes", "no"):
        import re
        m = re.search(r"spread:\s*(.+?)\s*\(", (title or "").lower())
        if m:
            name = m.group(1).strip()
    return name


def _true_hedge(a: dict, b_title: str, b_outcome: str, b_condition: str,
                b_outcome_index: int, b_event: str) -> bool:
    """Would holding BOTH a (open) and b (candidate) be self-hedging?
    Only genuine complements are blocked:

      1. Same market, opposing outcome (YES vs NO of one question).
      2. Same game, totals on opposite sides (Over vs Under).
      3. Same game, spread/moneyline on OPPOSING teams (France -1.5 vs Spain -1.5).

    Allowed (not hedges): different questions sharing an event slug
    (AOC-YES vs Vance-NO), same team's spread + moneyline, O/U + moneyline,
    different-deadline variants of a question.
    """
    # 1. same market, opposing outcome
    if a["condition_id"] == b_condition:
        return a["outcome_index"] != b_outcome_index

    if not _same_game(a, b_title, b_outcome, b_event):
        return False

    a_side = _side_token(a["outcome"], a["title"])
    b_side = _side_token(b_outcome, b_title)
    if not a_side or not b_side:
        return False

    a_ou = a_side in ("over", "under")
    b_ou = b_side in ("over", "under")
    # 2. totals opposite sides
    if a_ou and b_ou:
        return a_side != b_side
    # mixing a total with a team pick on the same game is NOT a hedge
    if a_ou != b_ou:
        return False
    # 3. team picks: opposing teams -> hedge; same team -> allowed.
    # Substring-tolerant so "Brewers" == "Milwaukee Brewers".
    if a_side in b_side or b_side in a_side:
        return False
    return True


def open_position_conflict(condition_id: str, outcome_index: int, event_key: str,
                           title: str = "", outcome: str = "") -> dict | None:
    """Return an open position that would be a TRUE hedge against this entry,
    or None. Only blocks genuine self-hedging (see _true_hedge)."""
    with db() as conn:
        rows = conn.execute("SELECT * FROM positions WHERE status='open'").fetchall()
    for r in rows:
        a = dict(r)
        if _true_hedge(a, title, outcome, condition_id, outcome_index, event_key):
            return a
    return None


def category_breakdown() -> list[dict]:
    with db() as conn:
        rows = conn.execute("""
            SELECT COALESCE(NULLIF(category, ''), 'Uncategorized') AS category,
                   COUNT(*) AS positions,
                   COALESCE(SUM(usd), 0) AS invested,
                   COALESCE(SUM(pnl), 0) AS pnl,
                   SUM(CASE WHEN status='won' OR (status='sold' AND pnl>0) THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN status='lost' OR (status='sold' AND pnl<=0) THEN 1 ELSE 0 END) AS losses
            FROM positions GROUP BY COALESCE(category, 'Uncategorized')
            ORDER BY pnl DESC
        """).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["roi"] = round(d["pnl"] / d["invested"], 4) if d["invested"] else 0.0
        out.append(d)
    return out


def whale_leaderboard() -> list[dict]:
    """Our results attributed to each whale that co-signed a mirrored signal."""
    with db() as conn:
        rows = conn.execute("""
            SELECT pw.address, MAX(pw.name) AS name,
                   COUNT(*) AS positions,
                   COALESCE(SUM(p.usd), 0) AS invested,
                   COALESCE(SUM(p.pnl), 0) AS pnl,
                   SUM(CASE WHEN p.status='won' OR (p.status='sold' AND p.pnl>0) THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN p.status='lost' OR (p.status='sold' AND p.pnl<=0) THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN p.status='open' THEN 1 ELSE 0 END) AS open_count,
                   MAX(p.ts) AS last_seen
            FROM position_whales pw JOIN positions p ON p.id = pw.position_id
            GROUP BY pw.address ORDER BY pnl DESC
        """).fetchall()
    followed = followed_whales()
    out = []
    for r in rows:
        d = dict(r)
        settled = d["wins"] + d["losses"]
        d["win_rate"] = round(d["wins"] / settled, 3) if settled else None
        d["roi"] = round(d["pnl"] / d["invested"], 4) if d["invested"] else 0.0
        d["followed"] = d["address"] in followed
        out.append(d)
    return out


# ── UI state (persisted interface preferences) ────────────────────────────
def get_ui_state() -> dict:
    with db() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k='ui_state'").fetchone()
    return json.loads(row["v"]) if row else {}


def save_ui_state(state: dict):
    # bounded: it's interface preferences, not a dumping ground
    blob = json.dumps(state)[:8000]
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES ('ui_state', ?)", (blob,))


def whale_detail(address: str) -> dict:
    """Per-whale breakdown: our results with them, by category, plus samples."""
    address = address.lower()
    with db() as conn:
        prof = conn.execute("""
            SELECT MAX(pw.name) AS name, COUNT(*) AS positions,
                   COALESCE(SUM(p.usd),0) AS invested, COALESCE(SUM(p.pnl),0) AS pnl,
                   SUM(CASE WHEN p.status='won' OR (p.status='sold' AND p.pnl>0) THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN p.status='lost' OR (p.status='sold' AND p.pnl<=0) THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN p.status='open' THEN 1 ELSE 0 END) AS open_count,
                   COALESCE(SUM(CASE WHEN p.status='open' THEN p.pnl ELSE 0 END),0) AS unrealized,
                   COALESCE(SUM(CASE WHEN p.status!='open' THEN p.pnl ELSE 0 END),0) AS realized,
                   MIN(p.ts) AS first_seen, MAX(p.ts) AS last_seen
            FROM position_whales pw JOIN positions p ON p.id = pw.position_id
            WHERE pw.address = ?
        """, (address,)).fetchone()

        cats = conn.execute("""
            SELECT COALESCE(NULLIF(p.category,''),'Uncategorized') AS category,
                   COUNT(*) AS positions, COALESCE(SUM(p.usd),0) AS invested,
                   COALESCE(SUM(p.pnl),0) AS pnl,
                   SUM(CASE WHEN p.status='won' OR (p.status='sold' AND p.pnl>0) THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN p.status='lost' OR (p.status='sold' AND p.pnl<=0) THEN 1 ELSE 0 END) AS losses
            FROM position_whales pw JOIN positions p ON p.id = pw.position_id
            WHERE pw.address = ? GROUP BY 1 ORDER BY pnl DESC
        """, (address,)).fetchall()

        def sample(where, order, limit=8):
            return [dict(r) for r in conn.execute(
                "SELECT p.* FROM position_whales pw JOIN positions p ON p.id = pw.position_id "
                f"WHERE pw.address = ? AND {where} ORDER BY {order} LIMIT ?",
                (address, limit)).fetchall()]

        open_sample = sample("p.status='open'", "p.pnl DESC")
        best = sample("p.status!='open'", "p.pnl DESC", 5)
        worst = sample("p.status!='open'", "p.pnl ASC", 5)

        co = conn.execute("""
            SELECT o.address, MAX(o.name) AS name, COUNT(*) AS shared
            FROM position_whales pw
            JOIN position_whales o ON o.position_id = pw.position_id AND o.address != pw.address
            WHERE pw.address = ? GROUP BY o.address ORDER BY shared DESC LIMIT 8
        """, (address,)).fetchall()

    p = dict(prof) if prof else {}
    settled = (p.get("wins") or 0) + (p.get("losses") or 0)
    p["win_rate"] = round(p["wins"] / settled, 3) if settled else None
    p["roi"] = round(p["pnl"] / p["invested"], 4) if p.get("invested") else 0.0
    p["address"] = address
    p["followed"] = address in followed_whales()
    cat_rows = []
    for r in cats:
        d = dict(r)
        d["roi"] = round(d["pnl"] / d["invested"], 4) if d["invested"] else 0.0
        cat_rows.append(d)
    return {"profile": p, "categories": cat_rows, "open_positions": open_sample,
            "best": best, "worst": worst, "co_whales": [dict(r) for r in co]}


def whale_detail(address: str) -> dict:
    """Everything we know about one whale from OUR mirrored results:
    headline stats, per-category breakdown, and sample positions."""
    address = (address or "").lower()
    with db() as conn:
        head = conn.execute("""
            SELECT MAX(pw.name) AS name, COUNT(*) AS positions,
                   COALESCE(SUM(p.usd),0) AS invested,
                   COALESCE(SUM(p.pnl),0) AS pnl,
                   SUM(CASE WHEN p.status='won' OR (p.status='sold' AND p.pnl>0) THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN p.status='lost' OR (p.status='sold' AND p.pnl<=0) THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN p.status='open' THEN 1 ELSE 0 END) AS open_count,
                   MIN(p.ts) AS first_seen, MAX(p.ts) AS last_seen,
                   COALESCE(AVG(p.entry_price),0) AS avg_entry
            FROM position_whales pw JOIN positions p ON p.id = pw.position_id
            WHERE pw.address = ?
        """, (address,)).fetchone()

        cats = conn.execute("""
            SELECT COALESCE(NULLIF(p.category,''),'Uncategorized') AS category,
                   COUNT(*) AS positions,
                   COALESCE(SUM(p.usd),0) AS invested,
                   COALESCE(SUM(p.pnl),0) AS pnl,
                   SUM(CASE WHEN p.status='won' OR (p.status='sold' AND p.pnl>0) THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN p.status='lost' OR (p.status='sold' AND p.pnl<=0) THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN p.status='open' THEN 1 ELSE 0 END) AS open_count
            FROM position_whales pw JOIN positions p ON p.id = pw.position_id
            WHERE pw.address = ?
            GROUP BY COALESCE(NULLIF(p.category,''),'Uncategorized')
            ORDER BY pnl DESC
        """, (address,)).fetchall()

        def sample(where: str, order: str, limit: int = 5):
            return [dict(r) for r in conn.execute(f"""
                SELECT p.id, p.title, p.outcome, p.category, p.usd, p.entry_price,
                       p.last_price, p.pnl, p.status, p.exit_reason, p.ts, p.closed_ts
                FROM position_whales pw JOIN positions p ON p.id = pw.position_id
                WHERE pw.address = ? AND {where}
                ORDER BY {order} LIMIT ?
            """, (address, limit)).fetchall()]

        open_positions = sample("p.status='open'", "p.pnl DESC")
        best = sample("p.status!='open'", "p.pnl DESC")
        worst = sample("p.status!='open'", "p.pnl ASC")

    h = dict(head) if head and (head["positions"] or 0) > 0 else {}
    if not h:
        return {"address": address, "found": False}
    settled = h["wins"] + h["losses"]
    cat_rows = []
    for r in cats:
        d = dict(r)
        d["roi"] = round(d["pnl"] / d["invested"], 4) if d["invested"] else 0.0
        s = d["wins"] + d["losses"]
        d["win_rate"] = round(d["wins"] / s, 3) if s else None
        cat_rows.append(d)
    return {
        "found": True, "address": address, "name": h["name"],
        "positions": h["positions"], "invested": round(h["invested"], 2),
        "pnl": round(h["pnl"], 2),
        "roi": round(h["pnl"] / h["invested"], 4) if h["invested"] else 0.0,
        "wins": h["wins"], "losses": h["losses"], "open_count": h["open_count"],
        "win_rate": round(h["wins"] / settled, 3) if settled else None,
        "avg_entry": round(h["avg_entry"], 3),
        "first_seen": h["first_seen"], "last_seen": h["last_seen"],
        "followed": address in followed_whales(),
        "categories": cat_rows,
        "samples": {"open": open_positions, "best": best, "worst": worst},
    }
