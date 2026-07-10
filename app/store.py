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


# ── Settings ──────────────────────────────────────────────────────────────
SETTINGS_DEFAULTS = {
    "auto_mirror": False,
    "dry_run": True,
    "per_trade_usd": 25.0,
    "daily_cap_usd": 100.0,
    "max_slippage": 0.03,          # skip if price moved > 3¢ past the signal
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
def log_mirror(signal: dict, usd: float, price: float, mode: str, status: str, detail: str):
    with db() as conn:
        conn.execute("""
            INSERT INTO mirrors (ts, signal_id, title, outcome, usd, price, mode, status, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (time.time(), signal["id"], signal["title"], signal["outcome"],
              usd, price, mode, status, detail[:500]))


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
            "WHERE ts >= ? AND status='ok' AND mode='live'", (midnight,)).fetchone()
    return float(row["s"])


# ── Position tracking ─────────────────────────────────────────────────────
def add_position(signal: dict, usd: float, price: float, token_id: str, mode: str):
    shares = usd / price if price > 0 else 0.0
    with db() as conn:
        conn.execute("""
            INSERT INTO positions (ts, signal_id, title, outcome, condition_id,
                outcome_index, token_id, mode, usd, entry_price, shares, last_price, pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (time.time(), signal["id"], signal["title"], signal["outcome"],
              signal["condition_id"], signal["outcome_index"], token_id, mode,
              usd, price, shares, price))


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
                  status: str = "open", closed: bool = False):
    with db() as conn:
        if closed:
            conn.execute("UPDATE positions SET last_price=?, pnl=?, status=?, closed_ts=? WHERE id=?",
                         (last_price, pnl, status, time.time(), pos_id))
        else:
            conn.execute("UPDATE positions SET last_price=?, pnl=? WHERE id=?",
                         (last_price, pnl, pos_id))


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


def performance_summary() -> dict:
    out = {}
    with db() as conn:
        for mode in ("dry_run", "live"):
            open_rows = conn.execute(
                "SELECT COALESCE(SUM(usd),0) c, COALESCE(SUM(pnl),0) u, COUNT(*) n "
                "FROM positions WHERE status='open' AND mode=?", (mode,)).fetchone()
            closed = conn.execute(
                "SELECT COALESCE(SUM(pnl),0) r, "
                "SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) w, "
                "SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) l "
                "FROM positions WHERE status!='open' AND mode=?", (mode,)).fetchone()
            wins, losses = closed["w"] or 0, closed["l"] or 0
            out[mode] = {
                "open_count": open_rows["n"], "open_cost": open_rows["c"],
                "unrealized": round(open_rows["u"], 2),
                "realized": round(closed["r"], 2),
                "total": round(open_rows["u"] + closed["r"], 2),
                "wins": wins, "losses": losses,
                "win_rate": round(wins / (wins + losses), 3) if (wins + losses) else None,
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
