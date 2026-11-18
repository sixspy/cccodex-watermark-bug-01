from app.db import get_db, write_transaction
from app.services.errors import ServiceError
from app.services.pointers import is_dirty_stamp, read_pointer, split_stamp


def pointer_for_stamp(conn) -> str:
    """The pointer the stamp path actually uses.

    Kept as a named seam so the acceptance check can call the same function the
    writer calls; if someone reintroduces a separate stamp pointer key here, the
    integrity check sees it immediately.
    """
    return read_pointer(conn)


def _resolve_template(conn, table_name: str, ptr: str) -> dict:
    tpl = conn.execute(
        "SELECT * FROM watermark_templates WHERE table_name=? AND status='现行' "
        "AND column_name=?",
        (table_name, ptr),
    ).fetchone()
    if tpl:
        return dict(tpl)
    row = conn.execute(
        "SELECT * FROM watermark_templates WHERE table_name=? AND status='现行'",
        (table_name,),
    ).fetchone()
    if row is None:
        raise ServiceError(f"表 {table_name} 无现行水印模板，无法盖印", 409)
    raise ServiceError(
        f"模板列名漂移：表 {table_name} 的现行列是 {row['column_name']}，"
        f"唯一指针是 {ptr}。列名未对齐，拒绝盖印；请先在指针目录点纠偏。",
        409,
    )


def list_batches(db_path: str | None = None, status: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM run_batches WHERE status=? ORDER BY id",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM run_batches ORDER BY id"
            ).fetchall()
        try:
            ptr = read_pointer(conn)
        except ServiceError:
            ptr = None
        items = []
        for r in rows:
            item = dict(r)
            item["watermark_base"], item["watermark_column"] = split_stamp(
                item.get("watermark_value")
            )
            item["dirty"] = (
                item.get("status") == "已盖印"
                and is_dirty_stamp(item.get("watermark_value"), ptr)
            )
            items.append(item)
        return items


def get_batch(batch_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["watermark_base"], item["watermark_column"] = split_stamp(
            item.get("watermark_value")
        )
        try:
            ptr = read_pointer(conn)
        except ServiceError:
            ptr = None
        item["dirty"] = (
            item.get("status") == "已盖印"
            and is_dirty_stamp(item.get("watermark_value"), ptr)
        )
        return item


def stamp_batch(
    batch_id: int,
    watermark_value: str,
    db_path: str | None = None,
) -> dict:
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")
    # One immediate transaction covers the pointer read, the template check and
    # the write, so a concurrent 纠偏 cannot move the pointer under the stamp.
    with write_transaction(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] == "已盖印":
            raise ServiceError("批次已盖印", 409)
        if batch["status"] != "待盖印":
            raise ServiceError("仅待盖印批次可盖印", 409)
        ptr = pointer_for_stamp(conn)
        tpl = _resolve_template(conn, batch["table_name"], ptr)
        conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value=?, "
            "stamped_at=datetime('now') WHERE id=?",
            (f"{value}@{tpl['column_name']}", batch_id),
        )
        row = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        item = dict(row)
        item["watermark_base"], item["watermark_column"] = split_stamp(
            item.get("watermark_value")
        )
        item["pointer"] = ptr
        item["dirty"] = False
        return item
