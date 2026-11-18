from app.db import get_db
from app.services.errors import ServiceError
from app.services.pointers import is_dirty_stamp, read_pointer, split_stamp


def _column_check(conn, ptr: str, table_names: set[str]) -> list[dict]:
    checks = []
    for table in sorted(table_names):
        row = conn.execute(
            "SELECT column_name FROM watermark_templates WHERE table_name=? AND status='现行'",
            (table,),
        ).fetchone()
        column = row["column_name"] if row else None
        # A table that never had a current template is not a column-name drift;
        # only a current template whose column disagrees with the pointer is.
        checks.append(
            {
                "table_name": table,
                "column_name": column,
                "has_template": column is not None,
                "pointer": ptr,
                "aligned": column is None or column == ptr,
            }
        )
    return checks


def _annotate(conn, ptr: str, row) -> dict:
    item = dict(row)
    base, tag = split_stamp(item.get("watermark_value"))
    item["watermark_base"] = base
    item["watermark_column"] = tag
    item["dirty"] = (
        item.get("status") == "已盖印"
        and is_dirty_stamp(item.get("watermark_value"), ptr)
    )
    return item


def trace_downstream(watermark_value: str, db_path: str | None = None) -> dict:
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")
    with get_db(db_path) as conn:
        # Trace re-verifies the pointer and column alignment on its own; it does
        # not borrow the stamp path's verdict.
        ptr = read_pointer(conn)
        sources_raw = conn.execute(
            "SELECT * FROM run_batches WHERE watermark_value=? ORDER BY id",
            (value,),
        ).fetchall()
        if not sources_raw:
            raise ServiceError("未找到该水印值的盖印批次", 404)
        codes = [s["run_code"] for s in sources_raw]
        placeholders = ",".join("?" for _ in codes)
        edges = conn.execute(
            f"SELECT * FROM lineage_edges WHERE upstream_run_code IN ({placeholders})",
            codes,
        ).fetchall()
        down_codes = sorted({e["downstream_run_code"] for e in edges})
        downstream = []
        if down_codes:
            ph2 = ",".join("?" for _ in down_codes)
            downstream = [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM run_batches WHERE run_code IN ({ph2}) ORDER BY id",
                    down_codes,
                ).fetchall()
            ]

        tables = {s["table_name"] for s in sources_raw}
        tables |= {d["table_name"] for d in downstream}
        checks = _column_check(conn, ptr, tables)
        drifted = [c for c in checks if not c["aligned"]]
        if drifted:
            detail = "；".join(
                f"{c['table_name']} 现行列 {c['column_name']} ≠ 指针 {ptr}"
                for c in drifted
            )
            raise ServiceError(
                f"追索列名漂移：{detail}。列名未与唯一指针同源，追索已拦截；"
                "请先在指针目录点纠偏。",
                409,
            )

        sources = [_annotate(conn, ptr, s) for s in sources_raw]
        downstream_items = [_annotate(conn, ptr, d) for d in downstream]
        dirty = [s for s in sources if s["dirty"]]
        dirty += [d for d in downstream_items if d["dirty"]]
        if dirty:
            codes = "、".join(sorted({d["run_code"] for d in dirty}))
            raise ServiceError(
                f"追索列名漂移：批次 {codes} 的盖印列与唯一指针 {ptr} 不同源（脏印）。"
                "脏印不得留在追索链；请先在指针目录点纠偏回滚重盖。",
                409,
            )

        return {
            "watermark_value": value,
            "pointer": ptr,
            "pointer_key": "tpl_ptr",
            "column_checks": checks,
            "column_aligned": True,
            "sources": sources,
            "edges": [dict(e) for e in edges],
            "downstream": downstream_items,
            "dirty_count": 0,
        }
