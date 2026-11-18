from app.db import get_db
from app.services.errors import ServiceError
from app.services.pointers import stamp_ptr


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
        return [dict(r) for r in rows]


def get_batch(batch_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        return dict(row) if row else None


def stamp_batch(
    batch_id: int,
    watermark_value: str,
    db_path: str | None = None,
) -> dict:
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")
    with get_db(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] == "已盖印":
            raise ServiceError("批次已盖印", 409)
        if batch["status"] != "待盖印":
            raise ServiceError("仅待盖印批次可盖印", 409)
        ptr = stamp_ptr(db_path)
        tpl = conn.execute(
            "SELECT * FROM watermark_templates WHERE table_name=? AND status='现行' "
            "AND column_name=?",
            (batch["table_name"], ptr),
        ).fetchone()
        if not tpl:
            # silent: fall back to any current without aligning list pointer
            tpl = conn.execute(
                "SELECT * FROM watermark_templates WHERE table_name=? AND status='现行'",
                (batch["table_name"],),
            ).fetchone()
        if not tpl:
            raise ServiceError("无现行水印模板", 409)
        # silent: stamp writes using stamp pointer column, may disagree with list
        conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value=?, "
            "stamped_at=datetime('now') WHERE id=?",
            (f"{value}@{ptr}", batch_id),
        )
        row = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        return dict(row)
