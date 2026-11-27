"""转换层（纯函数）：方舟 canonical <-> Higgsfield MAGI-2。

这里不出现任何网络调用：三个交付门（轮询 / 回调 / 同步等待）共享同一套序列化，
一旦折叠函数里混进 IO，就无法用同一批用例覆盖它们。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .config import settings
from .errors import (
    CODE_INVALID,
    CODE_MISSING,
    CODE_SENSITIVE,
    CODE_TASK_TYPE,
    CODE_UNSUPPORTED,
    ApiError,
)

# MAGI-2 实测/取证到的取值域
RATIOS = ["auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]
RESOLUTIONS = ["1080p"]          # 前端校验原文：MAGI-2 renders only 1080p
DURATION_MIN, DURATION_MAX = 4, 30
MAX_IMAGES, MAX_VIDEOS, MAX_AUDIOS = 10, 5, 5

MODEL_ALIASES = {
    "magi_2", "magi-2", "magi2", "magi 2",
    "higgsfield/magi_2", "higgsfield/magi-2", "higgsfield-ai/magi_2",
}
UPSTREAM_JOB_TYPE = "magi_2"

# 明确不支持、且必须显式拒绝的参数（静默丢弃会改变账单或画面）
REJECT_KEYS = {
    "seed": "MAGI-2 不开放 seed",
    "frames": "MAGI-2 用 duration（秒），不接受 frames",
    "watermark": "MAGI-2 无水印开关",
    "service_tier": "MAGI-2 无 service_tier",
    "draft": "MAGI-2 无样片模式",
    "priority": "MAGI-2 无优先级参数",
    "tools": "MAGI-2 不支持工具调用",
    "output_format": "MAGI-2 固定 mp4",
    "camera_fixed": "MAGI-2 无固定机位参数",
}

STATUS_MAP = {
    "queued": "queued",
    "in_progress": "running",
    "completed": "succeeded",
    "failed": "failed",
    "nsfw": "failed",
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "expired": "expired",
}
TERMINAL = {"succeeded", "failed", "cancelled", "expired"}


@dataclass
class Media:
    role: str          # start_image | end_image | image | video | audio
    asset_id: str
    dur: float | None = None


@dataclass
class Request:
    model_echo: str
    prompt: str = ""
    duration: int | None = None
    resolution: str | None = None
    ratio: str | None = None
    batch_size: int = 1
    medias: list[Media] = field(default_factory=list)
    callback_url: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def mode(self) -> str:
        if any(m.role in ("start_image", "end_image") for m in self.medias):
            return "keyframes"
        if self.medias:
            return "references"
        return "text"


# --------------------------------------------------------------------------- 入参

def _asset_id(url: str, kind: str) -> str:
    """素材引用只接受 asset://<id> 或裸 id。URL / data URI 需要先上传，当前版本不代传。"""
    if url.startswith("asset://"):
        return url[len("asset://"):]
    if url.startswith("data:"):
        raise ApiError(CODE_UNSUPPORTED, f"{kind} 不支持 data URI：请先上传素材，再用 asset://<id> 引用", 400,
                       param=f"content.{kind}")
    if url.startswith("http://") or url.startswith("https://"):
        raise ApiError(
            CODE_UNSUPPORTED,
            f"{kind} 上游只收素材 ID（medias[].data.id）。公网 URL 需先经 /admin/upload 转成素材，"
            f"或直接传 asset://<id>",
            400,
            param=f"content.{kind}",
        )
    # 裸 uuid
    return url


def parse_request(body: Any) -> Request:
    if not isinstance(body, dict):
        raise ApiError(CODE_INVALID, "请求体必须是 JSON 对象", 400)

    raw_model = body.get("model")
    if not isinstance(raw_model, str) or not raw_model.strip():
        raise ApiError(CODE_MISSING, "model 必填", 400, param="model")
    if raw_model.strip().lower() not in MODEL_ALIASES:
        raise ApiError(CODE_INVALID, f"未知模型 {raw_model!r}；本服务只代理 MAGI-2", 400, param="model")

    content = body.get("content")
    if not isinstance(content, list) or not content:
        raise ApiError(CODE_INVALID, "content 必须是非空数组", 400, param="content")

    req = Request(model_echo=raw_model)

    texts: list[str] = []
    for i, item in enumerate(content):
        if not isinstance(item, dict):
            raise ApiError(CODE_INVALID, f"content[{i}] 必须是对象", 400, param=f"content[{i}]")
        ctype = item.get("type")
        if ctype == "text":
            t = item.get("text")
            if t is None:
                raise ApiError(CODE_INVALID, f"content[{i}].text 缺失", 400, param=f"content[{i}].text")
            if isinstance(t, str) and t.strip():
                texts.append(t)
        elif ctype in ("image_url", "video_url", "audio_url"):
            payload = item.get(ctype) or {}
            url = payload.get("url") if isinstance(payload, dict) else None
            if not isinstance(url, str) or not url.strip():
                raise ApiError(CODE_INVALID, f"content[{i}].{ctype}.url 缺失", 400, param=f"content[{i}].{ctype}.url")
            role = _role_for(ctype, item.get("role"), req, i)
            req.medias.append(Media(role=role, asset_id=_asset_id(url, ctype)))
        elif ctype == "draft_task":
            raise ApiError(CODE_UNSUPPORTED, "MAGI-2 不支持 draft_task", 400, param=f"content[{i}]")
        else:
            raise ApiError(CODE_INVALID, f"不支持的 content type：{ctype!r}", 400, param=f"content[{i}].type")

    req.prompt = "\n".join(texts).strip()
    if not req.prompt and not req.medias:
        raise ApiError(CODE_INVALID, "content 里既没有文本也没有素材", 400, param="content")

    _check_scene(req)

    # duration
    dur = body.get("duration")
    if dur is not None:
        if not isinstance(dur, int) or isinstance(dur, bool):
            raise ApiError(CODE_INVALID, "duration 必须是整数秒", 400, param="duration")
        if dur == -1:
            dur = DURATION_MIN          # -1 = 模型自选；MAGI-2 不认 -1，取最短档，避免账单失控
            req.warnings.append("duration=-1 已按最短 4 秒下发（MAGI-2 不支持自选时长）")
        if not (DURATION_MIN <= dur <= DURATION_MAX):
            raise ApiError(CODE_INVALID, f"duration 必须在 {DURATION_MIN}..{DURATION_MAX} 秒，收到 {dur}", 400,
                           param="duration")
    req.duration = dur

    # resolution
    res = body.get("resolution")
    if res is not None:
        if not isinstance(res, str):
            raise ApiError(CODE_INVALID, "resolution 必须是字符串", 400, param="resolution")
        if res not in RESOLUTIONS:
            raise ApiError(
                CODE_INVALID,
                f"MAGI-2 只出 1080p，收到 {res!r}。这里不做静默升档：分辨率直接决定账单",
                400, param="resolution",
            )
    req.resolution = res

    # ratio
    ratio = body.get("ratio")
    if ratio is not None:
        if ratio == "adaptive":
            ratio = "auto"
        if ratio not in RATIOS:
            raise ApiError(CODE_INVALID, f"ratio 取值必须是 {RATIOS} 或 adaptive，收到 {ratio!r}", 400, param="ratio")
    req.ratio = ratio

    # batch
    bs = body.get("batch_size", body.get("n", 1))
    if not isinstance(bs, int) or isinstance(bs, bool) or bs < 1:
        raise ApiError(CODE_INVALID, "batch_size 必须是正整数", 400, param="batch_size")
    if bs > settings.max_batch:
        raise ApiError(
            CODE_INVALID,
            f"batch_size={bs} 超过本机上限 {settings.max_batch}；请调 MAX_BATCH 或分批提交",
            400, param="batch_size",
        )
    req.batch_size = bs

    for key, why in REJECT_KEYS.items():
        if key in body and body[key] is not None:
            raise ApiError(CODE_UNSUPPORTED, f"{why}（字段 {key}）", 400, param=key)

    if body.get("generate_audio") is False:
        raise ApiError(CODE_UNSUPPORTED, "MAGI-2 固定带音轨，无法关闭", 400, param="generate_audio")

    cb = body.get("callback_url")
    if cb is not None:
        if not isinstance(cb, str) or not cb.startswith(("http://", "https://")):
            raise ApiError(CODE_INVALID, "callback_url 必须是 http(s) URL", 400, param="callback_url")
    req.callback_url = cb

    return req


def _role_for(ctype: str, role: Any, req: Request, idx: int) -> str:
    if ctype == "image_url":
        if role is None:
            # 省略 role 只在「单图首帧」成立；这里先记 image，_check_scene 再收口
            return "image"
        if role not in ("first_frame", "last_frame", "reference_image"):
            raise ApiError(CODE_INVALID, f"image_url 的 role 非法：{role!r}", 400, param=f"content[{idx}].role")
        return {"first_frame": "start_image", "last_frame": "end_image", "reference_image": "image"}[role]
    if ctype == "video_url":
        if role not in (None, "reference_video"):
            raise ApiError(CODE_INVALID, f"video_url 的 role 必须是 reference_video，收到 {role!r}", 400,
                           param=f"content[{idx}].role")
        return "video"
    if ctype == "audio_url":
        if role not in (None, "reference_audio"):
            raise ApiError(CODE_INVALID, f"audio_url 的 role 必须是 reference_audio，收到 {role!r}", 400,
                           param=f"content[{idx}].role")
        return "audio"
    raise ApiError(CODE_INVALID, f"未知载体 {ctype!r}", 400)


def _check_scene(req: Request) -> None:
    """三类场景互斥：首尾帧 / 全模态参考。混用即 400（方舟官方约束）。"""
    has_frame = any(m.role in ("start_image", "end_image") for m in req.medias)
    has_ref = any(m.role in ("image", "video", "audio") for m in req.medias)

    # 单图且没写 role：按首帧处理（Ark 的唯一省略语义）
    if len(req.medias) == 1 and req.medias[0].role == "image" and not has_frame and req.medias[0].asset_id:
        single = req.medias[0]
        # 只有一张图时，image 与 start_image 语义不同；保持 image，交给上游当参考图，
        # 但记一条 warning 让调用方知道它没被当成首帧。
        req.warnings.append(
            "单张 image_url 省略 role 时按参考图下发；要首帧请显式写 role=first_frame"
        )
        del single
        return

    if has_frame and has_ref:
        raise ApiError(
            CODE_TASK_TYPE,
            "首尾帧场景不能混入参考素材（reference_image / reference_video / reference_audio）",
            400, param="content",
        )

    starts = [m for m in req.medias if m.role == "start_image"]
    ends = [m for m in req.medias if m.role == "end_image"]
    if len(starts) > 1 or len(ends) > 1:
        raise ApiError(CODE_TASK_TYPE, "首帧 / 尾帧各最多 1 张", 400, param="content")
    if ends and not starts:
        raise ApiError(CODE_TASK_TYPE, "给了 last_frame 就必须给 first_frame", 400, param="content")

    imgs = [m for m in req.medias if m.role == "image"]
    vids = [m for m in req.medias if m.role == "video"]
    auds = [m for m in req.medias if m.role == "audio"]
    if len(imgs) > MAX_IMAGES:
        raise ApiError(CODE_INVALID, f"MAGI-2 最多 {MAX_IMAGES} 张参考图，收到 {len(imgs)}", 400, param="content")
    if len(vids) > MAX_VIDEOS:
        raise ApiError(CODE_INVALID, f"MAGI-2 最多 {MAX_VIDEOS} 段参考视频，收到 {len(vids)}", 400, param="content")
    if len(auds) > MAX_AUDIOS:
        raise ApiError(CODE_INVALID, f"MAGI-2 最多 {MAX_AUDIOS} 段参考音频，收到 {len(auds)}", 400, param="content")


# --------------------------------------------------------------------------- 出参

def build_params(req: Request) -> dict[str, Any]:
    """canonical -> 上游 params。默认值不主动下发，只发调用方明确要的东西 + 成本护栏。"""
    params: dict[str, Any] = {
        "prompt": req.prompt,
        "resolution": req.resolution or "1080p",
        "duration": req.duration if req.duration is not None else settings.max_duration,
        "aspect_ratio": req.ratio or "auto",
        "batch_size": req.batch_size,
        "medias": [
            {"role": m.role, "data": {"id": m.asset_id, "type": _media_type(m.role)}}
            for m in req.medias
        ],
    }
    return params


def _media_type(role: str) -> str:
    if role in ("start_image", "end_image", "image"):
        return "image"
    if role == "video":
        return "video"
    return "audio"


def wrap_body(params: dict[str, Any], use_free_gens: bool) -> dict[str, Any]:
    """上游创建体的外层信封。前端所有 jobs/v2 调用都是 `{params: ...}`。"""
    body: dict[str, Any] = {"params": params}
    if use_free_gens:
        body["use_free_gens"] = True
    return body


# --------------------------------------------------------------------------- 任务对象

def _first_url(node: Any) -> str | None:
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        for k in ("url", "video_url"):
            if isinstance(node.get(k), str):
                return node[k]
    if isinstance(node, list):
        for x in node:
            u = _first_url(x)
            if u:
                return u
    return None


def extract_video_url(job: dict[str, Any]) -> str | None:
    results = job.get("results") or {}
    for key in ("raw", "min", "video"):
        u = _first_url(results.get(key))
        if u:
            return u
    return _first_url(results) or _first_url(job.get("result"))


def normalize_status(job: dict[str, Any]) -> str:
    raw = job.get("status")
    if not isinstance(raw, str):
        return "running"          # 未知值收敛为非终态，绝不落到 succeeded
    return STATUS_MAP.get(raw.strip().lower(), "running")


def job_to_task(
    job: dict[str, Any],
    *,
    model_echo: str,
    submitted_at: int,
    expires_after: int | None = None,
    artifact_wait: int = 90,
    now: int | None = None,
) -> dict[str, Any]:
    """上游 job -> canonical 任务对象。查询 / 回调共用这一个函数。"""
    now = now or int(time.time())
    expires_after = expires_after or settings.execution_expires_after

    status = normalize_status(job)
    url = extract_video_url(job)
    results = job.get("results") or {}

    # 产物闸门：报成功但没产物，不是终态
    if status == "succeeded" and not url:
        status = "running"
    # 非终态但产物已出现：认产物
    if status in ("queued", "running") and url:
        status = "succeeded"

    # 本地判定过期
    created = int(job.get("created_at") or submitted_at)
    if status not in TERMINAL and now - created > expires_after:
        status = "expired"

    task: dict[str, Any] = {
        "id": job.get("id") or job.get("job_id") or "",
        "model": model_echo,
        "status": status,
        "created_at": created,
        "updated_at": int(job.get("updated_at") or now),
    }

    params = job.get("params") or {}
    if isinstance(params, dict):
        if isinstance(params.get("duration"), (int, float)):
            task["duration"] = int(params["duration"])
        if isinstance(params.get("resolution"), str):
            task["resolution"] = params["resolution"]
        if isinstance(params.get("aspect_ratio"), str):
            task["ratio"] = params["aspect_ratio"]
        if isinstance(params.get("batch_size"), int):
            task["batch_size"] = params["batch_size"]

    if status == "succeeded" and url:
        task["content"] = {"video_url": url}
        thumb = None
        if isinstance(results, dict):
            rawnode = results.get("raw")
            if isinstance(rawnode, dict):
                thumb = rawnode.get("thumbnail_url") or rawnode.get("min_url")
        if thumb:
            task["content"]["last_frame_url"] = thumb  # 上游没给尾帧图时保留缩略图位，不做假承诺

    if status == "failed":
        task["error"] = _error_of(job)

    if job.get("job_set_id"):
        task["job_set_id"] = job["job_set_id"]
    if job.get("credits_cost") is not None:
        task["usage"] = {"credits_cost": job["credits_cost"]}

    return task


def _error_of(job: dict[str, Any]) -> dict[str, Any]:
    raw_status = str(job.get("status") or "").lower()
    if raw_status == "nsfw":
        return {
            "code": CODE_SENSITIVE,
            "message": "上游判定为 NSFW / 内容审核未通过",
            "type": "BadRequest",
        }
    reason = job.get("error") or job.get("failure_reason") or job.get("reason")
    msg = "上游任务失败"
    code = "InternalServiceError"
    if isinstance(reason, str) and reason.strip():
        msg = reason.strip()[:500]
        low = msg.lower()
        if any(k in low for k in ("nsfw", "sensitive", "moderation", "policy")):
            code = CODE_SENSITIVE
        elif any(k in low for k in ("invalid", "parameter", "unsupported")):
            code = CODE_INVALID
    elif isinstance(reason, dict):
        msg = str(reason.get("message") or reason)[:500]
        if reason.get("code"):
            code = str(reason["code"])
    return {
        "code": code,
        "message": msg,
        "type": "BadRequest" if code in (CODE_SENSITIVE, CODE_INVALID) else "InternalServerError",
        "upstream": {"status": job.get("status")},
    }
