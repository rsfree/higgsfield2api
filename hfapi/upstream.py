"""上游客户端：fnf-api-gw /jobs/v2/magi_2 的创建与查询（多账号版）。

计费红线：`create()` 是唯一会花钱的调用，只有 `ALLOW_BILLING=1` 且显式 dry_run=False 时才会走到这里。
`get_job()` 是只读，实测 `GET /jobs/{id}` 不校验 JWT（随机 UUID 返回 404 "Job not found"），
所以轮询不受 token 60 秒寿命影响。

**重试边界**（号池最关键的一条）：
  可重试 = 上游明确拒绝了这次请求，任务肯定没建 —— 401（会话失效）、429（限流）、
  403 Cloudflare 拦截。换一个号重发是安全的。
  不可重试 = 400/422（参数问题，换号也没用）、5xx（可能已受理）、网络异常（请求可能已到达）。
  后两类重发就是第二个计费任务，宁可报错让调用方自己决定。
"""
from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

from .config import settings
from .convert import UPSTREAM_JOB_TYPE, job_to_task
from .errors import (
    CODE_AUTH,
    CODE_INTERNAL,
    CODE_INVALID,
    CODE_RATE_LIMIT,
    CODE_UNAVAILABLE,
    ApiError,
    is_upstream_401,
)
from .pool import AccountPool, pool as default_pool
from .session import Session

def _retriable(exc: ApiError) -> bool:
    """上游明确没受理 ⇒ 换号重发不会多花钱。

    401 判的是 `upstream.http`，不是我们的 `http_status`：网关侧凭据失效对外是 502，
    因为「调用方自己的 token 错了」和「我们池子里的 cookie 过期了」是两回事。
    """
    if is_upstream_401(exc):
        return True
    if exc.http_status == 429:
        return True
    return exc.code == CODE_UNAVAILABLE and exc.http_status == 503


@lru_cache(maxsize=1)
def _anonymous() -> Session:
    """查询用的匿名会话：`GET /jobs/{id}` 不需要登录，号池为空时也能查任务。"""
    return Session({})


class Upstream:
    def __init__(self, pool: AccountPool | None = None) -> None:
        self.pool = pool or default_pool

    # ------------------------------------------------------------------ 创建
    def create(
        self,
        body: dict[str, Any],
        *,
        dry_run: bool = True,
        exclude: frozenset[str] = frozenset(),
    ) -> tuple[dict[str, Any], str | None]:
        """返回 `(上游响应, 使用的账号 id)`。

        失败时会自动换号重试，但只对 `_RETRIABLE` 里的情况——见模块文档。
        """
        url = f"{settings.gateway}/jobs/v2/{UPSTREAM_JOB_TYPE}"
        if dry_run:
            return {"dry_run": True, "url": url, "body": body}, None

        tried = set(exclude)
        last: ApiError | None = None
        attempts = max(1, settings.pool_retry)

        for _ in range(attempts):
            try:
                acc = self.pool.acquire(frozenset(tried))
            except ApiError:
                if last is not None:      # 已经试过至少一个号，报更具体的那个错
                    break
                raise

            try:
                r = acc.session.request("POST", url, json_body=body)
            except Exception as exc:  # noqa: BLE001 网络层
                err = ApiError(
                    CODE_INTERNAL,
                    f"上游网络异常，任务可能已提交，**不要自动重试**：{type(exc).__name__}: {exc}",
                    502,
                    upstream={"stage": "create", "unknown_outcome": True},
                )
                self.pool.release(acc, ok=False, exc=err)
                raise err from exc

            try:
                data = self._parse(r, stage="create")
            except ApiError as exc:
                self.pool.release(acc, ok=False, exc=exc)
                if not _retriable(exc):
                    raise
                tried.add(acc.id)
                last = exc
                continue

            self.pool.release(acc, ok=True)
            return data, acc.id

        raise last or ApiError(CODE_UNAVAILABLE, "号池里没有可用账号", 503)

    def _parse(self, r: Any, *, stage: str) -> dict[str, Any]:
        text = (r.text or "")[:800]
        ctype = r.headers.get("content-type", "")

        if r.status_code == 403 and "html" in ctype:
            raise ApiError(
                CODE_UNAVAILABLE,
                "被 Cloudflare 拦截（需要 cf_clearance，或指纹不匹配）。重新导出 cookie，"
                "并确认 HF_IMPERSONATE=chrome",
                503,
                upstream={"stage": stage, "http": 403, "body": text[:200]},
            )
        if r.status_code == 401:
            # 对外是 502：调用方的 token 没错，是我们池子里的 cookie 过期了
            raise ApiError(
                CODE_AUTH,
                "上游 401：cookie / Clerk 会话已失效，重新导入 cookie",
                502,
                upstream={"stage": stage, "http": 401, "body": text[:200]},
            )
        if r.status_code == 429:
            raise ApiError(
                CODE_RATE_LIMIT, "上游限流", 429,
                upstream={"stage": stage, "http": 429, "body": text[:200]},
            )
        if r.status_code in (400, 422):
            raise ApiError(
                CODE_INVALID,
                f"上游拒绝参数：{text}",
                400,
                upstream={"stage": stage, "http": r.status_code, "body": text[:400]},
            )
        if r.status_code >= 500:
            raise ApiError(
                CODE_INTERNAL, f"上游 {r.status_code}", 502,
                upstream={"stage": stage, "http": r.status_code, "body": text[:200]},
            )
        if r.status_code != 200:
            raise ApiError(
                CODE_INTERNAL, f"上游意外状态 {r.status_code}", 502,
                upstream={"stage": stage, "http": r.status_code, "body": text[:200]},
            )

        try:
            data = r.json()
        except Exception as exc:
            raise ApiError(CODE_INTERNAL, f"上游返回非 JSON：{text[:200]}", 502) from exc
        if not isinstance(data, dict):
            raise ApiError(CODE_INTERNAL, "上游返回结构不是对象", 502)
        return data

    # ------------------------------------------------------------------ 查询
    def _session_for(self, account_id: str | None) -> Session:
        """优先用建任务的那个号（任务归属最稳），否则挑一个可用号，再不行匿名。"""
        acc = self.pool.pick(account_id)
        return acc.session if acc else _anonymous()

    def get_job(self, job_id: str, account_id: str | None = None) -> dict[str, Any]:
        """单任务查询走 /jobs/{id}（实测匿名可读）。只有它 404 时才回退 /job-sets/{id}，
        回退路径要求鉴权，401/403 一律当作「查不到」，不能让轮询因为 token 过期而失败。"""
        sess = self._session_for(account_id)
        r = sess.request("GET", f"{settings.gateway}/jobs/{job_id}", authed=False)
        if r.status_code == 200:
            try:
                return r.json()
            except Exception as exc:
                raise ApiError(CODE_INTERNAL, "上游返回非 JSON", 502) from exc
        if r.status_code != 404:
            self._parse(r, stage="get")

        r2 = sess.request("GET", f"{settings.gateway}/job-sets/{job_id}", authed=False)
        if r2.status_code == 200:
            try:
                return r2.json()
            except Exception as exc:
                raise ApiError(CODE_INTERNAL, "上游返回非 JSON", 502) from exc
        raise ApiError("NotFound", f"上游查不到任务 {job_id}", 404)


def snapshot(upstream: Upstream, job_id: str, *, model_echo: str, submitted_at: int,
             account_id: str | None = None) -> dict[str, Any]:
    job = upstream.get_job(job_id, account_id)
    if "jobs" in job and isinstance(job["jobs"], list) and job["jobs"]:
        job = job["jobs"][0]
    return job_to_task(job, model_echo=model_echo, submitted_at=submitted_at, now=int(time.time()))
