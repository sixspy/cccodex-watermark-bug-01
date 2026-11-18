"""Single source of truth for the current watermark-template pointer.

The defect this module repairs: the lab used to carry *two* meta pointer keys
(``tpl_ptr_list`` for the template list, ``tpl_ptr_stamp`` for stamping) and
different code paths read different ones.  From now on there is exactly one
key, ``tpl_ptr``, and every path (catalog / stamping / trace / gap) reads it
through :func:`read_pointer`.
"""

from app.db import get_db
from app.services.errors import ServiceError

POINTER_KEY = "tpl_ptr"
LEGACY_POINTER_KEYS = ("tpl_ptr_list", "tpl_ptr_stamp")
ALL_POINTER_KEYS = (POINTER_KEY,) + LEGACY_POINTER_KEYS
DEFAULT_POINTER = "wm_batch"

# Dirty-stamp disposition fate is pinned to one engineering outcome:
# roll the whole dirty stamp back, then re-stamp under the unique pointer.
# This means a dirty stamp never remains in the trace chain.
FATE = "回滚重盖"
FATE_CHIP = "脏印回滚 · 按唯一指针重盖"
FATE_OPTIONS = [FATE]


def _pointer_rows(conn) -> list[dict]:
    marks = ",".join("?" for _ in ALL_POINTER_KEYS)
    rows = conn.execute(
        f"SELECT key, value FROM meta WHERE key IN ({marks}) ORDER BY key",
        ALL_POINTER_KEYS,
    ).fetchall()
    return [{"key": r["key"], "value": r["value"]} for r in rows]


def current_templates(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM watermark_templates WHERE status='现行' ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def _candidate(rows: list[dict], templates: list[dict]) -> str | None:
    """Pick a deterministic convergence target when the operator did not name one.

    Most template columns wins; then the canonical key; then list over stamp.
    """
    counts: dict[str, int] = {}
    for tpl in templates:
        counts[tpl["column_name"]] = counts.get(tpl["column_name"], 0) + 1
    best_value = None
    best_score = None
    for row in rows:
        score = (
            counts.get(row["value"], 0),
            1 if row["key"] == POINTER_KEY else 0,
            1 if row["key"] == "tpl_ptr_list" else 0,
        )
        if best_score is None or score > best_score:
            best_score = score
            best_value = row["value"]
    return best_value


def _state(conn) -> dict:
    rows = _pointer_rows(conn)
    templates = current_templates(conn)
    unique = len(rows) == 1
    if unique:
        pointer = rows[0]["value"]
    else:
        pointer = _candidate(rows, templates)

    columns = []
    for tpl in templates:
        columns.append(
            {
                "table_name": tpl["table_name"],
                "column_name": tpl["column_name"],
                "aligned": bool(pointer) and tpl["column_name"] == pointer,
            }
        )
    aligned_count = sum(1 for c in columns if c["aligned"])
    total = len(columns)

    if not rows:
        label = "缺少指针"
        message = "指针目录为空：没有任何现行模板指针，盖印/追索/对账均无法确认列名，请点纠偏按唯一指针写库。"
    elif not unique:
        listed = "、".join(f"{r['key']}={r['value']}" for r in rows)
        label = "双指针（已拦截）"
        message = (
            f"检测到 {len(rows)} 份现行模板指针（{listed}），双指针即拦："
            f"盖印、追索、对账已停止，请在指针目录点纠偏收敛为唯一指针。"
        )
    elif aligned_count != total:
        label = "唯一指针 · 列名漂移"
        message = (
            f"唯一指针 {pointer}，但 {total - aligned_count}/{total} 张现行模板列名与其不同源，"
            "相关表盖印会被拒绝，请点纠偏对齐列名。"
        )
    else:
        label = "唯一指针"
        message = f"唯一指针 {pointer}，{aligned_count}/{total} 张现行模板列名与其同源。"

    return {
        "key": POINTER_KEY,
        "pointer": pointer,
        "unique": unique,
        "label": label,
        "status_chip": label,
        "message": message,
        "keys": rows,
        "conflicts": rows if len(rows) > 1 else [],
        "columns": columns,
        "aligned_count": aligned_count,
        "template_count": total,
        "fate": FATE,
        "fate_chip": FATE_CHIP,
        "fate_options": FATE_OPTIONS,
    }


def pointer_state(conn=None, db_path: str | None = None) -> dict:
    if conn is not None:
        return _state(conn)
    with get_db(db_path) as own:
        return _state(own)


def integrity(db_path: str | None = None) -> dict:
    """Acceptance surface: pointer uniqueness, catalog and stamp path must agree.

    Deliberately goes through the *real* code paths — the catalog tagger
    (``list_templates``) and the writer's seam (``pointer_for_stamp``) — rather
    than re-reading one helper twice.  A fix that only rewrites catalog wording
    while the stamp path still reads its own pointer fails here.
    """
    # Imported lazily: templates/batches import this module at load time.
    from app.services.batches import pointer_for_stamp
    from app.services.templates import list_templates

    with get_db(db_path) as conn:
        state = _state(conn)
        stamp_ptr_value = None
        err = None
        try:
            stamp_ptr_value = pointer_for_stamp(conn)
        except ServiceError as exc:
            err = exc.message
        catalog_items = list_templates(db_path=db_path)
        tagged = {
            it["column_name"] for it in catalog_items if it.get("pointer_tag") == "现行指针"
        }
        # One unambiguous catalog pointer only when exactly one column is tagged.
        catalog_ptr = next(iter(tagged)) if len(tagged) == 1 else None
        aligned = state["aligned_count"] == state["template_count"]
        pointers_agree = bool(catalog_ptr) and catalog_ptr == stamp_ptr_value
        return {
            "pointer_key": POINTER_KEY,
            "pointer": state["pointer"],
            "unique": state["unique"],
            "pointer_keys": [r["key"] for r in state["keys"]],
            "catalog_pointer": catalog_ptr,
            "catalog_tagged": sorted(tagged),
            "stamp_pointer": stamp_ptr_value,
            "pointers_agree": pointers_agree,
            "column_aligned": aligned,
            "aligned_count": state["aligned_count"],
            "template_count": state["template_count"],
            # 指针唯一 + 目录与盖印读针同源 + 列名对齐, all three required.
            "ok": bool(
                state["unique"]
                and pointers_agree
                and aligned
                and err is None
            ),
            "fate": FATE,
            "fate_chip": FATE_CHIP,
            "message": state["message"],
        }


def read_pointer(conn, db_path: str | None = None) -> str:
    """Return the one current pointer; refuse to guess when the state is broken."""
    rows = _pointer_rows(conn)
    if not rows:
        raise ServiceError(
            "指针目录为空：没有现行模板指针。请点纠偏，按唯一指针一次写库后再盖印。",
            409,
        )
    if len(rows) > 1:
        listed = "、".join(f"{r['key']}={r['value']}" for r in rows)
        raise ServiceError(
            f"双指针已拦截：检测到 {len(rows)} 份现行模板指针（{listed}）。"
            "盖印不能猜着读指针，请先在指针目录点纠偏，收敛为唯一指针。",
            409,
        )
    return rows[0]["value"]


def split_stamp(value: str | None) -> tuple[str, str | None]:
    """Split ``BASE@column`` into base and column tag.

    A value with no ``@`` predates tagging and yields ``tag=None``.
    """
    if value is None:
        return "", None
    if "@" in value:
        base, tag = value.rsplit("@", 1)
        return base, tag
    return value, None


def is_dirty_stamp(value: str | None, pointer: str | None) -> bool:
    """A dirty stamp is a stamped value whose column tag disagrees with the
    unique pointer (or whose base is empty).  Untagged legacy values are not
    dirty — they predate column tagging and are traced as-is."""
    if value is None or not str(value).strip():
        return True
    base, tag = split_stamp(value)
    if not base:
        return True
    if tag is None:
        return False
    return pointer is None or tag != pointer


# ---------------------------------------------------------------------------
# Backwards-compatible aliases.  Both deliberately route to the same reader so
# the list and the stamp path can never read different pointers again.
# ---------------------------------------------------------------------------


def list_ptr(db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        return read_pointer(conn)


def stamp_ptr(db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        return read_pointer(conn)
