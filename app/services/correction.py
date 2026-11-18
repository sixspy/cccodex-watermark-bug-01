"""Pointer correction: converge on one pointer and settle dirty stamps.

Two duty officers can click 纠偏 at the same time.  The whole unit runs inside
``BEGIN IMMEDIATE`` (see :func:`app.db.write_transaction`) so the second click
waits for the first and then observes an already-converged pointer instead of
writing a second pointer value.

Dirty-stamp fate is pinned to one engineering outcome:

    FATE = 回滚重盖

Every dirty stamp is rolled back to ``待盖印`` and re-stamped under the unique
pointer, so no dirty stamp remains in the trace chain.  The old dirty value is
kept in ``dirty_stamp_archive`` as read-only history (that table is not part of
any trace query), but it is no longer a live traced watermark.
"""

from app.db import get_db, write_transaction
from app.services.errors import ServiceError
from app.services.pointers import (
    FATE,
    FATE_CHIP,
    POINTER_KEY,
    is_dirty_stamp,
    split_stamp,
)


def _resolve_target(conn, requested: str | None) -> str:
    """Pick the one pointer value to converge on.

    An explicit request wins only if a current template actually carries that
    column name; otherwise the service refuses with plain language rather than
    guessing.  With no request, the value already present under the canonical
    key wins, then the most common current column name.
    """
    current = [
        dict(r)
        for r in conn.execute(
            "SELECT table_name, column_name FROM watermark_templates "
            "WHERE status='现行' ORDER BY id"
        ).fetchall()
    ]
    if not current:
        raise ServiceError(
            "没有任何现行水印模板，无法确定唯一指针；请先登记模板再纠偏。", 409
        )
    valid = {t["column_name"] for t in current}

    if requested:
        target = requested.strip()
        if target not in valid:
            allowed = "、".join(sorted(valid))
            raise ServiceError(
                f"纠偏失败：请求的指针 {target or '（空）'} 不是任何现行模板的列名。"
                f"可选的唯一列名只有：{allowed}。",
                400,
            )
        return target

    row = conn.execute(
        "SELECT value FROM meta WHERE key=?", (POINTER_KEY,)
    ).fetchone()
    if row and row["value"] in valid:
        return row["value"]

    counts: dict[str, int] = {}
    for tpl in current:
        counts[tpl["column_name"]] = counts.get(tpl["column_name"], 0) + 1
    # Deterministic: most templates first, then lexical order.
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _dirty_batches(conn, ptr: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM run_batches WHERE status='已盖印' ORDER BY id"
    ).fetchall()
    return [
        dict(r)
        for r in rows
        if is_dirty_stamp(r["watermark_value"], ptr)
    ]


def _is_converged(conn, ptr: str, dirty: list[dict]) -> bool:
    keys = conn.execute(
        "SELECT key FROM meta WHERE key IN ('tpl_ptr','tpl_ptr_list','tpl_ptr_stamp')"
    ).fetchall()
    if len(keys) != 1 or keys[0]["key"] != POINTER_KEY:
        return False
    drifted = conn.execute(
        "SELECT COUNT(*) AS c FROM watermark_templates "
        "WHERE status='现行' AND column_name<>?",
        (ptr,),
    ).fetchone()["c"]
    return drifted == 0 and not dirty


def list_correction_jobs(db_path: str | None = None, limit: int = 20) -> list[dict]:
    """编排任务落档：every 纠偏 run is recorded with its fate and counts."""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM pointer_correction_jobs ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]


def list_dirty_archive(db_path: str | None = None, limit: int = 50) -> list[dict]:
    """Read-only history of old dirty stamps.

    Under the pinned fate (回滚重盖) these rows are *only* history: no trace
    query reads this table, so a dirty stamp is not in the trace chain.
    """
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM dirty_stamp_archive ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]


def correct_pointer(
    db_path: str | None = None,
    requested_pointer: str | None = None,
    operator: str | None = None,
) -> dict:
    try:
        with write_transaction(db_path) as conn:
            target = _resolve_target(conn, requested_pointer)
            dirty = _dirty_batches(conn, target)

            if _is_converged(conn, target, dirty):
                return {
                    "ok": True,
                    "changed": False,
                    "pointer": target,
                    "pointer_key": POINTER_KEY,
                    "fate": FATE,
                    "fate_chip": FATE_CHIP,
                    "message": (
                        f"已收敛：唯一指针 {target}，列名全部同源，"
                        "没有需要处置的脏印。"
                    ),
                    "job": None,
                }

            # 1) One pointer row, written exactly once inside this transaction.
            conn.execute(
                "DELETE FROM meta WHERE key IN ('tpl_ptr','tpl_ptr_list','tpl_ptr_stamp')"
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?)", (POINTER_KEY, target)
            )

            # 2) Stamp columns and the pointer must be same-source: align every
            #    current template's column to the unique pointer.
            conn.execute(
                "UPDATE watermark_templates SET column_name=? "
                "WHERE status='现行' AND column_name<>?",
                (target, target),
            )
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM watermark_templates WHERE status='现行'"
            ).fetchone()["c"]
            stamp_cols = conn.execute(
                "SELECT COUNT(*) AS c FROM watermark_templates "
                "WHERE status='现行' AND column_name=?",
                (target,),
            ).fetchone()["c"]

            # 3) Dirty stamps: archive, roll back, re-stamp under the unique
            #    pointer.  They leave the live trace chain in this same unit.
            restamped = 0
            pending = 0
            archived_ids: list[int] = []
            for batch in dirty:
                base, tag = split_stamp(batch["watermark_value"])
                new_value = f"{base}@{target}" if base else None
                arch = conn.execute(
                    "INSERT INTO dirty_stamp_archive("
                    "batch_id, run_code, table_name, old_watermark, new_watermark, "
                    "reason, pointer, fate"
                    ") VALUES (?,?,?,?,?,?,?,?)",
                    (
                        batch["id"],
                        batch["run_code"],
                        batch["table_name"],
                        batch["watermark_value"] or "",
                        new_value,
                        f"盖印列 {tag or '（空）'} 与唯一指针 {target} 不同源",
                        target,
                        FATE,
                    ),
                )
                archived_ids.append(arch.lastrowid)
                if new_value:
                    conn.execute(
                        "UPDATE run_batches SET status='已盖印', watermark_value=?, "
                        "stamped_at=datetime('now') WHERE id=?",
                        (new_value, batch["id"]),
                    )
                    restamped += 1
                else:
                    # Nothing left to re-stamp: return the batch to the queue.
                    conn.execute(
                        "UPDATE run_batches SET status='待盖印', watermark_value=NULL, "
                        "stamped_at=NULL WHERE id=?",
                        (batch["id"],),
                    )
                    pending += 1

            message = (
                f"纠偏完成：唯一指针 {target} 已一次写库；"
                f"现行模板 {stamp_cols}/{total} 张列名与其同源；"
                f"脏印 {len(dirty)} 笔已回滚并按唯一指针重盖 {restamped} 笔"
                + (f"，退回待盖印 {pending} 笔" if pending else "")
                + "。"
            )
            cur = conn.execute(
                "INSERT INTO pointer_correction_jobs("
                "pointer, pointer_key, fate, fate_chip, dirty_count, "
                "restamped_count, pending_count, aligned_count, template_count, "
                "status, message, operator"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    target,
                    POINTER_KEY,
                    FATE,
                    FATE_CHIP,
                    len(dirty),
                    restamped,
                    pending,
                    stamp_cols,
                    total,
                    "已完成",
                    message,
                    operator,
                ),
            )
            job_id = cur.lastrowid
            for arch_id in archived_ids:
                conn.execute(
                    "UPDATE dirty_stamp_archive SET job_id=? WHERE id=?",
                    (job_id, arch_id),
                )
            job = conn.execute(
                "SELECT * FROM pointer_correction_jobs WHERE id=?", (job_id,)
            ).fetchone()
            return {
                "ok": True,
                "changed": True,
                "pointer": target,
                "pointer_key": POINTER_KEY,
                "fate": FATE,
                "fate_chip": FATE_CHIP,
                "message": message,
                "job": dict(job),
            }
    except ServiceError:
        raise
    except Exception as exc:  # translate infrastructure failure to plain language
        raise ServiceError(f"纠偏写库失败：{exc}。请稍后重试，数据未改动。", 500) from exc
