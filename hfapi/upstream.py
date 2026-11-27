"""上游客户端：fnf-api-gw /jobs/v2/magi_2 的创建与查询。

计费红线：`create()` 是唯一会花钱的调用，只有 `ALLOW_BILLING=1` 且显式 dry_run=False 时才会走到这里。
`get_job()` 是只读，实测 `GET /jobs/{id}` 不校验 JWT（随机 UUID 返回 404 "Job not found"），
所以轮询不受 token 60 秒寿命影响。
"""
from __future__ import annotations

import time
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
)
from .session import Session


class Upstream:
    def __init__(self, session: Session | None = None) -> None:
        self.session = session or Session()

    # ------------------------------------------------------------------ 创建
    def create(self, body: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
        url = f"{settings.gateway}/jobs/v2/{UPSTREAM_JOB_TYPE}"
        if dry_run:
            return {"dry_run": True, "url": url, "body": body}

        r = self.session.request("POST", url, json_body=body)
        return self._parse(r, stage="create")

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
    def get_job(self, job_id: str) -> dict[str, Any]:
        """单任务查询走 /jobs/{id}（实测匿名可读）。只有它 404 时才回退 /job-sets/{id}，
        回退路径要求鉴权，401/403 一律当作「查不到」，不能让轮询因为 token 过期而失败。"""
        r = self.session.request("GET", f"{settings.gateway}/jobs/{job_id}", authed=False)
        if r.status_code == 200:
            try:
                return r.json()
            except Exception as exc:
                raise ApiError(CODE_INTERNAL, "上游返回非 JSON", 502) from exc
        if r.status_code != 404:
            self._parse(r, stage="get")

        r2 = self.session.request("GET", f"{settings.gateway}/job-sets/{job_id}", authed=False)
        if r2.status_code == 200:
            try:
                return r2.json()
            except Exception as exc:
                raise ApiError(CODE_INTERNAL, "上游返回非 JSON", 502) from exc
        raise ApiError("NotFound", f"上游查不到任务 {job_id}", 404)


def snapshot(upstream: Upstream, job_id: str, *, model_echo: str, submitted_at: int) -> dict[str, Any]:
    job = upstream.get_job(job_id)
    if "jobs" in job and isinstance(job["jobs"], list) and job["jobs"]:
        job = job["jobs"][0]
    return job_to_task(job, model_echo=model_echo, submitted_at=submitted_at, now=int(time.time()))
