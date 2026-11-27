"""FastAPI 出口：方舟（Seedance）视频协议四端点中的核心两个 + 运维端点。

  POST   /contents/generations/tasks   建任务（唯一计费动作）
  GET    /tasks/{id}                   查任务
  GET    /tasks                        列表（本地台账）
  DELETE /tasks/{id}                   未实现（不造假数据，直接 501）

  POST   /admin/dry-run                零计费：只翻译 + 校验，打印真实上游报文
  GET    /admin/session                零计费：号池概览 + 逐号自检
  GET    /admin/accounts               零计费：号池状态（不探测，秒回）
  POST   /admin/reload                 零计费：从磁盘重载号池（改完不必重启）

鉴权：设置 SERVICE_TOKEN 后，除 /healthz 外全部端点要求 `Authorization: Bearer <token>`
（或 `X-Api-Key: <token>`）。未设置时不做鉴权——此模式只能绑回环地址。
"""
from __future__ import annotations

import asyncio
import hmac
import time
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Header, Request as HttpRequest
from fastapi.responses import JSONResponse

from .config import settings
from .convert import TERMINAL, build_params, job_to_task, parse_request, preview, wrap_body
from .errors import (
    CODE_AUTH,
    CODE_BILLING_OFF,
    CODE_INTERNAL,
    ApiError,
)
from . import panel
from .pool import pool
from .store import Store
from .upstream import Upstream

app = FastAPI(title="Higgsfield MAGI-2 → Ark Video Protocol", version="0.1.0")
store = Store()
upstream = Upstream()
_bg: set[asyncio.Task] = set()

app.include_router(panel.router)

OPEN_PATHS = {"/healthz"}


@app.middleware("http")
async def _gate(request: HttpRequest, call_next: Any) -> Any:
    """两道门：`/panel*` 走面板口令（cookie），其余接口走 Bearer token。

    中间件里不能抛 ApiError（异常处理器在中间件内层），一律直接回 JSONResponse。
    """
    path = request.url.path
    if request.method == "OPTIONS" or path in OPEN_PATHS:
        return await call_next(request)

    if path == "/panel" or path.startswith("/panel/"):
        return await panel.guard(request, call_next)

    token = settings.service_token
    if not token:
        return await call_next(request)

    auth = (request.headers.get("authorization") or "").strip()
    provided = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    if not provided:
        provided = (request.headers.get("x-api-key") or "").strip()

    if not hmac.compare_digest(provided, token):
        exc = ApiError(
            CODE_AUTH,
            "缺少或错误的令牌：请带 `Authorization: Bearer <SERVICE_TOKEN>`",
            401,
        )
        return JSONResponse(status_code=401, content=exc.to_body())
    return await call_next(request)


# --------------------------------------------------------------------------- 基建

@app.exception_handler(ApiError)
async def _api_error(_: HttpRequest, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.to_body())


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    summary = pool.summary()
    return {
        "ok": True,
        "auth_required": bool(settings.service_token),
        "billing_enabled": settings.allow_billing,
        "use_free_gens": settings.use_free_gens,
        "max_duration": settings.max_duration,
        "max_batch": settings.max_batch,
        # 只给数量，不给账号 id —— healthz 是公开端点
        "accounts_total": summary["total"],
        "accounts_usable": summary["usable"],
    }


@app.get("/admin/accounts")
async def admin_accounts() -> dict[str, Any]:
    """号池状态快照。不探测上游，纯内存，秒回。"""
    return {
        "pool": pool.summary(),
        "accounts": [a.to_public() for a in pool.list()],
    }


@app.get("/admin/session")
async def admin_session(probe: int = 1) -> dict[str, Any]:
    """零计费自检：号池逐个 GET /jobs?size=1。`probe=0` 只看状态不打上游。"""
    body: dict[str, Any] = {
        "pool": pool.summary(),
        "accounts": [a.to_public() for a in pool.list()],
    }
    if probe:
        # whoami 是同步的 curl_cffi 调用，必须丢线程：直接 await 会锁死事件循环
        body["probe"] = await asyncio.to_thread(pool.probe_all)
    return body


@app.post("/admin/reload")
async def admin_reload() -> dict[str, Any]:
    """从 data/accounts.json 重读号池并自检，省掉一次重启。"""
    loaded = await asyncio.to_thread(pool.reload)
    results = await asyncio.to_thread(pool.probe_all)
    return {"reloaded": True, **loaded, "probe": results}


# --------------------------------------------------------------------------- 创建

@app.post("/contents/generations/tasks")
async def create_task(
    body: dict[str, Any],
    x_dry_run: str | None = Header(default=None, alias="X-Dry-Run"),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> JSONResponse:
    req = parse_request(body)
    params = build_params(req)

    if x_dry_run and x_dry_run.strip().lower() in {"1", "true", "yes"}:
        return JSONResponse(status_code=200, content=preview(req, params))

    if not settings.allow_billing:
        raise ApiError(
            CODE_BILLING_OFF,
            "计费未开启（ALLOW_BILLING=0）。建任务会真实消耗额度，先 POST /admin/dry-run"
            "（或带 X-Dry-Run: 1）确认报文，再开 ALLOW_BILLING=1",
            403,
        )

    if x_idempotency_key:
        prior = store.idem_get(x_idempotency_key)
        if prior:
            return JSONResponse(status_code=200, content={"id": prior})

    body_out = wrap_body(params, settings.use_free_gens)

    # 提交期失败会直接抛 ApiError（没有 id，改参数重试是有意义的）；
    # 网络超时绝不重发：{id} 是唯一幂等键，重发就是两个计费任务。
    # create 内部会按号池换号重试（仅限「上游明确没受理」的失败），返回用了哪个号
    data, acc_id = await asyncio.to_thread(upstream.create, body_out, dry_run=False)

    job_set_id = data.get("id") or data.get("job_set_id")
    jobs = data.get("jobs") if isinstance(data.get("jobs"), list) else []
    job_id = None
    if jobs and isinstance(jobs[0], dict):
        job_id = jobs[0].get("id")
    job_id = job_id or data.get("job_id") or job_set_id
    if not job_id:
        raise ApiError(CODE_INTERNAL, f"上游受理但没给任务 id：{str(data)[:300]}", 502)

    now = int(time.time())
    task = {
        "id": str(job_id),
        "model": req.model_echo,
        "prompt": req.prompt,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "duration": params["duration"],
        "resolution": params["resolution"],
        "ratio": params["aspect_ratio"],
        "batch_size": params["batch_size"],
    }
    store.put(task, job_id=str(job_id), job_set_id=str(job_set_id) if job_set_id else None,
              callback_url=req.callback_url, account_id=acc_id)
    if x_idempotency_key:
        store.idem_put(x_idempotency_key, str(job_id))

    if req.callback_url:
        t = asyncio.create_task(
            _poll_and_callback(str(job_id), req.model_echo, now, req.callback_url, acc_id)
        )
        _bg.add(t)
        t.add_done_callback(_bg.discard)

    # 方舟原生契约：创建只回 id，HTTP 200，不含 status / content
    return JSONResponse(status_code=200, content={"id": str(job_id)})


@app.post("/admin/dry-run")
async def admin_dry_run(body: dict[str, Any]) -> dict[str, Any]:
    """零计费的等价入口：同一套校验/翻译，不碰上游、不花钱。"""
    req = parse_request(body)
    return preview(req, build_params(req))


# --------------------------------------------------------------------------- 查询

@app.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict[str, Any]:
    row = store.get(task_id)
    job_id = (row or {}).get("job_id") or task_id
    model_echo = (row or {}).get("model") or "magi_2"
    submitted = int((row or {}).get("submitted_at") or time.time())
    acc_id = (row or {}).get("account_id")

    try:
        job = await asyncio.to_thread(upstream.get_job, str(job_id), acc_id)
    except ApiError as exc:
        if exc.http_status == 404:
            if row:
                task = job_to_task({"id": task_id, "status": "expired"},
                                   model_echo=model_echo, submitted_at=submitted)
                store.put(task, callback_url=row.get("callback_url"))
                return task
            raise
        raise

    if isinstance(job.get("jobs"), list) and job["jobs"]:
        merged = dict(job["jobs"][0])
        merged.setdefault("id", job_id)
        merged.setdefault("job_set_id", job.get("id"))
        job = merged

    task = job_to_task(job, model_echo=model_echo, submitted_at=submitted)
    store.put(task, callback_url=(row or {}).get("callback_url"))
    return task


@app.get("/tasks")
async def list_tasks(limit: int = 20) -> dict[str, Any]:
    items = store.list(max(1, min(limit, 100)))
    return {"items": items, "total": len(items)}


@app.delete("/tasks/{task_id}")
async def delete_task(task_id: str) -> JSONResponse:
    raise ApiError(
        "NotImplemented",
        "上游取消端点未取证，本服务不伪造成功。请在网页端取消。",
        501,
    )


# --------------------------------------------------------------------------- 回调

async def _poll_and_callback(task_id: str, model_echo: str, submitted: int, callback_url: str,
                              account_id: str | None = None) -> None:
    """回调不是唯一来源，轮询兜底；两边共用 job_to_task 同一个序列化函数。"""
    deadline = time.time() + settings.execution_expires_after
    last_status = ""
    async with httpx.AsyncClient(timeout=20) as client:
        while time.time() < deadline:
            try:
                job = await asyncio.to_thread(upstream.get_job, task_id, account_id)
                if isinstance(job.get("jobs"), list) and job["jobs"]:
                    job = job["jobs"][0]
                task = job_to_task(job, model_echo=model_echo, submitted_at=submitted)
            except ApiError:
                await asyncio.sleep(settings.poll_interval * 2)
                continue

            if task["status"] != last_status:
                last_status = task["status"]
                store.put(task)
                if task["status"] in TERMINAL:
                    await _post_callback(client, callback_url, task)
                    return
            await asyncio.sleep(settings.poll_interval)


async def _post_callback(client: httpx.AsyncClient, url: str, task: dict[str, Any]) -> None:
    for _ in range(3):
        try:
            r = await client.post(url, json=task)
            if r.status_code < 300:
                return
        except Exception:
            pass
        await asyncio.sleep(5)
