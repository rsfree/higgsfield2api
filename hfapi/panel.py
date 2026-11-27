"""运维面板：登录、任务台账、报文干跑、cookie 导入。

设计约束（都是踩过坑才这么定的）：

1. **面板口令与 API token 分离**：`PANEL_PASSWORD` 可用独立值，未设时复用 `SERVICE_TOKEN`。
   两者都为空 ⇒ 面板直接 503 关闭，绝不允许「无口令的 cookie 上传入口」存在。
2. **cookie 会话**：`hfpanel=<exp>.<HMAC-SHA256(口令, exp)>`，HttpOnly + SameSite=Strict。
   签名密钥是口令本身，改口令即吊销全部会话，不需要服务端 session 表。
3. **不在面板里放「开计费」开关**：ALLOW_BILLING 只读展示。改计费必须走 .env + 重启，
   这是刻意的摩擦——让「花钱」不会是误点一下。
4. **前端一律 textContent 渲染**：任务 prompt 是用户输入，innerHTML 会变成存储型 XSS。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request as HttpRequest
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .config import settings
from .convert import build_params, parse_request, preview
from .errors import CODE_AUTH, CODE_INVALID, ApiError
from .pool import pool
from .panel_html import DASHBOARD
from .session import cookies_from_curl, parse_cookies

COOKIE = "hfpanel"
TTL = 7 * 86400
MAX_BODY = 64 * 1024
SYNC_PROBE_LIMIT = 3   # 批量导入时同步自检的账号数上限

router = APIRouter()

# 登录失败限速（单进程内存计数，重启即清；够挡住脚本撞库）
_FAILS: dict[str, tuple[int, float]] = {}
MAX_FAILS, WINDOW = 8, 300


# --------------------------------------------------------------------------- 会话签名

def _mac(exp: int) -> str:
    key = settings.panel_password.encode()
    return hmac.new(key, f"panel:{exp}".encode(), hashlib.sha256).hexdigest()


def issue_token() -> str:
    exp = int(time.time()) + TTL
    return f"{exp}.{_mac(exp)}"


def token_valid(raw: str | None) -> bool:
    if not raw or raw.count(".") != 1:
        return False
    exp_s, mac = raw.split(".", 1)
    if not exp_s.isdigit() or int(exp_s) < time.time():
        return False
    return hmac.compare_digest(mac, _mac(int(exp_s)))


def _is_https(request: HttpRequest) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def _client_ip(request: HttpRequest) -> str:
    return (request.headers.get("x-forwarded-for") or request.client.host or "-").split(",")[0].strip()


def _blocked(ip: str) -> int:
    """返回剩余封禁秒数，0 = 未封。"""
    hits, first = _FAILS.get(ip, (0, 0.0))
    if hits < MAX_FAILS:
        return 0
    left = WINDOW - (time.time() - first)
    return int(left) if left > 0 else 0


def _note_fail(ip: str) -> None:
    hits, first = _FAILS.get(ip, (0, time.time()))
    if time.time() - first > WINDOW:
        hits, first = 0, time.time()
    _FAILS[ip] = (hits + 1, first)


# --------------------------------------------------------------------------- 守卫

async def guard(request: HttpRequest, call_next: Any) -> Any:
    path = request.url.path

    if path == "/panel/login":
        return await call_next(request)

    if not settings.panel_enabled:
        msg = "面板已禁用：未配置 PANEL_PASSWORD / SERVICE_TOKEN"
        if path.startswith("/panel/api/"):
            return JSONResponse(status_code=503, content={"error": {"code": "PanelDisabled", "message": msg}})
        return HTMLResponse(_disabled_html(msg), status_code=503)

    if not token_valid(request.cookies.get(COOKIE)):
        if path.startswith("/panel/api/"):
            return JSONResponse(
                status_code=401,
                content={"error": {"code": CODE_AUTH, "message": "面板未登录或会话已过期"}},
            )
        return RedirectResponse("/panel/login", status_code=303)

    return await call_next(request)


# --------------------------------------------------------------------------- 页面

@router.get("/panel", response_class=HTMLResponse)
@router.get("/panel/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD)


@router.get("/panel/login", response_class=HTMLResponse)
async def login_page(request: HttpRequest, e: str = "") -> Any:
    if token_valid(request.cookies.get(COOKIE)):
        return RedirectResponse("/panel", status_code=303)
    if not settings.panel_enabled:
        return HTMLResponse(_disabled_html("面板已禁用：未配置 PANEL_PASSWORD / SERVICE_TOKEN"), status_code=503)
    return HTMLResponse(_login_html(e))


async def _read_password(request: HttpRequest) -> str:
    raw = (await request.body())[:MAX_BODY].decode("utf-8", "replace")
    ctype = request.headers.get("content-type", "")
    if "json" in ctype:
        try:
            return str((json.loads(raw) or {}).get("password") or "")
        except Exception:
            return ""
    return (parse_qs(raw).get("password") or [""])[0]


@router.post("/panel/login")
async def do_login(request: HttpRequest) -> Any:
    ip = _client_ip(request)
    if not settings.panel_enabled:
        return HTMLResponse(_disabled_html("面板已禁用"), status_code=503)
    if _blocked(ip):
        return HTMLResponse(_login_html(f"尝试过于频繁，请 {_blocked(ip)} 秒后再试"), status_code=429)

    password = await _read_password(request)
    if not password or not hmac.compare_digest(password, settings.panel_password):
        _note_fail(ip)
        return HTMLResponse(_login_html("口令不正确"), status_code=401)

    _FAILS.pop(ip, None)
    resp = RedirectResponse("/panel", status_code=303)
    resp.set_cookie(
        COOKIE, issue_token(), max_age=TTL, path="/",
        httponly=True, samesite="strict", secure=_is_https(request),
    )
    return resp


@router.post("/panel/logout")
async def logout() -> Any:
    resp = RedirectResponse("/panel/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


# --------------------------------------------------------------------------- 面板 API

# --------------------------------------------------------------------------- 号池 API

def _split_segments(text: str) -> list[str]:
    """把一段粘贴内容切成多段。

    DevTools 的「Copy as cURL」多行时每行以 `\\` 续行、每段以 `curl ` 起头，
    所以按「行首 curl」切就能一批导入多个号。只有一段时原样返回。
    """
    parts = [p.strip() for p in re.split(r"(?m)^[ \t]*(?=curl\s)", text) if p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def _parse_one(segment: str) -> dict[str, str]:
    if re.search(r"-H\s+['\"]cookie:", segment, re.I) or re.search(r"(?:^|\s)(?:-b|--cookie)\s+['\"]", segment):
        return cookies_from_curl(segment)
    return parse_cookies(segment, source="面板输入")


def _bad_request(msg: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": {"code": CODE_INVALID, "message": msg}})


@router.post("/panel/api/accounts/probe-all")
async def probe_all() -> Any:
    """逐号零计费自检。串行——并发打上游更容易触发风控。"""
    results = await asyncio.to_thread(pool.probe_all)
    return {"probe": results, "accounts": [a.to_public() for a in pool.list()]}


@router.get("/panel/api/accounts")
async def list_accounts() -> dict[str, Any]:
    return {"pool": pool.summary(), "accounts": [a.to_public() for a in pool.list()]}


@router.post("/panel/api/accounts")
async def add_accounts(request: HttpRequest, name: str = "") -> Any:
    """新增账号。支持一次粘贴多段 cURL（按行首 curl 自动切分，批量导入）。"""
    raw = (await request.body())[:MAX_BODY].decode("utf-8", "replace").strip()
    if not raw:
        return _bad_request("内容为空：请粘贴 Copy as cURL 或 cookie 字符串")

    segments = _split_segments(raw)
    added: list[Any] = []
    errors: list[dict[str, Any]] = []
    for i, seg in enumerate(segments, 1):
        try:
            cookies = _parse_one(seg)
        except ApiError as exc:
            errors.append({"index": i, "detail": exc.message})
            continue
        label = f"{name}-{i}" if name and len(segments) > 1 else name
        added.append(pool.add(cookies, name=label))

    if not added:
        return JSONResponse(status_code=400, content={
            "error": {"code": CODE_INVALID, "message": "一段都没解析成功"},
            "errors": errors,
        })

    # 只同步自检前几个：一个连不通的号要等到超时，批量导入 10 个会让浏览器以为页面卡死。
    # 剩下的留「未验证」，用户点「全部自检」时走 4 路并发。
    results = []
    for acc in added[:SYNC_PROBE_LIMIT]:
        probe = await asyncio.to_thread(pool.probe, acc.id)
        results.append({"probe": probe, "account": acc.to_public()})
    for acc in added[SYNC_PROBE_LIMIT:]:
        results.append({"probe": None, "account": acc.to_public()})
    return {"added": len(added), "errors": errors, "results": results,
            "pending": max(0, len(added) - SYNC_PROBE_LIMIT),
            "accounts": [a.to_public() for a in pool.list()], "pool": pool.summary()}


@router.post("/panel/api/accounts/{account_id}/probe")
async def probe_one(account_id: str) -> Any:
    try:
        return {"probe": await asyncio.to_thread(pool.probe, account_id),
                "accounts": [a.to_public() for a in pool.list()]}
    except ApiError as exc:
        return JSONResponse(status_code=exc.http_status, content=exc.to_body())


@router.post("/panel/api/accounts/{account_id}/cookie")
async def replace_account_cookie(account_id: str, request: HttpRequest) -> Any:
    """换掉某号的 cookie（号过期最高频的修复动作），保留统计与任务归属。"""
    raw = (await request.body())[:MAX_BODY].decode("utf-8", "replace").strip()
    if not raw:
        return _bad_request("内容为空")
    segments = _split_segments(raw)
    try:
        cookies = _parse_one(segments[0])
        acc = pool.replace_cookies(account_id, cookies)
    except ApiError as exc:
        return JSONResponse(status_code=exc.http_status, content=exc.to_body())

    probe = await asyncio.to_thread(pool.probe, acc.id)
    return {
        "id": acc.id,
        "missing": [k for k in ("__client", "__client_uat") if k not in cookies],
        "probe": probe,
        "accounts": [a.to_public() for a in pool.list()],
    }


@router.post("/panel/api/accounts/{account_id}/toggle")
async def toggle_account(account_id: str, request: HttpRequest) -> Any:
    raw = (await request.body())[:4096].decode("utf-8", "replace").strip()
    try:
        enabled = bool(json.loads(raw or "{}").get("enabled"))
        pool.update(account_id, enabled=enabled)
    except ApiError as exc:
        return JSONResponse(status_code=exc.http_status, content=exc.to_body())
    except Exception:
        return _bad_request("请求体不是合法 JSON")
    return {"accounts": [a.to_public() for a in pool.list()], "pool": pool.summary()}


@router.post("/panel/api/accounts/{account_id}/rename")
async def rename_account(account_id: str, request: HttpRequest) -> Any:
    raw = (await request.body())[:4096].decode("utf-8", "replace").strip()
    try:
        name = str(json.loads(raw or "{}").get("name") or "")
        pool.update(account_id, name=name)
    except ApiError as exc:
        return JSONResponse(status_code=exc.http_status, content=exc.to_body())
    except Exception:
        return _bad_request("请求体不是合法 JSON")
    return {"accounts": [a.to_public() for a in pool.list()]}


@router.delete("/panel/api/accounts/{account_id}")
async def delete_account(account_id: str) -> Any:
    if not pool.remove(account_id):
        return JSONResponse(status_code=404, content={"error": {"code": "NotFound", "message": "账号不存在"}})
    return {"accounts": [a.to_public() for a in pool.list()], "pool": pool.summary()}


@router.post("/panel/api/reload")
async def reload_pool() -> Any:
    """从磁盘重读 data/accounts.json（手工改文件或多副本同步后用）。"""
    loaded = await asyncio.to_thread(pool.reload)
    return {**loaded, "accounts": [a.to_public() for a in pool.list()], "pool": pool.summary()}


@router.get("/panel/api/state")
async def state() -> dict[str, Any]:
    from . import server

    tasks = server.store.list(50)
    return {
        "server_time": int(time.time()),
        "flags": {
            "billing": settings.allow_billing,
            "auth": bool(settings.service_token),
            "use_free_gens": settings.use_free_gens,
            "max_duration": settings.max_duration,
            "max_batch": settings.max_batch,
            "expires_after": settings.execution_expires_after,
        },
        "pool": pool.summary(),
        "accounts": [a.to_public() for a in pool.list()],
        "tasks": tasks,
        "counts": {s: sum(1 for t in tasks if t.get("status") == s)
                   for s in ("queued", "running", "succeeded", "failed", "cancelled", "expired")},
    }


@router.get("/panel/api/task/{task_id}")
async def task_detail(task_id: str) -> Any:
    """转发到同一个查询实现，避免面板绕过 Bearer 鉴权直连 /tasks/{id}。"""
    from . import server

    try:
        return await server.get_task(task_id)
    except ApiError as exc:
        return JSONResponse(status_code=exc.http_status, content=exc.to_body())


@router.post("/panel/api/dry-run")
async def dry_run(request: HttpRequest) -> Any:
    raw = (await request.body())[:MAX_BODY]
    try:
        body = json.loads(raw or b"{}")
    except Exception:
        return JSONResponse(status_code=400, content={"error": {"code": CODE_INVALID, "message": "请求体不是合法 JSON"}})
    try:
        req = parse_request(body)
        return preview(req, build_params(req))
    except ApiError as exc:
        return JSONResponse(status_code=exc.http_status, content=exc.to_body())


@router.post("/panel/api/cookie")
async def upload_cookie_compat(request: HttpRequest) -> Any:
    """老接口别名：等价于新增账号。保留是为了不打断已有的 curl 脚本。"""
    return await add_accounts(request, name="")


# --------------------------------------------------------------------------- 模板

_DISABLED = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>面板已禁用</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f6f8;
font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:#1c1f23}
.card{background:#fff;border:1px solid #e3e6ea;border-radius:12px;padding:28px 32px;max-width:460px;box-shadow:0 2px 10px rgba(16,24,40,.06)}
h1{margin:0 0 10px;font-size:16px}code{background:#f2f4f7;padding:1px 5px;border-radius:4px}
</style></head><body><div class="card"><h1>面板已禁用</h1><p>{msg}</p>
<p>设置 <code>PANEL_PASSWORD</code>（或 <code>SERVICE_TOKEN</code>）后重启容器。</p></div></body></html>"""


def _disabled_html(msg: str) -> str:
    return _DISABLED.replace("{msg}", msg)


def _login_html(error: str = "") -> str:
    err = f'<p class="err">{error}</p>' if error else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · higgsfield2api</title><style>
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f6f8;color:#1c1f23;
font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
.card{{width:360px;background:#fff;border:1px solid #e3e6ea;border-radius:14px;padding:30px 28px;
box-shadow:0 6px 24px rgba(16,24,40,.08)}}
.brand{{display:flex;align-items:center;gap:10px;margin-bottom:4px}}
.dot{{width:10px;height:10px;border-radius:50%;background:#2f6fed}}
h1{{margin:0;font-size:17px;letter-spacing:.2px}}
p.sub{{margin:6px 0 22px;color:#6b7280;font-size:12.5px}}
label{{display:block;font-size:12px;color:#4b5563;margin-bottom:6px}}
input{{width:100%;padding:10px 12px;border:1px solid #d6dae1;border-radius:8px;font:inherit;outline:none}}
input:focus{{border-color:#2f6fed;box-shadow:0 0 0 3px rgba(47,111,237,.12)}}
button{{width:100%;margin-top:16px;padding:10px 12px;border:0;border-radius:8px;background:#2f6fed;color:#fff;
font:600 14px/1 inherit;cursor:pointer}}
button:hover{{background:#255ed4}}
.err{{margin:0 0 14px;padding:8px 10px;border-radius:8px;background:#fdecec;color:#b42318;font-size:12.5px}}
.hint{{margin-top:18px;padding-top:14px;border-top:1px dashed #e5e7eb;color:#9aa1ac;font-size:11.5px}}
</style></head><body><form class="card" method="post" action="/panel/login">
<div class="brand"><span class="dot"></span><h1>higgsfield2api</h1></div>
<p class="sub">MAGI-2 → 方舟视频协议 · 运维面板</p>{err}
<label for="p">登录口令</label>
<input id="p" name="password" type="password" autocomplete="current-password" autofocus required>
<button type="submit">进入面板</button>
<div class="hint">口令 = <code>PANEL_PASSWORD</code>，未单独配置时复用 <code>SERVICE_TOKEN</code>。连续失败 8 次锁定 5 分钟。</div>
</form></body></html>"""


