"""SQLite state: log, search history, titles already seen."""
import json
import os
import sqlite3
import threading
import time

from . import config

_lock = threading.RLock()
_conn = None


def db():
    global _conn
    if _conn is None:
        os.makedirs(config.CONFIG_DIR, exist_ok=True)
        _conn = sqlite3.connect(os.path.join(config.CONFIG_DIR, "dubarr.db"), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript("""
        CREATE TABLE IF NOT EXISTS log(ts REAL, level TEXT, msg TEXT);
        CREATE INDEX IF NOT EXISTS log_ts ON log(ts);
        CREATE TABLE IF NOT EXISTS searches(app TEXT, item_id INTEGER, ts REAL, PRIMARY KEY(app, item_id));
        CREATE TABLE IF NOT EXISTS seen(app TEXT, item_id INTEGER, PRIMARY KEY(app, item_id));
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS audio(path TEXT PRIMARY KEY, mtime REAL, size INTEGER, status TEXT,
            detail TEXT, ts REAL);
        """)
    return _conn


def log(msg, level="info"):
    with _lock:
        db().execute("INSERT INTO log VALUES(?,?,?)", (time.time(), level, msg))
        db().execute("DELETE FROM log WHERE ts < ?", (time.time() - 90 * 86400,))
        db().commit()
    print(f"[{level}] {msg}", flush=True)


def recent_log(limit=200, level=None):
    with _lock:
        if level:
            q = db().execute("SELECT * FROM log WHERE level=? ORDER BY ts DESC LIMIT ?", (level, limit))
        else:
            q = db().execute("SELECT * FROM log ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in q]


def count_log_since(level, since, like="%"):
    with _lock:
        return db().execute("SELECT COUNT(*) FROM log WHERE level=? AND ts>=? AND msg LIKE ?",
                            (level, since, like)).fetchone()[0]


def kv_get(key, default=None):
    with _lock:
        r = db().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default


def kv_set(key, value):
    with _lock:
        db().execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, json.dumps(value)))
        db().commit()


def last_search(app, item_id):
    with _lock:
        r = db().execute("SELECT ts FROM searches WHERE app=? AND item_id=?", (app, item_id)).fetchone()
        return r[0] if r else 0


def mark_searched(app, ids):
    with _lock:
        now = time.time()
        db().executemany("INSERT OR REPLACE INTO searches VALUES(?,?,?)", [(app, i, now) for i in ids])
        db().commit()


def seen(app):
    with _lock:
        return {r[0] for r in db().execute("SELECT item_id FROM seen WHERE app=?", (app,))}


def mark_seen(app, ids):
    with _lock:
        db().executemany("INSERT OR IGNORE INTO seen VALUES(?,?)", [(app, i) for i in ids])
        db().commit()


def audio_state(path):
    with _lock:
        r = db().execute("SELECT mtime, size, status, detail FROM audio WHERE path=?", (path,)).fetchone()
        return dict(r) if r else None


def set_audio_state(path, mtime, size, status, detail=""):
    with _lock:
        db().execute("INSERT OR REPLACE INTO audio VALUES(?,?,?,?,?,?)",
                     (path, mtime, size, status, detail, time.time()))
        db().commit()


def audio_counts():
    with _lock:
        return {r[0]: r[1] for r in db().execute("SELECT status, COUNT(*) FROM audio GROUP BY status")}


def audio_rows(status, limit=500):
    with _lock:
        return [dict(r) for r in db().execute(
            "SELECT * FROM audio WHERE status=? ORDER BY ts DESC LIMIT ?", (status, limit))]
