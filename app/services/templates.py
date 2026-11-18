from app.db import get_db
from app.services.pointers import list_ptr


def list_templates(db_path: str | None = None, status: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM watermark_templates WHERE status=? ORDER BY id",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM watermark_templates ORDER BY id"
            ).fetchall()
        items = [dict(r) for r in rows]
        # silent: list path tags current by list pointer only
        ptr = list_ptr(db_path)
        for it in items:
            it["pointer_tag"] = "现行指针" if it.get("column_name") == ptr else "旁路"
        return items
