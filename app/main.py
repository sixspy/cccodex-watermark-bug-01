from pathlib import Path

from blacksheep import Application, Content, Cookie, Request, Response
from blacksheep.server.responses import redirect
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import init_db
from app.services import (
    correct_pointer,
    get_batch,
    integrity,
    list_batches,
    list_correction_jobs,
    list_dirty_archive,
    list_reports,
    pointer_state,
    run_gap_report,
    stamp_batch,
    template_catalog,
    trace_downstream,
)
from app.services.errors import ServiceError

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-watermark-session")

app = Application()
app.serve_files(str(STATIC), root_path="/static")


@app.on_start
async def _on_start(_app) -> None:
    init_db()


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("watermark_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user != Config.ADMIN_USER:
        raise ServiceError("未登录", 401)
    return user


def json_response(data: dict, status: int = 200) -> Response:
    import json

    return Response(
        status,
        content=Content(
            b"application/json",
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
        ),
    )


def html_response(path: Path) -> Response:
    return Response(
        200,
        content=Content(b"text/html; charset=utf-8", path.read_bytes()),
    )


def _error(exc: ServiceError) -> Response:
    return json_response(
        {
            "ok": False,
            "error": exc.message,
            "message": exc.message,
            "status_code": exc.status_code,
        },
        exc.status_code,
    )


async def read_json_body(request: Request) -> dict:
    """Parse a JSON object body; an empty or malformed body is a plain 400.

    Without this, ``await request.json()`` returns ``None`` for an empty body and
    the next ``.get`` call raises AttributeError → 500, which is not白话音.
    """
    try:
        body = await request.json()
    except Exception:
        raise ServiceError("请求体不是合法 JSON") from None
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ServiceError("请求体必须是 JSON 对象")
    return body


@app.router.get("/")
async def root(request: Request):
    if _read_session(request).get("user") == Config.ADMIN_USER:
        return redirect("/app/")
    return redirect("/login")


@app.router.get("/login")
async def login_page():
    return html_response(STATIC / "login.html")


@app.router.get("/app/")
async def spa_page(request: Request):
    if _read_session(request).get("user") != Config.ADMIN_USER:
        return redirect("/login")
    return html_response(STATIC / "index.html")


@app.router.post("/api/login")
async def api_login(request: Request):
    try:
        body = await read_json_body(request)
    except ServiceError as exc:
        return _error(exc)
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json_response({"ok": False, "error": "账号或密码错误"}, 400)
    resp = json_response({"ok": True, "user": user})
    resp.set_cookie(
        Cookie(
            "watermark_session",
            serializer.dumps({"user": user}),
            http_only=True,
            path="/",
        )
    )
    return resp


@app.router.post("/api/logout")
async def api_logout():
    resp = json_response({"ok": True})
    resp.unset_cookie("watermark_session")
    return resp


@app.router.get("/api/meta")
async def api_meta(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return _error(exc)
    return json_response(
        {
            "lab_name": Config.LAB_NAME,
            "product_line": "cc-watermark-lab",
            "chain": ["水印模板", "批次盖印", "下游追索", "缺口对账"],
            "pointer_key": "tpl_ptr",
        }
    )


@app.router.get("/api/pointer")
async def api_pointer(request: Request):
    """指针目录：唯一键 + 列名同源 + 命运芯片。"""
    try:
        require_user(request)
    except ServiceError as exc:
        return _error(exc)
    return json_response(pointer_state())


@app.router.get("/api/pointer/integrity")
async def api_pointer_integrity(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return _error(exc)
    return json_response(integrity())


@app.router.post("/api/pointer/correct")
async def api_pointer_correct(request: Request):
    try:
        user = require_user(request)
        body = await read_json_body(request)
        result = correct_pointer(
            requested_pointer=body.get("pointer"),
            operator=user,
        )
    except ServiceError as exc:
        return _error(exc)
    return json_response({"ok": True, **result})


@app.router.get("/api/pointer/jobs")
async def api_pointer_jobs(request: Request):
    """编排任务落档：纠偏任务记录。"""
    try:
        require_user(request)
    except ServiceError as exc:
        return _error(exc)
    return json_response({"items": list_correction_jobs()})


@app.router.get("/api/pointer/archive")
async def api_pointer_archive(request: Request):
    """只读历史：已处置脏印。"""
    try:
        require_user(request)
    except ServiceError as exc:
        return _error(exc)
    return json_response({"items": list_dirty_archive()})


@app.router.get("/api/templates")
async def api_templates(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return _error(exc)
    status = request.query.get("status")
    status_val = status[0] if status else None
    catalog = template_catalog(status=status_val)
    return json_response(
        {"items": catalog["items"], "pointer": catalog["pointer"]}
    )


@app.router.get("/api/batches")
async def api_batches(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return _error(exc)
    status = request.query.get("status")
    status_val = status[0] if status else None
    return json_response({"items": list_batches(status=status_val)})


@app.router.get("/api/batches/{batch_id}")
async def api_batch_detail(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return _error(exc)
    batch_id = int(request.route_values["batch_id"])
    batch = get_batch(batch_id)
    if not batch:
        return json_response({"error": "批次不存在"}, 404)
    return json_response({"batch": batch})


@app.router.post("/api/batches/{batch_id}/stamp")
async def api_stamp(request: Request):
    try:
        require_user(request)
        batch_id = int(request.route_values["batch_id"])
        body = await read_json_body(request)
        batch = stamp_batch(batch_id, str(body.get("watermark_value", "")))
    except ServiceError as exc:
        return _error(exc)
    return json_response({"ok": True, "batch": batch})


@app.router.post("/api/trace")
async def api_trace(request: Request):
    try:
        require_user(request)
        body = await read_json_body(request)
        result = trace_downstream(str(body.get("watermark_value", "")))
    except ServiceError as exc:
        return _error(exc)
    return json_response({"ok": True, **result})


@app.router.get("/api/gap-reports")
async def api_gap_list(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return _error(exc)
    return json_response({"items": list_reports()})


@app.router.post("/api/gap-reports")
async def api_gap_run(request: Request):
    try:
        require_user(request)
        body = await read_json_body(request)
        report = run_gap_report(str(body.get("as_of_date", "2026-11-16")))
    except ServiceError as exc:
        return _error(exc)
    return json_response({"ok": True, "report": report})
