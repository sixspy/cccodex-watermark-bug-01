"""Regression tests for the single-pointer lockdown and 纠偏 flow.

The four surfaces named in the ticket are covered: 指针目录, 盖印列表,
追索详情, 对账水线.  Each test states the old failure mode in its docstring.
"""

import threading

import pytest

from app.config import Config
from app.db import get_db, init_db


def _path() -> str:
    return Config.database()


def _meta_keys() -> dict[str, str]:
    with get_db(_path()) as conn:
        return {
            r["key"]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE 'tpl_ptr%'"
            ).fetchall()
        }


def _plant_dual_pointers(list_value="wm_batch", stamp_value="wm_trace") -> None:
    """Reintroduce the planted defect on top of a healthy test volume."""
    with get_db(_path()) as conn:
        conn.execute("DELETE FROM meta WHERE key LIKE 'tpl_ptr%'")
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('tpl_ptr_list', ?)", (list_value,)
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('tpl_ptr_stamp', ?)", (stamp_value,)
        )


def _drift_templates() -> None:
    with get_db(_path()) as conn:
        conn.execute(
            "UPDATE watermark_templates SET column_name='wm_trace' WHERE table_name='dm_gmv_d'"
        )


def _stamp_dirty(run_code: str, value: str) -> None:
    with get_db(_path()) as conn:
        conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value=?, "
            "stamped_at=datetime('now') WHERE run_code=?",
            (value, run_code),
        )


@pytest.mark.asyncio
async def test_dual_pointer_blocks_every_surface(authed):
    """Old: list read east pointer, stamp read west pointer and silently fell back."""
    _plant_dual_pointers()

    pointer = await (await authed.get("/api/pointer")).json()
    assert pointer["unique"] is False
    assert pointer["status_chip"].startswith("双指针")
    assert {k["key"] for k in pointer["keys"]} == {"tpl_ptr_list", "tpl_ptr_stamp"}

    stamp = await authed.post(
        "/api/batches/1/stamp", json={"watermark_value": "WM-DUAL"}
    )
    assert stamp.status == 409
    body = await stamp.json()
    assert "双指针" in body["error"]
    assert "纠偏" in body["error"]

    trace = await authed.post("/api/trace", json={"watermark_value": "WM-ORD-131"})
    assert trace.status == 409
    assert "双指针" in (await trace.json())["error"]

    gap = await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})
    assert gap.status == 409
    assert "双指针" in (await gap.json())["error"]

    # 盖印列表 must not present a stampable batch under a broken pointer.
    batches = (await (await authed.get("/api/batches")).json())["items"]
    assert all(b["status"] != "已盖印" for b in batches if b["run_code"] == "ord-20261114-01")


@pytest.mark.asyncio
async def test_dual_pointer_blocks_a_pending_stamp(authed):
    """A genuinely pending batch must be refused, not stamped with a guess."""
    _plant_dual_pointers()
    pending = next(
        b
        for b in (await (await authed.get("/api/batches")).json())["items"]
        if b["status"] == "待盖印"
    )
    stamp = await authed.post(
        f"/api/batches/{pending['id']}/stamp", json={"watermark_value": "WM-DUAL"}
    )
    assert stamp.status == 409
    assert "双指针" in (await stamp.json())["error"]

    with get_db(_path()) as conn:
        row = conn.execute(
            "SELECT status, watermark_value FROM run_batches WHERE id=?",
            (pending["id"],),
        ).fetchone()
    assert row["status"] == "待盖印"
    assert row["watermark_value"] is None


@pytest.mark.asyncio
async def test_catalog_and_stamp_read_the_same_pointer(authed):
    """Acceptance hinge: a catalog-only text fix must not pass while stamp reads
    another pointer.  Here the two legacy keys disagree and the check must fail."""
    _plant_dual_pointers(list_value="wm_batch", stamp_value="wm_trace")
    integ = await (await authed.get("/api/pointer/integrity")).json()
    assert integ["ok"] is False
    assert integ["unique"] is False
    assert integ["pointers_agree"] is False

    await authed.post("/api/pointer/correct", json={"pointer": "wm_batch"})
    integ = await (await authed.get("/api/pointer/integrity")).json()
    assert integ["ok"] is True
    assert integ["unique"] is True
    assert integ["pointer_keys"] == ["tpl_ptr"]
    assert integ["catalog_pointer"] == integ["stamp_pointer"] == "wm_batch"
    assert integ["pointers_agree"] is True

    catalog = await (await authed.get("/api/templates")).json()
    assert catalog["pointer"]["unique"] is True
    tagged = [t for t in catalog["items"] if t["pointer_tag"] == "现行指针"]
    assert tagged and all(t["column_name"] == "wm_batch" for t in tagged)


@pytest.mark.asyncio
async def test_correction_converges_and_reports_fate(authed):
    _plant_dual_pointers("wm_batch", "wm_trace")
    _drift_templates()

    r = await authed.post("/api/pointer/correct", json={})
    assert r.status == 200
    body = await r.json()
    assert body["changed"] is True
    assert body["pointer"] == "wm_batch"
    assert body["fate"] == "回滚重盖"
    assert body["fate_chip"]
    assert "一次写库" in body["message"]

    # Exactly one pointer row, and every current template column agrees.
    assert _meta_keys() == {"tpl_ptr": "wm_batch"}
    with get_db(_path()) as conn:
        drifted = conn.execute(
            "SELECT COUNT(*) AS c FROM watermark_templates "
            "WHERE status='现行' AND column_name<>'wm_batch'"
        ).fetchone()["c"]
    assert drifted == 0

    # The corrected state stamps and reconciles normally.
    batches = (await (await authed.get("/api/batches")).json())["items"]
    pending = next(b for b in batches if b["status"] == "待盖印")
    stamp = await authed.post(
        f"/api/batches/{pending['id']}/stamp", json={"watermark_value": "WM-OK-1"}
    )
    assert stamp.status == 200
    assert (await stamp.json())["batch"]["watermark_column"] == "wm_batch"

    gap = await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})
    assert gap.status == 200
    report = (await gap.json())["report"]
    assert report["pointer"] == "wm_batch"
    assert report["fate_chip"]
    assert "唯一指针 wm_batch" in report["summary"]


@pytest.mark.asyncio
async def test_dirty_stamp_is_rolled_back_not_kept_in_trace(authed):
    """Chosen fate: dirty stamps are archived as read-only history, rolled back
    and re-stamped under the unique pointer — they do not stay in the trace chain."""
    _drift_templates()
    _stamp_dirty("ord-20261114-01", "WM-DIRTY-9@wm_trace")

    r = await authed.post("/api/pointer/correct", json={"pointer": "wm_batch"})
    body = await r.json()
    assert body["changed"] is True

    with get_db(_path()) as conn:
        row = conn.execute(
            "SELECT * FROM run_batches WHERE run_code='ord-20261114-01'"
        ).fetchone()
        archived = conn.execute(
            "SELECT * FROM dirty_stamp_archive WHERE run_code='ord-20261114-01'"
        ).fetchone()
    assert row["watermark_value"] == "WM-DIRTY-9@wm_batch"
    assert row["status"] == "已盖印"
    assert archived["old_watermark"] == "WM-DIRTY-9@wm_trace"
    assert archived["fate"] == "回滚重盖"

    # The dirty value is gone from the trace chain, the clean one is traceable.
    dirty_trace = await authed.post(
        "/api/trace", json={"watermark_value": "WM-DIRTY-9@wm_trace"}
    )
    assert dirty_trace.status == 404
    clean_trace = await authed.post(
        "/api/trace", json={"watermark_value": "WM-DIRTY-9@wm_batch"}
    )
    assert clean_trace.status == 200
    tbody = await clean_trace.json()
    assert tbody["sources"][0]["run_code"] == "ord-20261114-01"
    assert tbody["dirty_count"] == 0


@pytest.mark.asyncio
async def test_trace_and_gap_reverify_columns_themselves(authed):
    """Old: trace/gap never looked at template columns at all."""
    _drift_templates()
    trace = await authed.post("/api/trace", json={"watermark_value": "WM-ORD-131"})
    assert trace.status == 409
    assert "列名漂移" in (await trace.json())["error"]

    gap = await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})
    assert gap.status == 409
    assert "列名漂移" in (await gap.json())["error"]


@pytest.mark.asyncio
async def test_dirty_source_blocks_trace_before_correction(authed):
    """A dirty stamp must not travel through 追索详情 even if columns align."""
    _stamp_dirty("ord-20261114-01", "WM-DIRTY-9@wm_trace")  # tag ≠ wm_batch
    trace = await authed.post(
        "/api/trace", json={"watermark_value": "WM-DIRTY-9@wm_trace"}
    )
    assert trace.status == 409
    assert "脏印" in (await trace.json())["error"]


@pytest.mark.asyncio
async def test_correction_rejects_unknown_pointer_in_plain_language(authed):
    r = await authed.post("/api/pointer/correct", json={"pointer": "wm_nope"})
    assert r.status == 400
    body = await r.json()
    assert "wm_nope" in body["error"]
    assert "wm_batch" in body["error"]


@pytest.mark.asyncio
async def test_concurrent_corrections_do_not_double_write_pointer(authed):
    """Old: two duty officers clicking 纠偏 at once wrote two pointer rows.

    The whole unit sits behind BEGIN IMMEDIATE, so the second click serialises
    after the first and the meta table ends with exactly one pointer row.
    """
    _plant_dual_pointers("wm_batch", "wm_trace")
    _drift_templates()

    # Give both officers queues of pending batches so there is work to do.
    for i in range(6):
        with get_db(_path()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO run_batches(table_name, run_code, biz_date, status) "
                "VALUES ('dw_orders_d', ?, '2026-11-15', '待盖印')",
                (f"conc-{i}",),
            )

    from app.services import correct_pointer

    path = _path()
    errors: list[Exception] = []
    results: list[dict] = []

    def worker(target: str) -> None:
        try:
            results.append(correct_pointer(db_path=path, requested_pointer=target))
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("wm_batch",)),
        threading.Thread(target=worker, args=("wm_batch",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert len(results) == 2
    keys = _meta_keys()
    assert list(keys) == ["tpl_ptr"]
    assert keys["tpl_ptr"] == "wm_batch"
    with get_db(path) as conn:
        pointer_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM meta WHERE key LIKE 'tpl_ptr%'"
        ).fetchone()["c"]
        jobs = conn.execute(
            "SELECT COUNT(*) AS c FROM pointer_correction_jobs"
        ).fetchone()["c"]
        drifted = conn.execute(
            "SELECT COUNT(*) AS c FROM watermark_templates "
            "WHERE status='现行' AND column_name<>'wm_batch'"
        ).fetchone()["c"]
    assert pointer_rows == 1
    assert drifted == 0
    # Exactly one officer performed the write; the other observed convergence.
    assert jobs == 1


@pytest.mark.asyncio
async def test_catalog_only_fix_fails_acceptance(authed, monkeypatch):
    """若只改目录文案却不改盖印读针则验收失败.

    Simulate the half-fix: catalog renders the canonical pointer, but the stamp
    path is still wired to its own legacy key.  ``integrity`` must report
    ``pointers_agree == False`` and ``ok == False``.
    """
    import app.services.batches as batches_mod

    _migrate_to_canonical_list_only()
    monkeypatch.setattr(
        batches_mod, "pointer_for_stamp", lambda conn: "wm_trace"
    )

    integ = await (await authed.get("/api/pointer/integrity")).json()
    assert integ["unique"] is True
    assert integ["catalog_pointer"] == "wm_batch"
    assert integ["stamp_pointer"] == "wm_trace"
    assert integ["pointers_agree"] is False
    assert integ["ok"] is False


def _migrate_to_canonical_list_only() -> None:
    """Healthy canonical pointer key, but the stamp code path is what is stale."""
    with get_db(_path()) as conn:
        conn.execute("DELETE FROM meta WHERE key LIKE 'tpl_ptr%'")
        conn.execute("INSERT INTO meta(key, value) VALUES ('tpl_ptr','wm_batch')")


@pytest.mark.asyncio
async def test_legacy_single_pointer_is_promoted_on_start(tmp_path):
    """An old volume that only has tpl_ptr_list must heal to the canonical key."""
    db = tmp_path / "legacy.db"
    init_db(str(db))
    with get_db(str(db)) as conn:
        conn.execute("DELETE FROM meta WHERE key='tpl_ptr'")
        conn.execute("INSERT INTO meta(key, value) VALUES ('tpl_ptr_list','wm_batch')")
    init_db(str(db))
    with get_db(str(db)) as conn:
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'tpl_ptr%'"
        ).fetchall()
    assert [(r["key"], r["value"]) for r in rows] == [("tpl_ptr", "wm_batch")]


@pytest.mark.asyncio
async def test_empty_or_malformed_body_is_plain_error(authed, client):
    """纠偏失败必须吐白话；空/坏请求体不能变成 500."""
    # No auth → 401 regardless of body.
    for path in ("/api/pointer/correct", "/api/trace", "/api/gap-reports"):
        assert (await client.post(path)).status == 401

    # Empty body on correction means "converge with defaults": allowed.
    r = await authed.post_raw("/api/pointer/correct")
    assert r.status == 200
    assert "message" in await r.json()

    # Empty body on trace is a plain 400, not a 500.
    r = await authed.post_raw("/api/trace")
    assert r.status == 400
    assert "水印值" in (await r.json())["error"]

    # Malformed JSON is a plain 400.
    r = await authed.post_text("/api/trace", "{bad", "application/json")
    assert r.status == 400
    body = await r.json()
    assert body["ok"] is False
    assert "JSON" in body["error"]


@pytest.mark.asyncio
async def test_unauthenticated_pointer_endpoints(authed, client):
    for path in ("/api/pointer", "/api/pointer/integrity"):
        r = await client.get(path)
        assert r.status == 401


@pytest.mark.asyncio
async def test_correction_writes_job_and_links_archive(authed):
    """编排任务落档：the job row records counts and the archive points back."""
    _drift_templates()
    _stamp_dirty("ord-20261114-01", "WM-DIRTY-9@wm_trace")
    r = await authed.post("/api/pointer/correct", json={})
    body = await r.json()
    job = body["job"]
    assert job["fate"] == "回滚重盖"
    assert job["dirty_count"] == 1
    assert job["restamped_count"] == 1
    assert job["status"] == "已完成"
    assert job["operator"] == "admin"

    jobs = (await (await authed.get("/api/pointer/jobs")).json())["items"]
    assert jobs[0]["id"] == job["id"]

    archive = (await (await authed.get("/api/pointer/archive")).json())["items"]
    assert archive[0]["job_id"] == job["id"]
    assert archive[0]["old_watermark"] == "WM-DIRTY-9@wm_trace"


@pytest.mark.asyncio
async def test_correction_is_idempotent_when_already_converged(authed):
    # A fresh volume is already converged, so nothing is written.
    warm = await (await authed.post("/api/pointer/correct", json={})).json()
    assert warm["changed"] is False
    assert warm["job"] is None

    _drift_templates()
    first = await (await authed.post("/api/pointer/correct", json={})).json()
    assert first["changed"] is True
    second = await (await authed.post("/api/pointer/correct", json={})).json()
    assert second["changed"] is False
    assert second["job"] is None
    assert "已收敛" in second["message"]
    jobs = (await (await authed.get("/api/pointer/jobs")).json())["items"]
    assert len(jobs) == 1
