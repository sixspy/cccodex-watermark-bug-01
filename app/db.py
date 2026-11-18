import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(
    db_path: str | None = None,
    *,
    isolation_level: str | None = "",
) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path,
        check_same_thread=False,
        isolation_level=isolation_level,
        timeout=15,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_transaction(db_path: str | None = None):
    """Run a read-check-write unit under SQLite's write lock.

    ``BEGIN IMMEDIATE`` is taken before the first read, so two duty officers
    clicking ``纠偏`` at the same time cannot both decide they own the pointer
    row.  Without this lock both would pass the check and double-write.
    """
    conn = connect(db_path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS watermark_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  column_name TEXT NOT NULL,
  version_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('现行','停用')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS run_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  run_code TEXT NOT NULL UNIQUE,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待盖印','已盖印','已作废')),
  watermark_value TEXT,
  stamped_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_run_code TEXT NOT NULL,
  downstream_run_code TEXT NOT NULL,
  UNIQUE(upstream_run_code, downstream_run_code)
);

CREATE TABLE IF NOT EXISTS gap_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  expected_count INTEGER NOT NULL,
  stamped_count INTEGER NOT NULL,
  gap_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  pointer TEXT,
  pointer_key TEXT,
  fate TEXT,
  fate_chip TEXT,
  dirty_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dirty_stamp_archive (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  run_code TEXT NOT NULL,
  table_name TEXT NOT NULL,
  old_watermark TEXT NOT NULL,
  new_watermark TEXT,
  reason TEXT NOT NULL,
  pointer TEXT NOT NULL,
  fate TEXT NOT NULL,
  job_id INTEGER,
  archived_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pointer_correction_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pointer TEXT NOT NULL,
  pointer_key TEXT NOT NULL,
  fate TEXT NOT NULL,
  fate_chip TEXT NOT NULL,
  dirty_count INTEGER NOT NULL DEFAULT 0,
  restamped_count INTEGER NOT NULL DEFAULT 0,
  pending_count INTEGER NOT NULL DEFAULT 0,
  aligned_count INTEGER NOT NULL DEFAULT 0,
  template_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  message TEXT NOT NULL,
  operator TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SEED_TEMPLATES = [
    ("dw_orders_d", "wm_batch", 1, "现行"),
    ("dw_payments_d", "wm_batch", 1, "现行"),
    ("dm_gmv_d", "wm_batch", 1, "现行"),
]

SEED_BATCHES = [
    ("dw_orders_d", "ord-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_payments_d", "pay-20261114-01", "2026-11-14", "待盖印", None),
    ("dm_gmv_d", "gmv-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_orders_d", "ord-20261113-01", "2026-11-13", "已盖印", "WM-ORD-131"),
]

SEED_EDGES = [
    ("ord-20261114-01", "gmv-20261114-01"),
    ("pay-20261114-01", "gmv-20261114-01"),
    ("ord-20261113-01", "gmv-20261114-01"),
]

POINTER_KEY = "tpl_ptr"
LEGACY_POINTER_KEYS = ("tpl_ptr_list", "tpl_ptr_stamp")
DEFAULT_POINTER = "wm_batch"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """CREATE IF NOT EXISTS does not evolve an existing table, so补齐旧库列。"""
    wanted = {
        "gap_reports": {
            "pointer": "TEXT",
            "pointer_key": "TEXT",
            "fate": "TEXT",
            "fate_chip": "TEXT",
            "dirty_count": "INTEGER NOT NULL DEFAULT 0",
        }
    }
    for table, columns in wanted.items():
        existing = _table_columns(conn, table)
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _pointer_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    keys = (POINTER_KEY,) + LEGACY_POINTER_KEYS
    marks = ",".join("?" for _ in keys)
    return conn.execute(
        f"SELECT key, value FROM meta WHERE key IN ({marks}) ORDER BY key", keys
    ).fetchall()


def _migrate_pointers(conn: sqlite3.Connection) -> None:
    """Promote a lone legacy pointer, but never hide a genuine dual-pointer
    conflict.  A conflicting volume must stay visible until the operator clicks
    纠偏; silently collapsing it here would mask the defect."""
    rows = _pointer_rows(conn)
    if not rows:
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            (POINTER_KEY, DEFAULT_POINTER),
        )
        return
    if len(rows) == 1 and rows[0]["key"] != POINTER_KEY:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (POINTER_KEY, rows[0]["value"]),
        )
        conn.execute("DELETE FROM meta WHERE key=?", (rows[0]["key"],))


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if row:
            _migrate_pointers(conn)
            return
        for table, col, ver, status in SEED_TEMPLATES:
            conn.execute(
                "INSERT INTO watermark_templates(table_name, column_name, version_no, status) "
                "VALUES (?,?,?,?)",
                (table, col, ver, status),
            )
        for table, code, biz, status, wm in SEED_BATCHES:
            conn.execute(
                "INSERT INTO run_batches(table_name, run_code, biz_date, status, watermark_value, stamped_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    table,
                    code,
                    biz,
                    status,
                    wm,
                    "2026-11-13 18:00:00" if status == "已盖印" else None,
                ),
            )
        for up, down in SEED_EDGES:
            conn.execute(
                "INSERT INTO lineage_edges(upstream_run_code, downstream_run_code) VALUES (?,?)",
                (up, down),
            )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
        # Exactly one pointer key.  tpl_ptr_list / tpl_ptr_stamp are the old
        # two-pointer defect and are deliberately never seeded again.
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            (POINTER_KEY, DEFAULT_POINTER),
        )
        _migrate_pointers(conn)
