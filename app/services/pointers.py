from app.db import get_db


def list_ptr(db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='tpl_ptr_list'").fetchone()
        return row["value"] if row else "wm_batch"


def stamp_ptr(db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='tpl_ptr_stamp'").fetchone()
        return row["value"] if row else "wm_trace"
