from app.db import get_db, write_transaction
from app.services.errors import ServiceError
from app.services.pointers import (
    FATE,
    FATE_CHIP,
    is_dirty_stamp,
    read_pointer,
    split_stamp,
)


def _dirty_batches(conn, ptr: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM run_batches WHERE status='已盖印' ORDER BY id"
    ).fetchall()
    dirty = []
    for row in rows:
        item = dict(row)
        base, tag = split_stamp(item.get("watermark_value"))
        item["watermark_base"] = base
        item["watermark_column"] = tag
        if is_dirty_stamp(item.get("watermark_value"), ptr):
            dirty.append(item)
    return dirty


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM gap_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def run_gap_report(as_of_date: str, db_path: str | None = None) -> dict:
    day = (as_of_date or "").strip() or "2026-11-16"
    # BEGIN IMMEDIATE: the pointer cannot move between the column check and the
    # report insert, so a concurrent 纠偏 cannot land a report against a stale
    # pointer.  This is the reconciliation's own re-verification of alignment.
    with write_transaction(db_path) as conn:
        ptr = read_pointer(conn)
        current = conn.execute(
            "SELECT table_name, column_name FROM watermark_templates WHERE status='现行'"
        ).fetchall()
        drifted = [r for r in current if r["column_name"] != ptr]
        if drifted:
            detail = "；".join(
                f"{r['table_name']} 现行列 {r['column_name']}" for r in drifted
            )
            raise ServiceError(
                f"对账列名漂移：{detail} 与唯一指针 {ptr} 不同源，"
                "入库将对不上；请先在指针目录点纠偏再生成对账。",
                409,
            )

        expected = conn.execute(
            "SELECT COUNT(*) AS c FROM run_batches WHERE biz_date<=? AND status!='已作废'",
            (day,),
        ).fetchone()["c"]
        stamped = conn.execute(
            "SELECT COUNT(*) AS c FROM run_batches WHERE biz_date<=? AND status='已盖印'",
            (day,),
        ).fetchone()["c"]
        dirty = _dirty_batches(conn, ptr)
        dirty_count = len(dirty)
        gap = expected - stamped
        summary = (
            f"截至 {day}：应盖印 {expected}，已盖印 {stamped}，缺口 {gap}"
            f"（唯一指针 {ptr}）"
        )
        if dirty_count:
            summary += f"；脏印 {dirty_count} 笔待纠偏处置（{FATE}）"
        cur = conn.execute(
            "INSERT INTO gap_reports("
            "as_of_date, expected_count, stamped_count, gap_count, summary, "
            "pointer, pointer_key, fate, fate_chip, dirty_count"
            ") VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                day,
                expected,
                stamped,
                gap,
                summary,
                ptr,
                "tpl_ptr",
                FATE,
                FATE_CHIP,
                dirty_count,
            ),
        )
        row = conn.execute(
            "SELECT * FROM gap_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)
