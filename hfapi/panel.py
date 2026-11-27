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
import os
import time
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request as HttpRequest
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .config import settings
from .convert import build_params, parse_request, preview
from .errors import CODE_AUTH, CODE_INVALID, ApiError
from .session import cookies_from_curl, load_cookies, parse_cookies

COOKIE = "hfpanel"
TTL = 7 * 86400
MAX_BODY = 64 * 1024

router = APIRouter()

# 登录失败限速（单进程内存计数，重启即清；够挡住脚本撞库）
_FAILS: dict[str, tuple[int, float]] = {}
MAX_FAILS, WINDOW = 8, 300

# 会话自检结果缓存，避免面板轮询把上游当靶子打
_SESSION: dict[str, Any] = {"at": 0.0, "value": None}
SESSION_TTL = 60


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

async def _session_snapshot(force: bool = False) -> dict[str, Any]:
    from . import server  # 延迟导入：server 反向依赖本模块，避免循环

    now = time.time()
    if not force and _SESSION["value"] is not None and now - _SESSION["at"] < SESSION_TTL:
        return _SESSION["value"]
    # whoami 是同步的 curl_cffi 调用：必须丢线程池，否则面板轮询会把事件循环锁死
    try:
        value = await asyncio.to_thread(server.upstream.session.whoami)
    except ApiError as exc:
        value = {"ok": False, "status": exc.http_status, "detail": exc.message}
    except Exception as exc:  # noqa: BLE001 网络层任何异常都不该把面板打挂
        value = {"ok": False, "status": 0, "detail": f"{type(exc).__name__}: {exc}"}
    _SESSION.update(at=now, value=value)
    return value


def _cookie_info() -> dict[str, Any]:
    """只读本地，不打上游：面板每 15 秒轮询一次，不能拿它当健康检查打。"""
    info: dict[str, Any] = {"present": False, "count": 0, "updated_at": None, "source": None}
    if os.getenv("HF_COOKIES_JSON", "").strip():
        try:
            info.update(present=True, count=len(load_cookies()), source="HF_COOKIES_JSON")
        except ApiError:
            info.update(present=True, source="HF_COOKIES_JSON", count=0)
        return info
    path = settings.cookie_path()
    if path.exists():
        try:
            info.update(
                present=True,
                count=len(parse_cookies(path.read_text(encoding="utf-8", errors="replace"), source=str(path))),
                updated_at=int(path.stat().st_mtime),
                source=str(path),
            )
        except ApiError:
            info.update(present=True, source=str(path), count=0)
    return info


@router.get("/panel/api/state")
async def state(force: int = 0) -> dict[str, Any]:
    from . import server

    tasks = server.store.list(50)
    if force:
        _SESSION["value"] = None
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
        "session": await _session_snapshot(),
        "cookie": _cookie_info(),
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
async def upload_cookie(request: HttpRequest) -> Any:
    """接受 Copy as cURL 文本 / 原始 cookie 头 / Cookie-Editor JSON，写盘并立即自检。"""
    from . import server

    raw = (await request.body())[:MAX_BODY].decode("utf-8", "replace").strip()
    if not raw:
        return JSONResponse(status_code=400, content={"error": {"code": CODE_INVALID, "message": "内容为空"}})

    try:
        cookies = cookies_from_curl(raw) if ("-H" in raw and "cookie:" in raw.lower()) else parse_cookies(raw, source="面板输入")
    except ApiError as exc:
        return JSONResponse(status_code=exc.http_status, content=exc.to_body())

    path = settings.cookie_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    path.chmod(0o600)

    server.upstream.session.set_cookies(cookies)
    _SESSION["value"] = None
    probe = await _session_snapshot(force=True)
    return {
        "written": str(path),
        "count": len(cookies),
        "missing": [k for k in ("__client", "__client_uat") if k not in cookies],
        "session": probe,
    }


@router.post("/panel/api/reload")
async def reload_cookie() -> Any:
    from . import server

    try:
        server.upstream.session.reload_cookies()
    except ApiError as exc:
        _SESSION.update(at=0.0, value=None)
        return JSONResponse(status_code=exc.http_status, content=exc.to_body())
    _SESSION.update(at=0.0, value=None)
    return {"reloaded": True, "session": await _session_snapshot(force=True)}


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


DASHBOARD = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>higgsfield2api 面板</title><style>
*{box-sizing:border-box}
:root{--bg:#f5f6f8;--card:#fff;--line:#e3e6ea;--ink:#1c1f23;--muted:#6b7280;--accent:#2f6fed;
--ok:#0f8a4d;--okbg:#e8f6ee;--warn:#a8620a;--warnbg:#fdf3e3;--bad:#b42318;--badbg:#fdecec;--run:#1f6fb2;--runbg:#e8f2fb}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;gap:14px;padding:14px 24px;background:#fff;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:9}
header h1{margin:0;font-size:15px;letter-spacing:.2px}
header .sp{flex:1}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600}
.pill.ok{background:var(--okbg);color:var(--ok)}.pill.warn{background:var(--warnbg);color:var(--warn)}
.pill.bad{background:var(--badbg);color:var(--bad)}.pill.run{background:var(--runbg);color:var(--run)}
.pill.off{background:#f0f1f3;color:var(--muted)}
.ghost{border:1px solid var(--line);background:#fff;color:#374151;padding:6px 12px;border-radius:8px;font:inherit;font-size:12.5px;cursor:pointer}
.ghost:hover{border-color:#c3c9d2;background:#fafbfc}
main{max-width:1180px;margin:0 auto;padding:22px 24px 60px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:18px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi .k{font-size:11.5px;color:var(--muted);letter-spacing:.3px;text-transform:uppercase}
.kpi .v{font-size:19px;font-weight:650;margin-top:5px}
.kpi .d{font-size:11.5px;color:var(--muted);margin-top:3px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:16px;overflow:hidden}
section > h2{margin:0;padding:13px 16px;font-size:13px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px}
section > h2 .tag{font-weight:400;color:var(--muted);font-size:11.5px}
.pad{padding:16px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;padding:9px 16px;background:#fafbfc;color:var(--muted);font-weight:600;font-size:11.5px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:10px 16px;border-bottom:1px solid #f1f3f5;vertical-align:top}
tr:last-child td{border-bottom:0}
tr.row:hover td{background:#fafbfd}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11.5px}
.prompt{max-width:420px;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;color:#374151}
.empty{padding:26px 16px;text-align:center;color:var(--muted);font-size:12.5px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:900px){.cols{grid-template-columns:1fr}}
label.f{display:block;font-size:11.5px;color:var(--muted);margin:0 0 5px}
textarea,input.t{width:100%;border:1px solid #d6dae1;border-radius:8px;padding:9px 11px;font:inherit;font-size:12.5px;outline:none;background:#fff}
textarea:focus,input.t:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(47,111,237,.1)}
textarea{min-height:92px;resize:vertical;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.actions{display:flex;gap:8px;align-items:center;margin-top:10px}
.btn{border:0;border-radius:8px;background:var(--accent);color:#fff;padding:8px 14px;font:600 13px/1 inherit;cursor:pointer}
.btn:hover{background:#255ed4}.btn[disabled]{opacity:.6;cursor:default}
pre.out{margin:12px 0 0;padding:12px;background:#0f1420;color:#d7e3f4;border-radius:8px;font-size:11.5px;line-height:1.55;
overflow:auto;max-height:300px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
.hint{font-size:11.5px;color:var(--muted);margin-top:8px}
footer{max-width:1180px;margin:0 auto;padding:0 24px 40px;color:#9aa1ac;font-size:11.5px}
code{background:#f2f4f7;padding:1px 5px;border-radius:4px;font-size:11.5px}
</style></head><body>
<header>
  <span class="pill run" style="width:10px;height:10px;padding:0"></span>
  <h1>higgsfield2api</h1>
  <span id="hdrFlags" class="hint"></span>
  <span class="sp"></span>
  <span id="hdrSession" class="pill off">会话检查中…</span>
  <button class="ghost" onclick="refresh(true)">刷新</button>
  <form method="post" action="/panel/logout" style="margin:0"><button class="ghost" type="submit">退出</button></form>
</header>
<main>
  <div class="kpis">
    <div class="kpi"><div class="k">计费</div><div class="v" id="kBilling">—</div><div class="d" id="kBillingSub"></div></div>
    <div class="kpi"><div class="k">上游会话</div><div class="v" id="kSession">—</div><div class="d" id="kSessionSub"></div></div>
    <div class="kpi"><div class="k">本地台账</div><div class="v" id="kTasks">—</div><div class="d" id="kTasksSub"></div></div>
    <div class="kpi"><div class="k">护栏</div><div class="v" id="kGuard">—</div><div class="d" id="kGuardSub"></div></div>
  </div>

  <section>
    <h2>任务台账 <span class="tag" id="taskTag">最近 50 条</span></h2>
    <div id="taskWrap"><div class="empty">加载中…</div></div>
  </section>

  <div class="cols">
    <section>
      <h2>报文干跑 <span class="tag">零计费</span></h2>
      <div class="pad">
        <label class="f" for="prompt">prompt</label>
        <textarea id="prompt" placeholder="一只橘猫在雨天的霓虹街头慢跑">一只橘猫在雨天的霓虹街头慢跑</textarea>
        <div class="grid2" style="margin-top:10px">
          <div><label class="f" for="dur">duration（4–30）</label><input class="t" id="dur" type="number" value="4" min="4" max="30"></div>
          <div><label class="f" for="ratio">ratio</label>
            <select class="t" id="ratio">
              <option>auto</option><option>16:9</option><option>9:16</option><option>1:1</option>
              <option>4:3</option><option>3:4</option><option>21:9</option>
            </select></div>
        </div>
        <div class="actions"><button class="btn" id="dryBtn">翻译并校验</button>
          <span class="hint">只走本地纯函数，不碰上游</span></div>
        <pre class="out" id="dryOut" hidden></pre>
      </div>
    </section>

    <section>
      <h2>上游会话 <span class="tag">cookie</span></h2>
      <div class="pad">
        <p class="hint" style="margin-top:0" id="cookieInfo">读取中…</p>
        <label class="f" for="curl">粘贴 DevTools「Copy as cURL」（必须含 <code>-H 'cookie: …'</code>）</label>
        <textarea id="curl" placeholder="curl 'https://fnf-api-gw.higgsfield.ai/fnf/jobs?size=1' -H 'cookie: __client=...; __client_uat=...' ..."></textarea>
        <div class="actions">
          <button class="btn" id="ckBtn">导入并自检</button>
          <button class="btn" id="rlBtn" style="background:#4b5563">重载已有 cookie</button>
        </div>
        <pre class="out" id="ckOut" hidden></pre>
      </div>
    </section>
  </div>

  <section>
    <h2>只读配置 <span class="tag">改 .env 后 docker compose up -d 生效</span></h2>
    <div class="pad mono" id="cfg" style="line-height:1.9">读取中…</div>
  </section>
</main>
<footer>计费开关不在面板里做：<code>ALLOW_BILLING</code> 只能改 .env 并重启，避免误点一下就花钱。</footer>
<script>
const $ = s => document.querySelector(s);

async function api(path, opts = {}) {
  const r = await fetch(path, {credentials: 'same-origin', ...opts});
  if (r.status === 401) { location.href = '/panel/login'; throw new Error('未登录'); }
  const txt = await r.text();
  let data; try { data = JSON.parse(txt); } catch { data = {raw: txt}; }
  if (!r.ok) throw new Error((data.error && data.error.message) || ('HTTP ' + r.status));
  return data;
}

function pill(text, cls) {
  const s = document.createElement('span');
  s.className = 'pill ' + cls; s.textContent = text; return s;
}

const ST = {queued:['排队','run'], running:['生成中','run'], succeeded:['成功','ok'],
            failed:['失败','bad'], cancelled:['已取消','off'], expired:['过期','warn']};

function renderTasks(tasks) {
  const wrap = $('#taskWrap');
  if (!tasks.length) { wrap.innerHTML = '<div class="empty">还没有任务。先在上游会话里导入 cookie，再到干跑区核对报文。</div>'; return; }
  const t = document.createElement('table');
  const thead = document.createElement('thead');
  thead.innerHTML = '<tr><th>创建时间</th><th>ID</th><th>状态</th><th>时长/比例</th><th>prompt</th><th>产物</th></tr>';
  t.appendChild(thead);
  const tb = document.createElement('tbody');
  for (const x of tasks) {
    const tr = document.createElement('tr'); tr.className = 'row';
    const created = new Date((x.created_at || 0) * 1000);
    const when = document.createElement('td');
    when.className = 'mono';
    when.textContent = created.toLocaleString('zh-CN', {hour12: false});
    const id = document.createElement('td');
    id.className = 'mono'; id.textContent = (x.id || '').slice(0, 14) + ((x.id || '').length > 14 ? '…' : '');
    const status = document.createElement('td');
    const [label, cls] = ST[x.status] || [x.status || '—', 'off'];
    status.appendChild(pill(label, cls));
    if (x.error && x.error.message) {
      const e = document.createElement('div');
      e.className = 'hint'; e.style.marginTop = '4px';
      e.textContent = String(x.error.message).slice(0, 90);
      status.appendChild(e);
    }
    const meta = document.createElement('td');
    meta.className = 'mono';
    meta.textContent = (x.duration || '—') + 's / ' + (x.ratio || '—') + ' / ' + (x.resolution || '—');
    const pr = document.createElement('td');
    const p = document.createElement('div'); p.className = 'prompt';
    p.textContent = x.prompt || (x.content && x.content.length ? '' : '（无 prompt 记录）');
    pr.appendChild(p);
    const out = document.createElement('td');
    const url = x.content && x.content.video_url;
    if (url && /^https?:\/\//i.test(url)) {
      const a = document.createElement('a');
      a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer';
      a.textContent = '视频 ↗'; a.style.fontSize = '12px';
      out.appendChild(a);
    } else if (x.id) {
      const b = document.createElement('button');
      b.className = 'ghost'; b.textContent = '刷新状态'; b.style.padding = '3px 9px';
      b.onclick = async () => {
        b.disabled = true; b.textContent = '刷新中…';
        try { const d = await api('/panel/api/task/' + encodeURIComponent(x.id)); }
        catch (e) { b.textContent = '失败'; }
        finally { setTimeout(() => { b.disabled = false; b.textContent = '刷新状态'; refresh(false); }, 600); }
      };
      out.appendChild(b);
    } else { out.textContent = '—'; }
    tr.append(when, id, status, meta, pr, out);
    tb.appendChild(tr);
  }
  t.appendChild(tb);
  wrap.replaceChildren(t);
}

function setPill(el, session) {
  el.className = 'pill ' + (session.ok ? 'ok' : (session.status === 401 ? 'bad' : 'warn'));
  el.textContent = session.ok ? '会话可用' : ('会话异常 ' + (session.status || ''));
  el.title = session.detail || '';
}

async function refresh(force) {
  try {
    const s = await api('/panel/api/state' + (force ? '?force=1' : ''));
    const f = s.flags;
    $('#kBilling').textContent = f.billing ? '已开启' : '关闭';
    $('#kBilling').style.color = f.billing ? 'var(--warn)' : 'var(--ok)';
    $('#kBillingSub').textContent = f.billing ? '建任务会真实消耗额度' : '建任务返回 403，只翻译不花钱';
    $('#kSession').textContent = s.session.ok ? '正常' : '待导入';
    $('#kSession').style.color = s.session.ok ? 'var(--ok)' : 'var(--bad)';
    $('#kSessionSub').textContent = s.cookie.present ? ('cookie ' + s.cookie.count + ' 项') : '无 cookie 文件';
    $('#kTasks').textContent = s.tasks.length;
    const c = s.counts;
    $('#kTasksSub').textContent = '成功 ' + c.succeeded + ' · 失败 ' + c.failed + ' · 进行中 ' + (c.queued + c.running);
    $('#kGuard').textContent = '≤' + f.max_duration + 's / ≤' + f.max_batch + ' 条';
    $('#kGuardSub').textContent = (f.auth ? 'API 鉴权已开' : '⚠ API 无鉴权') + ' · 免费额度优先 ' + (f.use_free_gens ? '是' : '否');
    $('#hdrFlags').textContent = f.billing ? '计费已开' : '计费关闭';
    setPill($('#hdrSession'), s.session);

    const ci = s.cookie;
    $('#cookieInfo').textContent = ci.present
      ? ('当前 cookie：' + ci.count + ' 项，来源 ' + ci.source + (ci.updated_at ? '，更新于 ' + new Date(ci.updated_at * 1000).toLocaleString('zh-CN', {hour12: false}) : ''))
      : '当前没有 cookie 文件 —— 建任务会 401，需要先导入';

    renderTasks(s.tasks);
    $('#cfg').textContent = '';
    const rows = {
      'SERVICE_TOKEN': f.auth ? '已设置（面板与 API 共用同一口令）' : '未设置（危险：API 无鉴权）',
      'ALLOW_BILLING': String(f.billing),
      'MAX_DURATION': f.max_duration + ' 秒',
      'MAX_BATCH': String(f.max_batch),
      'USE_FREE_GENS': String(f.use_free_gens),
      'EXECUTION_EXPIRES_AFTER': f.expires_after + ' 秒',
      '服务器时间': new Date(s.server_time * 1000).toLocaleString('zh-CN', {hour12: false})
    };
    for (const [k, v] of Object.entries(rows)) {
      const d = document.createElement('div');
      d.textContent = k + ' = ' + v;
      $('#cfg').appendChild(d);
    }
  } catch (e) {
    console.error(e);
  }
}

$('#dryBtn').onclick = async () => {
  const out = $('#dryOut'); out.hidden = false; out.textContent = '计算中…';
  const body = {
    model: 'magi_2',
    content: [{type: 'text', text: $('#prompt').value || 'x'}],
    duration: Number($('#dur').value || 4),
    ratio: $('#ratio').value
  };
  try { out.textContent = JSON.stringify(await api('/panel/api/dry-run', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}), null, 2); }
  catch (e) { out.textContent = '错误：' + e.message; }
};

$('#ckBtn').onclick = async () => {
  const out = $('#ckOut'); out.hidden = false;
  const txt = $('#curl').value.trim();
  if (!txt) { out.textContent = '先粘贴 Copy as cURL 的内容'; return; }
  out.textContent = '导入并自检中…';
  try {
    const r = await api('/panel/api/cookie', {method: 'POST', headers: {'Content-Type': 'text/plain'}, body: txt});
    out.textContent = JSON.stringify(r, null, 2);
    refresh(true);
  } catch (e) { out.textContent = '错误：' + e.message; }
};

$('#rlBtn').onclick = async () => {
  const out = $('#ckOut'); out.hidden = false; out.textContent = '重载中…';
  try { out.textContent = JSON.stringify(await api('/panel/api/reload', {method: 'POST'}), null, 2); refresh(true); }
  catch (e) { out.textContent = '错误：' + e.message; }
};

refresh(true);
setInterval(() => refresh(false), 15000);
</script></body></html>"""
