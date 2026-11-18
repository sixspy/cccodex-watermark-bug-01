from app.db import get_db
from app.services.pointers import pointer_state, read_pointer
from app.services.errors import ServiceError


def _items(conn, status: str | None) -> list[dict]:
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
    try:
        ptr = read_pointer(conn)
    except ServiceError:
        ptr = None
    # pointer_tag and the stamp path read the *same* unique pointer; this is the
    # anti-regression hinge — a catalog-only fix shows here immediately.
    for it in items:
        aligned = bool(ptr) and it.get("column_name") == ptr
        it["pointer_aligned"] = aligned
        it["unique_key"] = aligned
        if ptr is None:
            it["pointer_tag"] = "指针异常"
        elif aligned:
            it["pointer_tag"] = "现行指针"
        else:
            it["pointer_tag"] = "列名漂移"
    return items


def list_templates(db_path: str | None = None, status: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        return _items(conn, status)


def template_catalog(db_path: str | None = None, status: str | None = None) -> dict:
    """Template catalog with its pointer-directory header.

    The catalog owns no second copy of the pointer: items and header are built
    from the same connection and the same :func:`read_pointer` call.
    """
    with get_db(db_path) as conn:
        items = _items(conn, status)
        state = pointer_state(conn)
    return {"items": items, "pointer": state}
