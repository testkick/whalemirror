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
    "refresh_minutes": 30,
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
